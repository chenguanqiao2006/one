#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_quotes.py — 行情拉取（日线 + 1 分钟），北交所全程过滤
==============================================================

产出：
    data/kline/{sh|sz}/{code}.json    日线，统一数组格式：
        ["YYYY-MM-DD", open, high, low, close, volume]
        周日全量重建 1023 根并覆盖；平日增量 10 根合并去重
    data/kline_1min/{code}.json       1 分钟线，统一数组格式：
        ["YYYY-MM-DD HH:MM", open, high, low, close, volume]

数据源自动切换：
    日线：东财 → 腾讯 → 新浪
    1 分钟：东财(klt=1) → 腾讯(分时接口，仅当日)

说明：本脚本基于原 workflow 的拉取逻辑重建。首次使用请用
workflow_dispatch 手动触发一次，从 Actions 日志确认数据源可达。
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
KLINE_DIR = BASE / 'data' / 'kline'
K1M_DIR = BASE / 'data' / 'kline_1min'
HUS300_FILE = BASE / 'data' / 'hushen300.json'

CST = timezone(timedelta(hours=8))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
TIMEOUT = 15
MAX_WORKERS = 5

# ⚠️ 唯一需要你动手的地方：你原仓库有 9 只自选，我只确认了下面 3 只。
#    把其余 6 只按同样格式补全（sh600xxx / sz00xxxx / sz300xxx）。
HOLDINGS_1MIN = [
    'sh601138',   # 工业富联
    'sz002156',   # 通富微电
    'sh600584',   # 长电科技
    # 'sh600000',  # ← 剩余自选加在这里
]


# ---------------------------------------------------------------------------
# 北交所过滤
# ---------------------------------------------------------------------------

def is_bj(code: str) -> bool:
    c = (code or '').lower().replace('.bj', '').strip()
    if not c:
        return False
    if c.startswith('bj'):
        return True
    digits = c[2:] if (len(c) > 2 and c[:2] in ('sh', 'sz')) else c
    return digits.startswith(('920', '83', '87', '43'))


def normalize_code(raw):
    """'600519' / 'sh600519' / '000688' → 'sh600519' / 'sz000688'；北交所返回 None。"""
    s = re.sub(r'[^0-9]', '', str(raw or ''))
    if len(s) != 6:
        return None
    if s.startswith(('920', '83', '87', '43')):      # 北交所，剔除
        return None
    if s.startswith('6'):                             # 沪市
        return 'sh' + s
    if s.startswith(('0', '3')):                      # 深市主板 / 创业板
        return 'sz' + s
    return None


# ---------------------------------------------------------------------------
# 时间逻辑（北京时间）
# ---------------------------------------------------------------------------

def beijing_now():
    return datetime.now(CST)


def in_trading_hours(dt):
    m = dt.hour * 60 + dt.minute
    return (9 * 60 + 30) <= m < 15 * 60


def is_sunday(dt):
    return dt.weekday() == 6


# ---------------------------------------------------------------------------
# 东财 K 线（日线 klt=101 / 1分钟 klt=1），统一输出
# [时间, open, high, low, close, volume]
# ---------------------------------------------------------------------------

def _em_klines(code, klt, datalen):
    mkt = '1' if code.startswith('sh') else '0'
    url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?'
           f'secid={mkt}.{code[2:]}&fields1=f1,f2,f3,f4,f5,f6'
           f'&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&fqt=1'
           f'&end=20500101&lmt={datalen}')
    j = requests.get(url, headers=UA, timeout=TIMEOUT).json()
    rows = (j.get('data') or {}).get('klines') or []
    out = []
    for s in rows:
        p = s.split(',')                # 东财顺序: 日期,开,收,高,低,量
        if len(p) >= 6:
            out.append([p[0], float(p[1]), float(p[3]),
                        float(p[4]), float(p[2]), float(p[5])])
    return out


# ---------------------------------------------------------------------------
# 日线数据源
# ---------------------------------------------------------------------------

def daily_from_eastmoney(code, datalen):
    return _em_klines(code, 101, datalen)


