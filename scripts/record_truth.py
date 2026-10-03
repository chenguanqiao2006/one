#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
record_truth.py — 真假量柱账本分析器 v2.2
==========================================

职责链：
    读取 data/kline_1min/ 下的 1 分钟 K 线
      → 四指标投票（CV / 量价相关性 / 尾盘占比 / 分时均匀度）
      → 判定「真金白银 / 疑似量化 / 量化对倒」（涨跌停日豁免）
      → 写入按月分片账本 data/analysis/truth_ledger/YYYY-MM.json
      → 验证落盘成功后删除原始 1 分钟数据（先删后提交，仓库不膨胀）

v2.2 相对 v2.1 的变更：
    [新增] filter_session 兜底过滤 —— 每日 K 线只保留连续竞价时段
           （09:30-11:30 / 13:00-15:00），并裁掉尾部连续零量行，
           防止盘后"幽灵K线"稀释尾盘占比指标。

用法：
    python scripts/record_truth.py               # 正常运行
    python scripts/record_truth.py --dry-run     # 只分析、不写账本、不删源文件
    python scripts/record_truth.py --no-delete   # 写账本、但保留源文件

纯标准库，无第三方依赖。
"""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与常量配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
KLINE_1MIN_DIR = BASE_DIR / 'data' / 'kline_1min'
KLINE_DAILY_DIR = BASE_DIR / 'data' / 'kline'
LEDGER_DIR = BASE_DIR / 'data' / 'analysis' / 'truth_ledger'

CST = timezone(timedelta(hours=8))
MIN_FULL_DAY_BARS = 230

# --- 判定阈值（集中一处，调参只改这里） ---
THRESH = {
    'cv':        0.5,
    'corr':      0.3,
    'tail':      0.3,
    'flatness':  0.25,
}

SESSION_SPLITS = [
    ('开盘30分',   0,  30),
    ('上午前段',  30,  90),
    ('午前',      90, 120),
    ('午后',     120, 180),
    ('尾盘前',   180, 210),
    ('尾盘30分', 210, 240),
]


# ---------------------------------------------------------------------------
# 北交所过滤（兜底；主过滤在 fetch_quotes.py）
# ---------------------------------------------------------------------------

def is_bj(code: str) -> bool:
    c = (code or '').lower().replace('.bj', '').strip()
    if not c:
        return False
    if c.startswith('bj'):
        return True
    digits = c[2:] if (len(c) > 2 and c[:2] in ('sh', 'sz')) else c
    return digits.startswith(('920', '83', '87', '43'))


# ---------------------------------------------------------------------------
# 涨跌停规则（无北交所分支）
# ---------------------------------------------------------------------------

def limit_pct_for(code: str) -> float:
    if code.startswith(('sz30', 'sh68')):
        return 0.198       # 创业板 / 科创板 ±20%
    return 0.098           # 主板 ±10%


def min_bars_for(code: str) -> int:
    return MIN_FULL_DAY_BARS


# ---------------------------------------------------------------------------
# 数据解析层：兼容字符串 / 数组 / 字典三种 K 线格式
# ---------------------------------------------------------------------------

def extract_bars_payload(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ('data', 'bars', 'klines', 'kline'):
            v = obj.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ('klines', 'data', 'bars'):
                    v2 = v.get(k2)
                    if isinstance(v2, list):
                        return v2
    return None


def _looks_like_time(x) -> bool:
    if isinstance(x, (int, float)):
        return x > 10_000
    s = str(x).strip()
    if re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', s):
        return True
    if s.isdigit() and len(s) >= 8:
        return True
    return False


def normalize_bar(item):
    try:
        if isinstance(item, dict):
            g = lambda *ks: next((item[k] for k in ks if item.get(k) is not None), None)
            return {
                'time':   g('time', 't', 'datetime', 'dt', 'date'),
                'open':   float(g('open', 'o', 0)),
                'high':   float(g('high', 'h', 0)),
                'low':    float(g('low', 'l', 0)),
                'close':  float(g('close', 'c', 0)),
                'volume': float(g('volume', 'vol', 'v', 0)),
            }
        if isinstance(item, str):
            parts = [p.strip() for p in item.split(',')]
            if len(parts) >= 6 and _looks_like_time(parts[0]):
                return {
                    'time':   parts[0],
                    'open':   float(parts[1]),
                    'high':   float(parts[2]),
                    'low':    float(parts[3]),
                    'close':  float(parts[4]),
                    'volume': float(parts[5]),
                }
            return None
        if isinstance(item, (list, tuple)) and len(item) >= 6:
            if _looks_like_time(item[0]):
                return {
                    'time':   item[0],
                    'open':   float(item[1]),
                    'high':   float(item[2]),
                    'low':    float(item[3]),
                    'close':  float(item[4]),
                    'volume': float(item[5]),
                }
            return None
    except (ValueError, TypeError):
        return None
    return None


def extract_date(t):
    if t is None:
        return None
    if isinstance(t, (int, float)):
        ts = t / 1000 if t > 1e12 else t
        try:
            return datetime.fromtimestamp(ts, tz=CST).strftime('%Y-%m-%d')
        except (OverflowError, OSError, ValueError):
            return None
    s = str(t).strip()
    if s.isdigit() and len(s) >= 8:
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    return None


# ---------------------------------------------------------------------------
# [v2.2 新增] 幽灵K线兜底过滤
# ---------------------------------------------------------------------------

def filter_session(bars):
    """只保留连续竞价时段（09:30-11:30 / 13:00-15:00），
    并裁掉尾部连续零量行（最多 30 根，防止误伤真实缩量尾盘）。"""
    def in_session(t):
        m = re.search(r'(\d{1,2}):(\d{2})', str(t))
        if not m:
            return True                 # 无时间信息的行保守保留
        v = int(m.group(1)) * 60 + int(m.group(2))
        return (570 <= v <= 690) or (780 <= v <= 900)

    def _vol(b):
        try:
            return float(b.get('volume') or 0)
        except (TypeError, ValueError):
            return 0.0

    kept = [b for b in bars if in_session(b['time'])]
    cut = 0
    while kept and cut < 30 and _vol(kept[-1]) <= 0:
        kept.pop()
        cut += 1
    return kept


def load_and_group_days(path: Path):
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f'[WARN] {path.name} JSON 解析失败: {e}')
        return None
    payload = extract_bars_payload(obj)
    if not payload:
        print(f'[WARN] {path.name} 未识别到 K 线数据，跳过')
        return None
    bars = [b for b in (normalize_bar(x) for x in payload) if b]
    if not bars:
        print(f'[WARN] {path.name} K 线全部无法解析，跳过')
        return None
    bars.sort(key=lambda b: str(b['time']))
    days = {}
    for b in bars:
        d = extract_date(b['time'])
        if d:
            days.setdefault(d, []).append(b)
    if not days:
        print(f'[WARN] {path.name} 时间字段无日期信息，跳过')
    else:
        for d in days:
            days[d] = filter_session(days[d])   # [v2.2] 每天先过滤再分析
    return days


def resolve_code(path: Path) -> str:
    stem = path.stem
    if stem.lower().startswith(('sh', 'sz', 'bj')):
        return stem.lower()
    parent = path.parent.name.lower()
    if parent in ('sh', 'sz', 'bj'):
        return parent + stem
    return stem.lower()


# ---------------------------------------------------------------------------
# prev_close 加载（涨跌停检测用）
# ---------------------------------------------------------------------------

def load_prev_close(code: str, target_date: str, _cache={}):
    if code in _cache:
        daily = _cache[code]
    else:
        daily = None
        for cand in (KLINE_DAILY_DIR / f'{code[:2]}' / f'{code}.json',
                     KLINE_DAILY_DIR / f'{code}.json'):
            if cand.exists():
                try:
                    obj = json.loads(cand.read_text(encoding='utf-8'))
                    payload = extract_bars_payload(obj) or []
                    daily = []
                    for item in payload:
                        if isinstance(item, str):
                            p = [x.strip() for x in item.split(',')]
                        elif isinstance(item, (list, tuple)):
                            p = item
                        else:
                            continue
                        d = extract_date(p[0]) if p else None
                        if d and len(p) >= 5:
                            try:
                                daily.append((d, float(p[4])))
                            except (ValueError, TypeError):
                                pass
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                break
        _cache[code] = daily
    if not daily:
        return None
    before = [c for d, c in daily if d < target_date]
    return before[-1] if before else None


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def safe_corr(xs, ys):
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    r = cov / math.sqrt(vx * vy)
    return max(-1.0, min(1.0, r))


def session_volume_profile(volumes):
    total = sum(volumes)
    if total <= 0:
        return None
    n = len(volumes)
    return [sum(volumes[min(a, n):min(b, n)]) / total for _, a, b in SESSION_SPLITS]


def flatness_score(profile):
    if not profile:
        return None
    u = 1.0 / len(profile)
    return sum(abs(p - u) for p in profile)


def build_feature_snapshot(volumes):
    """240 根 1 分钟 → 24 个 10 分钟桶占比（4 位小数，体积友好）。"""
    n = 24
    buckets = [0.0] * n
    for i, v in enumerate(volumes):
        buckets[min(i // 10, n - 1)] += v
    total = sum(buckets)
    if total <= 0:
        return None
    return [round(b / total, 4) for b in buckets]


# ---------------------------------------------------------------------------
# 涨跌停 / 一字板检测
# ---------------------------------------------------------------------------

def detect_limit_status(bars, prev_close, limit_pct):
    if not bars:
        return None
    high = max(b['high'] for b in bars)
    low = min(b['low'] for b in bars)
    if high == low:                          # 一字板
        return 'one_word'
    base = prev_close if (prev_close and prev_close > 0) else bars[0]['open']
    if not base or base <= 0:
        return None
    close = bars[-1]['close']
    eps = 0.001
    limit_up = round(base * (1 + limit_pct), 2)
    limit_dn = round(base * (1 - limit_pct), 2)
    if close >= limit_up - eps:
        return 'limit_up'
    if close <= limit_dn + eps:
        return 'limit_down'
    return None


# ---------------------------------------------------------------------------
# 判定层
# ---------------------------------------------------------------------------

def compute_quant_pct(cv, corr, tail_ratio, flatness):
    contribs = [70 if cv < 0.5 else (40 if cv < 1.0 else 10)]
    if corr is not None:
        a = abs(corr)
        contribs.append(60 if a < 0.2 else (30 if a < 0.5 else 10))
    if tail_ratio is not None:
        contribs.append(70 if tail_ratio > 0.4 else (40 if tail_ratio > 0.2 else 10))
    if flatness is not None:
        contribs.append(60 if flatness < 0.15 else (35 if flatness < 0.3 else 10))
    return round(sum(contribs) / len(contribs), 1)


def analyze_day(bars, code, prev_close):
    volumes = [b['volume'] for b in bars]
    total_vol = sum(volumes)
    if total_vol <= 0 or len(bars) < 2:
        return None

    limit_status = detect_limit_status(bars, prev_close, limit_pct_for(code))
    if limit_status:
        # 豁免条目不参与任何统计，快照是死重量 → 不存
        return {
            'is_real': None,
            'verdict': f'涨跌停豁免({limit_status})',
            'quant_pct': None,
            'limit_status': limit_status,
        }

    closes = [b['close'] for b in bars]

    vol_mean = total_vol / len(volumes)
    vol_std = math.sqrt(sum((v - vol_mean) ** 2 for v in volumes) / len(volumes))
    cv = vol_std / vol_mean

    corr = safe_corr(
        [closes[i] - closes[i - 1] for i in range(1, len(closes))],
        [volumes[i] - volumes[i - 1] for i in range(1, len(volumes))],
    )

    tail_ratio = sum(volumes[-30:]) / total_vol
    flatness = flatness_score(session_volume_profile(volumes))

    hits = 0
    if cv < THRESH['cv']:
        hits += 1
    if corr is not None and abs(corr) < THRESH['corr']:
        hits += 1
    if tail_ratio > THRESH['tail']:
        hits += 1
    if flatness is not None and flatness < THRESH['flatness']:
        hits += 1

    if hits == 0:
        is_real, verdict = True, '真金白银'
    elif hits == 1:
        is_real, verdict = None, '疑似量化'
    else:
        is_real, verdict = False, '量化对倒'

    return {
        'is_real': is_real,
        'verdict': verdict,
        'quant_pct': compute_quant_pct(cv, corr, tail_ratio, flatness),
        'cv': round(cv, 4),
        'corr': round(corr, 4) if corr is not None else None,
        'tail_ratio': round(tail_ratio, 4),
        'flatness': round(flatness, 4) if flatness is not None else None,
        'limit_status': None,
        'vprofile_24': build_feature_snapshot(volumes),
    }


# ---------------------------------------------------------------------------
# 账本读写层
# ---------------------------------------------------------------------------

class LedgerCache:
    def __init__(self, ledger_dir: Path):
        self.dir = ledger_dir
        self._cache = {}
        self.dirty = set()

    def path_for(self, month: str) -> Path:
        return self.dir / f'{month}.json'

    def load(self, month: str) -> dict:
        if month not in self._cache:
            p = self.path_for(month)
            if p.exists():
                try:
                    self._cache[month] = json.loads(p.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    print(f'[WARN] 账本 {month}.json 损坏，将重建（请人工核查备份）')
                    self._cache[month] = {}
            else:
                self._cache[month] = {}
        return self._cache[month]

    def has(self, month, code, date):
        return date in self.load(month).get(code, {})

    def put(self, month, code, date, entry):
        self.load(month).setdefault(code, {})[date] = entry
        self.dirty.add(month)

    def flush(self):
        # 紧凑 JSON：仓库体积友好（代价是网页上不再逐行可读）
        self.dir.mkdir(parents=True, exist_ok=True)
        for month in sorted(self.dirty):
            p = self.path_for(month)
            tmp = p.with_suffix('.tmp')
            tmp.write_text(
                json.dumps(self._cache[month], ensure_ascii=False,
                           separators=(',', ':')),
                encoding='utf-8',
            )
            tmp.replace(p)
            print(f'[INFO] 账本落盘: {p.name}')

    def verify(self, entries) -> bool:
        for month, code, date in entries:
            try:
                data = json.loads(self.path_for(month).read_text(encoding='utf-8'))
            except Exception:
                return False
            if date not in data.get(code, {}):
                return False
        return True


# ---------------------------------------------------------------------------
# 汇总统计
# ---------------------------------------------------------------------------

def print_summary(stats: dict):
    n = stats['processed']
    if n == 0:
        print('\n本日无新增账目（可能全部已存在或无新数据）。')
        return
    line = '=' * 52
    print(f'\n{line}')
    print(f"本次入账: {n} 条 (股票, 日期)")
    for label, key in (('真金白银', 'real'), ('疑似量化', 'suspect'), ('量化对倒', 'fake')):
        v = stats[key]
        print(f'  {label}  : {v:5d}  ({v / n:6.1%})')
    v = stats['limit_exempt']
    print(f'  涨跌停豁免: {v:5d}  ({v / n:6.1%})')
    print(f'  跳过: 北交所 {stats["skipped_bj"]} | 不完整 {stats["skipped_incomplete"]} | '
          f'已存在 {stats["skipped_exists"]} | 无法解析 {stats["skipped_bad"]}')
    print(f'  文件: 删除 {stats["files_deleted"]} | 保留 {stats["files_kept"]}')
    for name, key in (('cv', 'cv'), ('corr', 'corr'),
                      ('tail_ratio', 'tail'), ('flatness', 'flat')):
        cnt = stats[f'{key}_n']
        if cnt:
            print(f'  指标均值: {name}={stats[f"{key}_sum"] / cnt:.3f}')
    print(f'{line}\n')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def process_file(path: Path, ledger: LedgerCache, stats: dict, dry_run: bool) -> list:
    code = resolve_code(path)
    if is_bj(code):
        stats['skipped_bj'] += 1
        return []

    days = load_and_group_days(path)
    if not days:
        stats['skipped_bad'] += 1
        return []

    entries = []
    for date in sorted(days):
        month = date[:7]
        bars = days[date]

        if ledger.has(month, code, date):
            stats['skipped_exists'] += 1
            continue
        if len(bars) < min_bars_for(code):
            stats['skipped_incomplete'] += 1
            continue

        prev_close = load_prev_close(code, date)
        entry = analyze_day(bars, code, prev_close)
        if entry is None:
            stats['skipped_bad'] += 1
            continue

        if not dry_run:
            ledger.put(month, code, date, entry)
        entries.append((month, code, date))
        stats['processed'] += 1

        if entry['is_real'] is True:
            stats['real'] += 1
        elif entry['is_real'] is False:
            stats['fake'] += 1
        elif entry.get('limit_status'):
            stats['limit_exempt'] += 1
        else:
            stats['suspect'] += 1

        for k, v in (('cv', entry.get('cv')), ('corr', entry.get('corr')),
                     ('tail', entry.get('tail_ratio')), ('flat', entry.get('flatness'))):
            if v is not None:
                stats[f'{k}_sum'] += v
                stats[f'{k}_n'] += 1

    return entries


def main():
    ap = argparse.ArgumentParser(description='真假量柱账本 v2.2')
    ap.add_argument('--dry-run', action='store_true', help='只分析，不写账本、不删源文件')
    ap.add_argument('--no-delete', action='store_true', help='写账本，但保留源文件')
    args = ap.parse_args()

    stats = {k: 0 for k in (
        'processed', 'real', 'suspect', 'fake', 'limit_exempt',
        'skipped_bj', 'skipped_incomplete', 'skipped_exists', 'skipped_bad',
        'files_deleted', 'files_kept',
        'cv_sum', 'cv_n', 'corr_sum', 'corr_n', 'tail_sum', 'tail_n', 'flat_sum', 'flat_n',
    )}

    if not KLINE_1MIN_DIR.exists():
        print(f'[INFO] 目录不存在，无数据可处理: {KLINE_1MIN_DIR}')
        return 0

    files = sorted(KLINE_1MIN_DIR.rglob('*.json'))
    print(f'[INFO] 扫描到 {len(files)} 个源文件，模式: '
          f'{"DRY-RUN" if args.dry_run else ("不删除源文件" if args.no_delete else "常规")}')

    ledger = LedgerCache(LEDGER_DIR)

    for path in files:
        entries = process_file(path, ledger, stats, args.dry_run)
        # --dry-run 与 --no-delete 都不进入删除分支
        if not entries or args.dry_run or args.no_delete:
            continue
        ledger.flush()
        if ledger.verify(entries):
            path.unlink()
            stats['files_deleted'] += 1
        else:
            print(f'[安全拦截] {path.name} 账本验证未通过，保留源文件')
            stats['files_kept'] += 1

    ledger.flush()
    print_summary(stats)
    return 0


if __name__ == '__main__':
    sys.exit(main())
