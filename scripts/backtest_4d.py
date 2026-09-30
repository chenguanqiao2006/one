#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四维循环看盘法 - 历史回测验证（终极完整版 + 真假账本优先）
=================================================
【无未来函数】
【买入标准】信号确认后 → 等回踩关键位 → 缩量企稳 → T+1开盘买入
【交易成本】0.35%
【位置分档】低位<30% / 中位30%-70% / 高位>70%
【趋势判断】上升趋势/下降趋势（个股20日均线）
【大盘环境】牛市/熊市（上证指数200日均线）
【真假量柱】优先查 truth_ledger/ 账本（按月分片）
【资料来源】股海明灯《量柱擒涨停》《量线捉涨停》黑马王子著
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

# ============================================================
# 【配置区】所有参数
# ============================================================
DATA_DIR = Path(__file__).parent.parent / "data" / "kline"
INDEX_PATH = DATA_DIR / "sh" / "sh000001.json"
MIN1_DIR = Path(__file__).parent.parent / "data" / "kline_1min"
LEDGER_DIR = Path(__file__).parent.parent / "data" / "analysis" / "truth_ledger"
HS300_PATH = Path(__file__).parent.parent / "data" / "hushen300.json"

MAX_STOCKS = 308
START_IDX = 500

HOLDINGS = [
    ("sh", "600584"), ("sz", "002156"), ("sh", "603283"), ("sz", "300394"),
    ("sh", "601138"), ("sh", "601231"), ("sz", "300476"), ("sh", "603516"),
]

# ============================================================
# 【统一配置接入】优先使用 config.py 的持仓股；config 缺失时用上面内置默认
# 为什么这么做：实现"改持仓只改 config.py 一处"，避免多个脚本不同步；
# 用 try/except 包裹，config 不在也能正常运行，不增加故障点。
# ============================================================
try:
    from config import HOLDINGS_TUPLE as _CFG_HOLDINGS
    HOLDINGS = list(_CFG_HOLDINGS)
    print("[config] 已从 config.py 加载持仓股")
except Exception as _cfg_err:
    print(f"[config] 未加载 config.py，使用脚本内置持仓（{_cfg_err}）")

HOLD_PERIODS = [5, 10, 20]

MAX_WAIT_DAYS = 10
TOUCH_TOLERANCE = 0.02
SHRINK_VOL_RATIO = 0.8
STAY_ABOVE = True

COMMISSION_RATE = 0.00125
STAMP_TAX = 0.001
TOTAL_COST = COMMISSION_RATE * 2 + STAMP_TAX

POSITION_LOOKBACK = 120
LOW_PCTL = 0.30
HIGH_PCTL = 0.70

LOW_VOL_PCTL = 0.30
LOW_PRICE_THRESHOLD = 20.0

TREND_MA_PERIOD = 20
INDEX_TREND_MA = 200

STEP_LOOKBACK = 20

ATR_ADAPTIVE = True
ATR_PERIOD = 14
YIN_BODY_ATR_MULT = 1.5
YIN_BODY_FALLBACK = 5.0
YIN_LOOKBACK = 60
CHANGYANG_ATR_MULT = 1.5
CHANGYANG_FALLBACK = 5.0

BEISHU_RATIO = 1.8
GAOLIANG_LOOKBACK = 20
PINGLIANG_TOLERANCE = 0.15
SMALL_BEISHU_MIN = 1.5
SMALL_BEISHU_MAX = 2.0

VOL_LOOKBACK = 20
VOL_PCTL_HIGH = 0.80
VOL_PCTL_LOW = 0.20
BASE_VOL_PCTL = 0.60
BEISHUO_EXTEND_PCTL = 0.80
BEISHUO_SHRINK_PCTL = 0.20
LONG_YIN_SHORT_VOL_PCTL = 0.30

LIMIT_UP_MAIN = 9.8
LIMIT_UP_GEM = 19.8

GENERAL_CONFIRM_DAYS = 3

SHORT_WINDOW = 20
CONFIRM_DAYS_SHORT = 2
PEAK_SIDE_SHORT = 2
VOL_PERCENTILE = 0.7

BODY_RATIO_THRESHOLD = 0.6
NIUGU_TOUCH_TOLERANCE = 0.01

# ============================================================
# 【B5修复】量价背离：价格创窗口新高/新低 + 量能低于近期均量 双确认
# 与 level4d_report_html.py 保持同一口径
# ============================================================
DIVERGENCE_LOOKBACK = 20
DIVERGENCE_VOL_SHRINK = 0.85

# ============================================================
# 【T5】二次三倍量（自媒体说法、权威性待考——纳入回测用数据验证）
# ============================================================
SANBEI_RATIO = 3.0
SANBEI_LOOKBACK = 60
SANBEI_MIN_GAP = 3