def daily_from_tencent(code, datalen):
    url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'
           f'param={code},day,,,{datalen},qfq')
    j = requests.get(url, headers=UA, timeout=TIMEOUT).json()
    node = (j.get('data') or {}).get(code) or {}
    rows = node.get('qfqday') or node.get('day') or []
    out = []
    for r in rows:                      # 腾讯顺序: 日期,开,收,高,低,量,...
        if len(r) >= 6:
            out.append([r[0], float(r[1]), float(r[3]),
                        float(r[4]), float(r[2]), float(r[5])])
    return out


def daily_from_sina(code, datalen):
    url = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var/CN_MarketDataService.'
           f'getKLineData?symbol={code}&scale=240&ma=no&datalen={datalen}')
    t = requests.get(url, headers=UA, timeout=TIMEOUT).text
    m = re.search(r'\[.*\]', t, re.S)
    if not m:
        return []
    out = []
    for d in json.loads(m.group(0)):
        try:
            out.append([d['day'], float(d['open']), float(d['high']),
                        float(d['low']), float(d['close']), float(d['volume'])])
        except (KeyError, TypeError, ValueError):
            continue
    return out


DAILY_SOURCES = [('东财', daily_from_eastmoney),
                 ('腾讯', daily_from_tencent),
                 ('新浪', daily_from_sina)]


# ---------------------------------------------------------------------------
# 1 分钟数据源
# ---------------------------------------------------------------------------

def min1_from_eastmoney(code, datalen=480):
    """近 480 根 1 分钟 K（约 2 个交易日），跨日去重由合并逻辑负责。"""
    return _em_klines(code, 1, datalen)


def min1_from_tencent(code):
    """腾讯分时接口（仅当日）。分时是累计量，差分为每分钟量。"""
    url = f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}'
    j = requests.get(url, headers=UA, timeout=TIMEOUT).json()
    info = ((j.get('data') or {}).get(code) or {}).get('data') or {}
    dstr = str(info.get('date') or '')
    rows = info.get('data') or []
    if len(dstr) != 8 or not rows:
        return []
    day = f'{dstr[:4]}-{dstr[4:6]}-{dstr[6:8]}'
    out, prev = [], 0
    for r in rows:
        p = r.split()                    # '0930 15.00 1234'
        if len(p) < 3 or len(p[0]) != 4:
            continue
        try:
            price, cum = float(p[1]), int(float(p[2]))
        except ValueError:
            continue
        vol = max(0, cum - prev)
        prev = cum
        out.append([f'{day} {p[0][:2]}:{p[0][2:]}',
                    price, price, price, price, vol])
    return out


# ---------------------------------------------------------------------------
# 抓取（多源自动切换）
# ---------------------------------------------------------------------------

def fetch_daily(code, datalen):
    for name, fn in DAILY_SOURCES:
        try:
            bars = fn(code, datalen)
            if bars:
                return name, bars
        except Exception as e:
            print(f'    [{name}] {code} 日线失败: {type(e).__name__}: {e}')
    return None, None


def fetch_min1(code):
    try:
        bars = min1_from_eastmoney(code, 480)
        if bars:
            return '东财', bars
    except Exception as e:
        print(f'    [东财] {code} 1分钟失败: {type(e).__name__}: {e}')
    try:
        bars = min1_from_tencent(code)
        if bars:
            return '腾讯', bars
    except Exception as e:
        print(f'    [腾讯] {code} 1分钟失败: {type(e).__name__}: {e}')
    return None, None


# ---------------------------------------------------------------------------
# 合并 / 落盘
# ---------------------------------------------------------------------------

def merge_bars(local, new):
    if not local:
        return sorted(new, key=lambda b: b[0])
    m = {b[0]: b for b in local}
    for b in new:
        m[b[0]] = b                      # 新数据优先
    return [m[k] for k in sorted(m)]


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')),
                   encoding='utf-8')
    tmp.replace(path)                    # 原子替换


# ---------------------------------------------------------------------------
# 股票池
# ---------------------------------------------------------------------------