# ============================================================
# 【ATR计算】
# ============================================================
def calculate_atr(df, period=14):
    if len(df) < period + 1: return None, None
    high, low, close = df['high'].values, df['low'].values, df['close'].values
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    atr = np.mean(tr[-period:])
    atr_pct = atr / close[-1] * 100
    return atr, atr_pct


# ============================================================
# 【真假量柱判断】—— 账本优先（按月分片），1分钟数据兜底
# ============================================================
_real_money_cache = {}
_ledger_cache = None


def _load_ledger():
    """加载所有分片账本（data/analysis/truth_ledger/*.json）"""
    global _ledger_cache
    if _ledger_cache is not None: return _ledger_cache
    _ledger_cache = {}
    if not LEDGER_DIR.exists():
        print(f"  [账本] 目录不存在 {LEDGER_DIR}，将退回1分钟数据")
        return _ledger_cache
    files = sorted(LEDGER_DIR.glob("*.json"))
    if not files:
        print(f"  [账本] 目录为空，将退回1分钟数据")
        return _ledger_cache
    for f in files:
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                monthly = json.load(fp)
            for code, dates in monthly.items():
                if code not in _ledger_cache: _ledger_cache[code] = {}
                _ledger_cache[code].update(dates)
        except Exception as e:
            print(f"  [账本] 读取 {f.name} 失败: {e}")
    total = sum(len(v) for v in _ledger_cache.values())
    print(f"  [账本] 已加载 {len(_ledger_cache)} 只股票，共 {total} 条历史记录")
    return _ledger_cache


def is_real_money(market, code, trade_date):
    cache_key = f"{market}{code}_{trade_date}"
    if cache_key in _real_money_cache: return _real_money_cache[cache_key]

    ledger = _load_ledger()
    key = f"{market}{code}"
    if key in ledger and trade_date in ledger[key]:
        entry = ledger[key][trade_date]
        result = (entry.get("is_real"), entry.get("quant_pct", 0))
        _real_money_cache[cache_key] = result
        return result

    filepath = MIN1_DIR / f"{market}{code}.json"
    if not filepath.exists():
        # 无任何数据可判 → 第三态"未知"(None)，不再乐观当真金，避免系统性高估真金样本
        result = (None, None)
        _real_money_cache[cache_key] = result
        return result

    try:
        with open(filepath, 'r') as f: data = json.load(f)
        klines = data.get('klines', [])
        if len(klines) < 100:
            # 1分钟样本太少、无法可靠判真假 → 未知，不混入真金
            result = (None, None); _real_money_cache[cache_key] = result; return result
        day_klines = [k for k in klines if k[0].startswith(trade_date)]
        if len(day_klines) < 200:
            # 当天1分钟K线不足（停牌/数据残缺）→ 未知，不乐观当真金
            result = (None, None); _real_money_cache[cache_key] = result; return result
        volumes = [float(k[5]) for k in day_klines]
        closes = [float(k[2]) for k in day_klines]
        vol_mean, vol_std = np.mean(volumes), np.std(volumes)
        cv = vol_std / vol_mean if vol_mean > 0 else 1
        price_changes, vol_changes = np.diff(closes), np.diff(volumes)
        if len(price_changes) > 10:
            corr = np.corrcoef(price_changes, vol_changes)[0, 1]
            if np.isnan(corr): corr = 0.5
        else: corr = 0.5
        tail_ratio = sum(volumes[-30:]) / sum(volumes) if sum(volumes) > 0 else 0
        quant_count = (1 if cv < 0.5 else 0) + (1 if abs(corr) < 0.3 else 0) + (1 if tail_ratio > 0.3 else 0)
        cv_score = 0.7 if cv < 0.5 else (0.4 if cv < 1.0 else 0.1)
        corr_score = 0.6 if abs(corr) < 0.3 else (0.3 if abs(corr) < 0.5 else 0.1)
        tail_score = 0.7 if tail_ratio > 0.3 else (0.4 if tail_ratio > 0.2 else 0.1)
        quant_ratio = (cv_score + corr_score + tail_score) / 3 * 100
        result = (quant_count < 2, round(quant_ratio, 1))
        _real_money_cache[cache_key] = result
        return result
    except Exception:
        # 解析异常属于"判不出来"→ 未知，避免异常样本被默认真金
        result = (None, None); _real_money_cache[cache_key] = result; return result


def get_atr_threshold(atr_pct, mult, fallback):
    return atr_pct * mult if (ATR_ADAPTIVE and atr_pct is not None) else fallback


def is_gem_star(code):
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    return pure.startswith(('300', '301', '688'))


def get_vol_percentile(df, lookback=20):
    if len(df) < lookback: lookback = len(df)
    recent_vols = df.iloc[-lookback:]['volume']
    return (recent_vols < df.iloc[-1]['volume']).sum() / len(recent_vols)


def get_position_level(df, end_idx, lookback=POSITION_LOOKBACK):
    if end_idx < lookback: lookback = end_idx
    recent_df = df.iloc[end_idx-lookback+1:end_idx+1]
    pct = (recent_df['close'] < df.iloc[end_idx]['close']).sum() / len(recent_df)
    if pct < LOW_PCTL: return "低位"
    elif pct > HIGH_PCTL: return "高位"
    else: return "中位"


def get_stock_trend(df, end_idx, ma_period=TREND_MA_PERIOD):
    if end_idx < ma_period: return "未知"
    ma = df.iloc[end_idx-ma_period+1:end_idx+1]['close'].mean()
    return "上升趋势" if df.iloc[end_idx]['close'] > ma else "下降趋势"


def load_index_data():
    if not INDEX_PATH.exists(): return None
    with open(INDEX_PATH, 'r') as f: data = json.load(f)
    klines = data.get('klines', [])
    if not klines: return None
    ncols = len(klines[0])
    cols = ['date', 'open', 'close', 'high', 'low', 'volume'] if ncols == 6 else ['date', 'open', 'close', 'high', 'low', 'volume', 'amount']
    df = pd.DataFrame(klines).iloc[:, :ncols]
    df.columns = cols[:ncols]
    for col in ['open', 'close', 'high', 'low', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df.dropna(subset=['open', 'close', 'high', 'low', 'volume'])


def get_market_regime(index_df, target_date):
    if index_df is None: return "未知"
    date_mask = index_df['date'] <= target_date
    if date_mask.sum() < INDEX_TREND_MA: return "未知"
    end_idx = date_mask.sum() - 1
    ma = index_df.iloc[end_idx-INDEX_TREND_MA+1:end_idx+1]['close'].mean()
    return "牛市" if index_df.iloc[end_idx]['close'] > ma else "熊市"


def load_klines(market, code):
    for filepath in [DATA_DIR / market / f"{code}.json", DATA_DIR / market / f"{market}{code}.json", DATA_DIR / f"{market}{code}.json"]:
        if filepath.exists():
            with open(filepath, 'r') as f: data = json.load(f)
            klines = data.get('klines', [])
            if not klines: return None
            ncols = len(klines[0])
            cols = ['date', 'open', 'close', 'high', 'low', 'volume'] if ncols == 6 else ['date', 'open', 'close', 'high', 'low', 'volume', 'amount']
            df = pd.DataFrame(klines).iloc[:, :ncols]
            df.columns = cols[:ncols]
            for col in ['open', 'close', 'high', 'low', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            return df.dropna(subset=['open', 'close', 'high', 'low', 'volume'])
    return None


def scan_all_stocks():
    stocks = []
    for market, code in HOLDINGS:
        stocks.append((market, code))
    if HS300_PATH.exists():
        try:
            with open(HS300_PATH, 'r', encoding='utf-8') as f: data = json.load(f)
            for code in data.get('codes', []):
                market, pure_code = code[:2], code[2:]
                if not any(c == pure_code for _, c in stocks):
                    stocks.append((market, pure_code))
        except Exception: pass
    for sub in ['sh', 'sz']:
        d = DATA_DIR / sub
        if d.exists():
            for f in d.glob("*.json"):
                code = f.stem
                if code == "sh000001" or code.startswith("sz399"): continue
                pure_code = code[2:] if code.startswith(('sh', 'sz')) else code
                if not any(c == pure_code for _, c in stocks):
                    stocks.append((sub, pure_code))
    return stocks[:MAX_STOCKS]


def check_pullback_buy(df, confirm_idx, support_price, max_wait_days=MAX_WAIT_DAYS):
    if support_price is None or support_price <= 0: return None
    n = len(df)
    for i in range(confirm_idx + 1, min(confirm_idx + 1 + max_wait_days, n - 2)):
        today, yesterday = df.iloc[i], df.iloc[i - 1]
        if today['low'] <= support_price * (1 + TOUCH_TOLERANCE) and today['close'] >= support_price * (1 - TOUCH_TOLERANCE) and today['volume'] < yesterday['volume'] * SHRINK_VOL_RATIO:
            if i + 1 < n: return i + 1
    return None


# ============================================================
# 【信号判断函数】
# ============================================================
def check_bei_liang(df, i):
    if i < 2: return False, None
    if df.iloc[i-1]['volume'] > 0 and df.iloc[i]['volume'] / df.iloc[i-1]['volume'] >= BEISHU_RATIO:
        return True, df.iloc[i]['open']
    return False, None

def check_gao_liang(df, i):
    if i < GAOLIANG_LOOKBACK: return False, None
    if df.iloc[i]['volume'] == df.iloc[i-GAOLIANG_LOOKBACK:i+1]['volume'].max(): return True, df.iloc[i]['low']
    return False, None

def check_di_liang(df, i):
    if i < GAOLIANG_LOOKBACK: return False, None
    if df.iloc[i]['volume'] == df.iloc[i-GAOLIANG_LOOKBACK:i+1]['volume'].min(): return True, df.iloc[i]['low']
    return False, None

def check_ti_liang(df, i):
    if i < 3: return False, None
    v1, v2, v3 = df.iloc[i-2]['volume'], df.iloc[i-1]['volume'], df.iloc[i]['volume']
    return (True, df.iloc[i-2]['open']) if v1 < v2 < v3 else (False, None)

def check_suo_liang(df, i):
    if i < 3: return False, None
    v1, v2, v3 = df.iloc[i-2]['volume'], df.iloc[i-1]['volume'], df.iloc[i]['volume']
    return (True, df.iloc[i-2]['open']) if v1 > v2 > v3 else (False, None)

def check_ping_liang(df, i):
    if i < 6: return False, None
    avg5 = df.iloc[i-5:i]['volume'].mean()
    return (True, df.iloc[i]['close']) if (avg5 > 0 and abs(df.iloc[i]['volume'] - avg5) / avg5 <= PINGLIANG_TOLERANCE) else (False, None)

def check_xiao_bei_yang(df, i):
    if i < 2: return False, None
    today, yesterday = df.iloc[i], df.iloc[i-1]
    body_pct = (today['close'] - today['open']) / today['open'] * 100
    vr = today['volume'] / yesterday['volume'] if yesterday['volume'] > 0 else 0
    return (True, today['open']) if (0 < body_pct < 3 and SMALL_BEISHU_MIN <= vr < SMALL_BEISHU_MAX) else (False, None)

def check_yang_sheng_jin(df, i):
    if i < 2: return False, None
    today, yesterday = df.iloc[i], df.iloc[i-1]
    return (True, today['open']) if (today['close'] > today['open'] and today['volume'] > yesterday['volume'] and today['close'] > yesterday['close']) else (False, None)

def check_yin_sheng_chu(df, i):
    if i < 2: return False, None
    today, yesterday = df.iloc[i], df.iloc[i-1]
    return (True, today['close']) if (today['close'] < today['open'] and today['volume'] > yesterday['volume'] and today['close'] < yesterday['close']) else (False, None)

def check_chang_duan_yin(df, i, atr_pct):
    if i < 6: return False, None
    today = df.iloc[i]
    body_pct = (today['open'] - today['close']) / today['close'] * 100
    return (True, today['low']) if (body_pct > get_atr_threshold(atr_pct, CHANGYANG_ATR_MULT, CHANGYANG_FALLBACK) and get_vol_percentile(df.iloc[:i+1], VOL_LOOKBACK) < LONG_YIN_SHORT_VOL_PCTL) else (False, None)

def check_yang_bao_yin(df, i):
    if i < 2: return False, None
    today, yesterday = df.iloc[i], df.iloc[i-1]
    # B3修复：要求昨天是阴线（昨收<昨开），两根阳线不算阳包阴
    return (True, today['open']) if (yesterday['close'] < yesterday['open']
            and today['close'] > yesterday['open']
            and today['open'] < yesterday['close']
            and today['close'] > today['open']) else (False, None)

def check_ban_zhang(df, i, code):
    if i < 2: return False, None
    today, yesterday = df.iloc[i], df.iloc[i-1]
    pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
    limit = LIMIT_UP_GEM if is_gem_star(code) else LIMIT_UP_MAIN
    return (True, today['open']) if pct >= limit else (False, None)

def check_guo_zuofeng(df, i, peak_20):
    if peak_20 is None: return False, None
    return (True, peak_20) if df.iloc[i]['close'] > peak_20 else (False, None)

def check_jiayin_ciyang(df, i, atr_pct):
    if i < 6: return False, None
    today = df.iloc[i]
    recent_5 = df.iloc[i-5:i]
    cy = get_atr_threshold(atr_pct, CHANGYANG_ATR_MULT, CHANGYANG_FALLBACK)
    for j in range(len(recent_5)-2, 0, -1):
        row = recent_5.iloc[j]
        if (row['close'] - row['open']) / row['open'] * 100 < -cy:
            if today['close'] > today['open'] and (row['open'] - row['close']) > 0 and (today['close'] - row['close']) / (row['open'] - row['close']) > 0.5:
                return True, row['close']
            break
    return False, None

def check_changyang_aizhu(df, i, atr_pct):
    if i < 2: return False, None
    today, yesterday = df.iloc[i], df.iloc[i-1]
    if (today['close'] - yesterday['close']) / yesterday['close'] * 100 > get_atr_threshold(atr_pct, CHANGYANG_ATR_MULT, CHANGYANG_FALLBACK) and get_vol_percentile(df.iloc[:i+1], VOL_LOOKBACK) < 0.5:
        return True, today['open']
    return False, None

def check_beiliang_buchuan(df, i, code):
    if i < 60: return False, None
    recent_60 = df.iloc[i-59:i+1]
    for j in range(len(recent_60)-1, 5, -1):
        row, prev_row = recent_60.iloc[j], recent_60.iloc[j-1]
        if prev_row['volume'] > 0 and row['volume'] / prev_row['volume'] >= BEISHU_RATIO and row['close'] > row['open']:
            bottom = row['open']
            future = recent_60.iloc[j+1:]
            if len(future) > 0 and all(future['low'] >= bottom * (1 - NIUGU_TOUCH_TOLERANCE)):
                return True, bottom
            break
    return False, None

def check_gaoliang_bupo(df, i):
    if i < 60: return False, None
    # B1修复：切片重置索引（标签=位置），避免 idxmax 返回大标签、iloc 按位置越界
    recent_60 = df.iloc[i-59:i+1].reset_index(drop=True)
    mvi = recent_60['volume'].idxmax()
    bottom = recent_60.loc[mvi]['low']
    future = recent_60.iloc[mvi + 1:]
    return (True, bottom) if (len(future) > 0 and all(future['low'] >= bottom * (1 - NIUGU_TOUCH_TOLERANCE))) else (False, None)

def check_diliang_qun(df, i):
    if i < 100: return False, None
    recent_100 = df.iloc[i-99:i+1]
    vlow = recent_100['volume'].quantile(VOL_PCTL_LOW)
    if sum(recent_100['volume'] <= vlow) >= 5:
        return True, recent_100[recent_100['volume'] <= vlow].iloc[-1]['low']
    return False, None

def check_jiasheng_liangsou(df, i):
    if i < 3: return False, None
    r3 = df.iloc[i-2:i+1]
    pu = all(r3.iloc[j]['close'] > r3.iloc[j-1]['close'] for j in range(1, len(r3)))
    vd = all(r3.iloc[j]['volume'] < r3.iloc[j-1]['volume'] for j in range(1, len(r3)))
    return (True, r3.iloc[0]['open']) if (pu and vd) else (False, None)

def check_beishuo_shensuo(df, i):
    if i < 5: return False, None
    r5 = df.iloc[i-4:i+1]
    p = (r5['volume'].rank(pct=True)).values
    for j in range(1, len(p)):
        if p[j] >= BEISHUO_EXTEND_PCTL and p[j-1] <= BEISHUO_SHRINK_PCTL:
            return True, r5.iloc[j]['open']
    return False, None

def check_huicai_jingzhun(df, i, precise_price):
    if precise_price is None or i < 10: return False, None
    r10 = df.iloc[i-9:i+1]
    if any(abs(row['low'] - precise_price) / precise_price < TOUCH_TOLERANCE for _, row in r10.iterrows()):
        return True, precise_price
    return False, None


def identify_price_pattern(df, i, atr_pct):
    if i < 1: return "普通"
    today = df.iloc[i]
    body = abs(today['close'] - today['open'])
    total_range = today['high'] - today['low']
    if total_range == 0: return "十字星"
    br = body / total_range
    cy = get_atr_threshold(atr_pct, CHANGYANG_ATR_MULT, CHANGYANG_FALLBACK)
    if br > 0.7:
        if (today['close'] - today['open']) / today['open'] * 100 > cy: return "长阳"
        if (today['open'] - today['close']) / today['open'] * 100 > cy: return "长阴"
    if br < 0.1: return "十字星"
    return "普通"


def check_divergence(df, i):
    # B5修复：与报告同一口径——价格真创窗口新高/新低 + 量能低于近期均量
    if i < DIVERGENCE_LOOKBACK: return None, None
    win = df.iloc[i-DIVERGENCE_LOOKBACK+1:i+1]
    last = win.iloc[-1]
    prior_avg_vol = win.iloc[:-1]['volume'].mean()
    if prior_avg_vol <= 0: return None, None
    if last['close'] >= win['high'].max() and last['volume'] < prior_avg_vol * DIVERGENCE_VOL_SHRINK:
        return "顶背离", last['close']
    if last['close'] <= win['low'].min() and last['volume'] < prior_avg_vol * DIVERGENCE_VOL_SHRINK:
        return "底背离", last['close']
    return None, None


# ============================================================
# 【T5】二次三倍量（自媒体说法、权威性待考）
# 截至第 i 天出现第二次三倍量（间隔达标）才返回 True，用于回测验证胜率
# ============================================================
def check_second_sanbei(df, i):
    if i < SANBEI_MIN_GAP + 1: return False, None
    win = df.iloc[max(0, i-SANBEI_LOOKBACK):i+1]
    days = []
    for j in range(1, len(win)):
        prev_v = win.iloc[j-1]['volume']
        v = win.iloc[j]['volume']
        if prev_v > 0 and v / prev_v >= SANBEI_RATIO:
            days.append(j)
    if len(days) >= 2 and (days[-1] - days[-2]) >= SANBEI_MIN_GAP:
        return True, win.iloc[days[-1]]['open']
    return False, None


def identify_vol_pattern_bt(df, i):
    # 审计第7点修复：与报告 identify_vol_pattern 同优先级，返回"纯量柱名称"
    # （signal_checks 里的 name 带括号后缀如"低量柱（地量）"，直接拿去匹配意图会失败）
    if i < 1: return "普通量柱"
    for name, fn in [("倍量柱", check_bei_liang), ("高量柱", check_gao_liang),
                     ("低量柱", check_di_liang), ("梯量柱", check_ti_liang),
                     ("缩量柱", check_suo_liang), ("平量柱", check_ping_liang)]:
        try:
            ok, _ = fn(df, i)
            if ok: return name
        except Exception:
            pass
    return "普通量柱"


def check_main_intent(position, stock_trend, vol_pattern, pillar_type, price_pattern,
                      is_real_sig=None, has_diliang=False):
    # 与报告 get_main_force_intent 口径对齐：is_real_sig True=真金/False=量化/None=未知
    intents = []
    real = (is_real_sig is True)
    fake = (is_real_sig is False)
    if position == "低位" and vol_pattern == "倍量柱" and real and stock_trend == "上升趋势": intents.append(("建仓中", None))
    # B7对齐：洗盘=中位上升中王牌柱后缩量/平量回踩，而非旧的"黄金柱+上升"
    if position == "中位" and stock_trend == "上升趋势" and pillar_type in ["黄金柱", "将军柱"] and vol_pattern in ["缩量柱", "平量柱", "低量柱"]: intents.append(("洗盘", None))
    if position == "中位" and pillar_type == "元帅柱" and vol_pattern == "倍量柱": intents.append(("拉升", None))
    if position == "高位" and vol_pattern == "倍量柱" and fake: intents.append(("出货", None))
    if position == "高位" and price_pattern == "长阴" and vol_pattern in ["倍量柱", "高量柱"]: intents.append(("出逃", None))
    if position == "低位" and has_diliang and real: intents.append(("吸筹", None))
    if position in ["中位", "高位"] and vol_pattern == "倍量柱" and fake: intents.append(("诱多", None))
    return intents


def find_fenggu_at(df, end_idx, lookback_days, peak_side, confirm_days, vol_percentile):
    if end_idx < lookback_days + confirm_days + peak_side: return None, None
    recent_df = df.iloc[end_idx-lookback_days+1:end_idx+1]
    vt = recent_df['volume'].quantile(vol_percentile)
    peaks, valleys = [], []
    for i in range(peak_side, len(recent_df) - max(peak_side, confirm_days)):
        row = recent_df.iloc[i]
        window = recent_df.iloc[i-peak_side:i+peak_side+1]
        if row['high'] == window['high'].max() and row['volume'] >= vt:
            if all(recent_df.iloc[i+1:i+1+confirm_days]['close'] < row['high']): peaks.append({'price': row['high']})
        if row['low'] == window['low'].min() and row['volume'] >= vt:
            if all(recent_df.iloc[i+1:i+1+confirm_days]['close'] > row['low']): valleys.append({'price': row['low']})
    return (peaks[-1]['price'] if peaks else None, valleys[-1]['price'] if valleys else None)


def find_precise_at(df, end_idx, lookback_days=120, min_points=3, price_tolerance=1.0):
    if end_idx < lookback_days: return None
    recent_df = df.iloc[end_idx-lookback_days+1:end_idx+1]
    prices = recent_df['close'].values
    if len(prices) < min_points: return None
    clusters, used = {}, set()
    for i in range(len(prices)):
        if i in used: continue
        cluster = [i]
        for j in range(len(prices)):
            if i == j or j in used: continue
            if abs(prices[i] - prices[j]) / prices[i] * 100 <= price_tolerance: cluster.append(j)
        if len(cluster) >= min_points:
            clusters[len(cluster)] = np.mean([prices[idx] for idx in cluster])
            for idx in cluster: used.add(idx)
    return clusters[max(clusters.keys())] if clusters else None


def find_big_yin_top_at(df, end_idx, lookback_days, yin_body_pct):
    if end_idx < lookback_days: return None, None
    recent_df = df.iloc[end_idx-lookback_days+1:end_idx+1]
    for i in range(len(recent_df)-1, -1, -1):
        row = recent_df.iloc[i]
        if row['close'] < row['open'] and (row['open'] - row['close']) / row['close'] * 100 >= yin_body_pct:
            return row['open'], row['date']
    return None, None


def find_pillars_at(df, end_idx, lookback_days=60):
    if end_idx < lookback_days + GENERAL_CONFIRM_DAYS + 20: return "无", None
    recent_df = df.iloc[end_idx-lookback_days+1:end_idx+1]
    marshals, goldens, generals = [], [], []
    for i in range(len(recent_df) - GENERAL_CONFIRM_DAYS - 1, 5, -1):
        row = recent_df.iloc[i]
        if row['close'] <= row['open'] or i < 20: continue
        vol_window = recent_df.iloc[max(0, i-20):i]['volume']
        if (vol_window < row['volume']).sum() / len(vol_window) < BASE_VOL_PCTL: continue
        future = recent_df.iloc[i+1:i+1+GENERAL_CONFIRM_DAYS]
        if len(future) < GENERAL_CONFIRM_DAYS: continue
        if future['close'].mean() < row['open'] or future.iloc[-1]['volume'] >= row['volume']: continue
        is_golden = future['close'].mean() >= row['close']
        is_gap_up = row['open'] > recent_df.iloc[i-1]['high'] if i > 0 else False
        if is_golden and is_gap_up: marshals.append(row['low'])
        elif is_golden: goldens.append(row['low'])
        else: generals.append(row['low'])
    if marshals: return "元帅柱", marshals[0]
    if goldens: return "黄金柱", goldens[0]
    if generals: return "将军柱", generals[0]
    return "无", None


# ============================================================
# 【主函数】
# ============================================================
def main():
    print("=" * 70)
    print("四维循环看盘法 - 历史回测验证（终极完整版 + 真假账本优先）")
    print("=" * 70)
    print(f"\n样本：沪深300 + 持仓股，共 {MAX_STOCKS} 只")
    print(f"回测起始日：第 {START_IDX} 天（只跑近2年）")
    print(f"交易成本：{TOTAL_COST*100:.2f}%")
    print(f"真假量柱：优先查账本 truth_ledger/*.json")

    _load_ledger()
    index_df = load_index_data()
    all_stocks = scan_all_stocks()
    print(f"\n自动扫描到股票数：{len(all_stocks)}只\n")

    all_trades = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {h: [] for h in HOLD_PERIODS}))))

    for idx, (market, code) in enumerate(all_stocks):
        print(f"  回测中 {idx+1}/{len(all_stocks)}: {market}{code} ...")
        df = load_klines(market, code)
        if df is None: continue
        full_code = f"{market}{code}"
        n = len(df)

        for i in range(START_IDX, n - 60 - MAX_WAIT_DAYS - 2):
            atr_v, atr_pct = calculate_atr(df.iloc[:i+1], ATR_PERIOD)
            ybt = get_atr_threshold(atr_pct, YIN_BODY_ATR_MULT, YIN_BODY_FALLBACK)
            peak_20, valley_20 = find_fenggu_at(df, i, SHORT_WINDOW, PEAK_SIDE_SHORT, CONFIRM_DAYS_SHORT, VOL_PERCENTILE)
            precise_price = find_precise_at(df, i)
            big_yin_top, _ = find_big_yin_top_at(df, i, YIN_LOOKBACK, ybt)
            pillar_type, golden_line = find_pillars_at(df, i)
            position = get_position_level(df, i)
            stock_trend = get_stock_trend(df, i)
            market_regime = get_market_regime(index_df, df.iloc[i]['date'])

            signals_today = []
            # 审计第7点修复：直接用统一识别得到纯量柱名称（不再靠信号name兜底、避免括号后缀）
            vol_pattern = identify_vol_pattern_bt(df, i)

            # 【关键修复】：用 lambda 包装所有信号检测
            signal_checks = [
                ("倍量柱", lambda: check_bei_liang(df, i)),
                ("高量柱", lambda: check_gao_liang(df, i)),
                ("低量柱（地量）", lambda: check_di_liang(df, i)),
                ("梯量柱", lambda: check_ti_liang(df, i)),
                ("缩量柱", lambda: check_suo_liang(df, i)),
                ("平量柱", lambda: check_ping_liang(df, i)),
                ("小倍阳（矮将军）", lambda: check_xiao_bei_yang(df, i)),
                ("阳胜进", lambda: check_yang_sheng_jin(df, i)),
                ("阴胜出", lambda: check_yin_sheng_chu(df, i)),
                ("长阴短柱", lambda: check_chang_duan_yin(df, i, atr_pct)),
                ("阳包阴", lambda: check_yang_bao_yin(df, i)),
                ("涨停板", lambda: check_ban_zhang(df, i, full_code)),
                ("过左峰", lambda: check_guo_zuofeng(df, i, peak_20)),
                ("极阴次阳", lambda: check_jiayin_ciyang(df, i, atr_pct)),
                ("长阳矮柱", lambda: check_changyang_aizhu(df, i, atr_pct)),
                ("倍量不穿", lambda: check_beiliang_buchuan(df, i, full_code)),
                ("高量不破", lambda: check_gaoliang_bupo(df, i)),
                ("地量群", lambda: check_diliang_qun(df, i)),
                ("价升量缩", lambda: check_jiasheng_liangsou(df, i)),
                ("倍量伸缩", lambda: check_beishuo_shensuo(df, i)),
                ("回踩精准线", lambda: check_huicai_jingzhun(df, i, precise_price)),
                ("二次三倍量(待验证)", lambda: check_second_sanbei(df, i)),
            ]
            for name, check in signal_checks:
                try:
                    ok, support = check()
                    if ok:
                        signals_today.append((name, support))
                        if vol_pattern == "无": vol_pattern = name
                except Exception: pass

            if pillar_type != "无": signals_today.append((pillar_type, golden_line))

            if valley_20 and abs(df.iloc[i]['low'] - valley_20) / valley_20 < TOUCH_TOLERANCE and df.iloc[i]['close'] > valley_20:
                signals_today.append(("回踩谷底线不破", valley_20))
            if big_yin_top and df.iloc[i]['close'] > big_yin_top:
                signals_today.append(("突破大阴实顶", big_yin_top))

            price_pattern = identify_price_pattern(df, i, atr_pct)
            if price_pattern != "普通": signals_today.append((price_pattern, df.iloc[i]['close']))
            div_name, div_support = check_divergence(df, i)
            if div_name: signals_today.append((div_name, div_support))

            # 意图判断需真假量与地量群，先取（is_real_money 带缓存）
            is_real_sig, _ = is_real_money(market, code, df.iloc[i]['date'])
            has_diliang, _ = check_diliang_qun(df, i)
            for intent_name, intent_support in check_main_intent(position, stock_trend, vol_pattern, pillar_type, price_pattern, is_real_sig, has_diliang):
                signals_today.append((intent_name, intent_support if intent_support else df.iloc[i]['close']))

            for signal_name, support_price in signals_today:
                if signal_name == "阴胜出": continue
                buy_idx = check_pullback_buy(df, i, support_price)
                if buy_idx is None: continue
                buy_today, buy_yesterday = df.iloc[buy_idx], df.iloc[buy_idx - 1]
                if buy_today['open'] == buy_today['close'] and (buy_today['close'] - buy_yesterday['close']) / buy_yesterday['close'] * 100 > 9.5: continue
                bp = df.iloc[buy_idx]['open']
                # 复用上方意图判断已取的 is_real_sig（同一信号日），不再重复调用
                for hold_days in HOLD_PERIODS:
                    sell_idx = buy_idx + hold_days
                    if sell_idx >= n: continue
                    sp = df.iloc[sell_idx]['close']
                    if bp <= 0: continue
                    ret = (sp - bp) / bp * 100 - TOTAL_COST * 100
                    all_trades[signal_name][position][stock_trend][market_regime][hold_days].append((ret, is_real_sig))

    print("\n" + "=" * 70)
    print("回测完成，生成 winrate.json ...")
    print("=" * 70)

    WINRATE_HOLD_DAYS = 20
    winrate_data = {}
    positions = ["低位", "中位", "高位"]
    stock_trends = ["上升趋势", "下降趋势"]
    market_regimes = ["牛市", "熊市", "未知"]

    for signal_name in all_trades:
        for pos in positions:
            if pos not in winrate_data: winrate_data[pos] = {}
            for trend in stock_trends:
                if trend not in winrate_data[pos]: winrate_data[pos][trend] = {}
                all_list = []
                for regime in market_regimes: all_list.extend(all_trades[signal_name][pos][trend][regime][WINRATE_HOLD_DAYS])
                all_returns = [t[0] for t in all_list]
                if len(all_returns) >= 5:
                    winrate_data[pos][trend][signal_name] = {
                        "win_rate": round(sum(1 for r in all_returns if r > 0) / len(all_returns) * 100, 1),
                        "avg_ret": round(float(np.mean(all_returns)), 2)
                    }
                real_returns = [t[0] for t in all_list if t[1] is True]
                if len(real_returns) >= 5:
                    winrate_data[pos][trend][f"{signal_name}_真金"] = {
                        "win_rate": round(sum(1 for r in real_returns if r > 0) / len(real_returns) * 100, 1),
                        "avg_ret": round(float(np.mean(real_returns)), 2)
                    }
                quant_returns = [t[0] for t in all_list if t[1] is False]
                if len(quant_returns) >= 5:
                    winrate_data[pos][trend][f"{signal_name}_量化"] = {
                        "win_rate": round(sum(1 for r in quant_returns if r > 0) / len(quant_returns) * 100, 1),
                        "avg_ret": round(float(np.mean(quant_returns)), 2)
                    }
                # 新增第三态细分：数据缺失/异常的"未知"样本单独统计，不混入真金也不混入量化
                unknown_returns = [t[0] for t in all_list if t[1] is None]
                if len(unknown_returns) >= 5:
                    winrate_data[pos][trend][f"{signal_name}_未知"] = {
                        "win_rate": round(sum(1 for r in unknown_returns if r > 0) / len(unknown_returns) * 100, 1),
                        "avg_ret": round(float(np.mean(unknown_returns)), 2)
                    }

    output_dir = Path(__file__).parent.parent / "data" / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    winrate_path = output_dir / "winrate.json"
    with open(winrate_path, 'w', encoding='utf-8') as f:
        json.dump(winrate_data, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已生成胜率文件：{winrate_path}")


if __name__ == "__main__":
    main()