def load_hushen300():
    if HUS300_FILE.exists():
        try:
            arr = json.loads(HUS300_FILE.read_text(encoding='utf-8'))
            if isinstance(arr, list) and arr:
                if isinstance(arr[0], str):
                    out = [normalize_code(c) for c in arr]
                else:
                    out = [normalize_code(d.get('代码') or d.get('code') or '')
                           for d in arr if isinstance(d, dict)]
                out = [c for c in out if c]
                if out:
                    return [c for c in out if not is_bj(c)]
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol='000300')
        out = [normalize_code(str(x)) for x in df['代码'].tolist()]
        out = [c for c in out if c]
        save_json(HUS300_FILE, out)
        print(f'[INFO] 沪深300 成分股已拉取并缓存: {len(out)} 只')
        return out
    except Exception as e:
        print(f'[WARN] 沪深300 拉取失败（1分钟范围将只用自选）: {e}')
        return []


def get_1min_universe():
    codes = [c for c in HOLDINGS_1MIN if not is_bj(c)]
    for c in load_hushen300():
        if c not in codes:
            codes.append(c)
    return codes


def get_daily_universe():
    codes = set()
    if KLINE_DIR.exists():
        for p in KLINE_DIR.rglob('*.json'):
            stem = p.stem.lower()
            if re.fullmatch(r'(sh|sz)\d{6}', stem):
                codes.add(stem)
            elif p.parent.name.lower() in ('sh', 'sz') and re.fullmatch(r'\d{6}', stem):
                codes.add(p.parent.name.lower() + stem)
    codes = {c for c in codes if not is_bj(c)}
    if not codes:                        # 本地为空 → 默认列表
        codes = {c for c in HOLDINGS_1MIN if not is_bj(c)}
        codes.add('sh000001')            # 上证指数
    return sorted(codes)


# ---------------------------------------------------------------------------
# 工作单元
# ---------------------------------------------------------------------------

def daily_worker(code, datalen, full):
    time.sleep(0.15)                     # 轻微限速，礼貌抓取
    src, bars = fetch_daily(code, datalen)
    if not bars:
        return False
    path = KLINE_DIR / code[:2] / f'{code}.json'
    if full:
        merged = sorted(bars, key=lambda b: b[0])
    else:
        local = []
        if path.exists():
            try:
                local = json.loads(path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                local = []
        merged = merge_bars(local, bars)
    save_json(path, merged)
    return True


def min1_worker(code):
    time.sleep(0.15)
    src, bars = fetch_min1(code)
    if not bars:
        return False
    path = K1M_DIR / f'{code}.json'
    local = []
    if path.exists():
        try:
            local = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            local = []
    save_json(path, merge_bars(local, bars))
    return True


def run_threaded(codes, worker):
    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(worker, c): c for c in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                print(f'    {code} 异常: {type(e).__name__}: {e}')
                r = False
            if r:
                ok += 1
            else:
                failed.append(code)
    return ok, failed


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    now = beijing_now()
    sunday, trading = is_sunday(now), in_trading_hours(now)
    print(f'[INFO] 北京时间 {now:%Y-%m-%d %H:%M}  周日={sunday}  交易时段={trading}')

    # --- 日线 ---
    daily_codes = get_daily_universe()
    print(f'[INFO] 日线股票池: {len(daily_codes)} 只')
    if trading and not sunday:
        print('[INFO] 当前处于 A 股交易时段，跳过日线拉取（避免盘中数据不完整）')
    else:
        datalen, full = (1023, True) if sunday else (10, False)
        print(f'[INFO] 日线模式: {"周日全量重建 1023 根" if sunday else "平日增量 10 根合并"}')
        ok, failed = run_threaded(daily_codes, lambda c: daily_worker(c, datalen, full))
        print(f'[INFO] 日线完成: 成功 {ok}/{len(daily_codes)}，失败 {len(failed)}')
        if failed:
            print('[INFO] 日线失败清单(前20): ' + ', '.join(failed[:20]))

    # --- 1 分钟 ---
    m1_codes = get_1min_universe()
    print(f'[INFO] 1分钟股票池: {len(m1_codes)} 只（自选 + 沪深300，北交所已剔除）')
    ok, failed = run_threaded(m1_codes, min1_worker)
    print(f'[INFO] 1分钟完成: 成功 {ok}/{len(m1_codes)}，失败 {len(failed)}')
    if failed:
        print('[INFO] 1分钟失败清单(前20): ' + ', '.join(failed[:20]))
    print('[INFO] fetch_quotes.py 全部结束')
    return 0


if __name__ == '__main__':
    sys.exit(main())
