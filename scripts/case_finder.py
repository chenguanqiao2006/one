#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
case_finder.py — 量学现代案例扫描器（案例册生成器）
====================================================
目的：回应「四册笔记案例都是十多年前的」——用本仓库 data/kline/ 的
     全量日线自动扫描四册笔记的核心形态，生成《量学现代案例集》。

特点：
  1. 全市场无偏扫描——原书案例是事后挑选（幸存者偏差），
     本扫描先定参数后扫全市场，涨跌如实收录，方法论更硬。
  2. 零第三方依赖——只用 Python 标准库，workflow 免安装。
  3. 格式自适应——自动探测 kline JSON 的结构与键名。
  4. is_real 真伪标注——案例日期在账本覆盖范围（2025-11 起）内时，
     自动关联真假量柱判定（原书案例完全没有的维度）。

用法：
  python scripts/case_finder.py
  产物：data/analysis/casebook.md
"""

import json
import os
import glob
import math
from collections import defaultdict, Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINE_DIR = os.path.join(BASE, "data", "kline")
LEDGER_DIR = os.path.join(BASE, "data", "analysis", "truth_ledger")
HS300_PATH = os.path.join(BASE, "data", "hushen300.json")
OUT_PATH = os.path.join(BASE, "data", "analysis", "casebook.md")

TOP_N = 8        # 每形态收录案例数（最新优先）
LOOKBACK = 500   # 扫描窗口：每只股票最近 500 根日线

# ---------- 工具函数 ----------

def to_f(x):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def norm_code(s):
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def norm_date(s):
    return str(s).replace("/", "-")[:10]


def _is_date_key(s):
    s = str(s)
    return len(s) >= 8 and s[:2].isdigit() and "-" in s


def _looks_like_date(s):
    s = str(s)
    return len(s) >= 8 and s[0:1].isdigit() and ("-" in s or "/" in s or s.isdigit())


# ---------- K 线解析（格式自适应） ----------

DATE_KEYS = ["date", "datetime", "day", "time", "t", "日期"]
OPEN_KEYS = ["open", "o", "kp", "开盘"]
HIGH_KEYS = ["high", "h", "g", "最高"]
LOW_KEYS = ["low", "l", "d", "最低"]
CLOSE_KEYS = ["close", "c", "sp", "收盘"]
VOL_KEYS = ["vol", "volume", "v", "cjl", "成交量"]


def _finalize(d):
    """清洗：剔除价格缺失行；high/low 缺失用开收补；vol 缺失填 0"""
    clean = {"code": d["code"], "dates": [], "open": [], "high": [],
             "low": [], "close": [], "vol": []}
    n = len(d["close"])
    for i in range(n):
        c = d["close"][i]
        o = d["open"][i]
        if c is None or o is None or c <= 0 or o <= 0:
            continue
        h = d["high"][i] if d["high"][i] else max(o, c)
        l = d["low"][i] if d["low"][i] else min(o, c)
        v = d["vol"][i] if d["vol"][i] is not None else 0.0
        clean["dates"].append(d["dates"][i])
        clean["open"].append(o)
        clean["high"].append(h)
        clean["low"].append(l)
        clean["close"].append(c)
        clean["vol"].append(v)
    return clean if len(clean["close"]) >= 60 else None


def parse_from_dict_list(code, rows):
    """list of dict → 统一结构"""
    if not rows or not isinstance(rows[0], dict):
        return None
    keys = set(rows[0].keys())

    def find(cands):
        for k in cands:
            if k in keys:
                return k
        return None

    kd, ko = find(DATE_KEYS), find(OPEN_KEYS)
    kh, kl = find(HIGH_KEYS), find(LOW_KEYS)
    kc, kv = find(CLOSE_KEYS), find(VOL_KEYS)
    if kd is None or kc is None or kv is None:
        return None
    out = {"code": code, "dates": [], "open": [], "high": [],
           "low": [], "close": [], "vol": []}
    for r in rows:
        if not isinstance(r, dict):
            continue
        out["dates"].append(str(r.get(kd, "")))
        out["open"].append(to_f(r.get(ko)) if ko else None)
        out["high"].append(to_f(r.get(kh)) if kh else None)
        out["low"].append(to_f(r.get(kl)) if kl else None)
        out["close"].append(to_f(r.get(kc)))
        out["vol"].append(to_f(r.get(kv)))
    return _finalize(out)


def parse_from_rows(code, rows):
    """list of str(逗号分隔) 或 list of list → 按列序猜测"""
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, str):
        if "," not in first:
            return None
        cells = [s.split(",") for s in rows]
    elif isinstance(first, list):
        cells = rows
    else:
        return None
    if not cells or len(cells[0]) < 6:
        return None
    if not _looks_like_date(cells[0][0]):
        return None
    ncols = len(cells[0])
    # 常见布局：date,open,close,high,low,vol / date,open,high,low,close,vol
    layouts = [(0, 1, 2, 3, 4, 5), (0, 1, 4, 2, 3, 5), (0, 1, 2, 3, 4, 5)]
    chosen = None
    for lay in layouts:
        di, oi, ci, hi, li, vi = lay
        if max(lay) >= ncols:
            continue
        ok = True
        for row in cells[:20]:
            o, c = to_f(row[oi]), to_f(row[ci])
            h, l, v = to_f(row[hi]), to_f(row[li]), to_f(row[vi])
            if None in (o, c, h, l, v) or min(o, c, h, l) <= 0 or v < 0:
                ok = False
                break
            if h < max(o, c) * 0.98 or l > min(o, c) * 1.02:
                ok = False
                break
        if ok:
            chosen = lay
            break
    if chosen is None:
        return None
    di, oi, ci, hi, li, vi = chosen
    out = {"code": code, "dates": [], "open": [], "high": [],
           "low": [], "close": [], "vol": []}
    for row in cells:
        out["dates"].append(str(row[di]))
        out["open"].append(to_f(row[oi]))
        out["high"].append(to_f(row[hi]))
        out["low"].append(to_f(row[li]))
        out["close"].append(to_f(row[ci]))
        out["vol"].append(to_f(row[vi]))
    return _finalize(out)


def parse_parallel(code, raw):
    """dict 平行数组 {dates:[], open:[], ...}"""
    def pk(cands):
        for k in cands:
            if k in raw and isinstance(raw[k], list):
                return raw[k]
        return None

    dates = pk(["dates", "date", "datetime", "days", "time"])
    o = pk(["opens", "open", "o"])
    h = pk(["highs", "high", "h"])
    l = pk(["lows", "low", "l"])
    c = pk(["closes", "close", "c"])
    v = pk(["vols", "vol", "volume", "v", "cjl"])
    if not (dates and c and v):
        return None
    n = len(dates)

    def pad(arr):
        if arr is None:
            return [None] * n
        return (list(arr) + [None] * n)[:n]

    out = {"code": code, "dates": [str(x) for x in dates],
           "open": pad(o), "high": pad(h), "low": pad(l),
           "close": pad(c), "vol": pad(v)}
    return _finalize(out)


def load_kline_file(path):
    code = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None

    def try_list(sub):
        return parse_from_dict_list(code, sub) or parse_from_rows(code, sub)

    if isinstance(raw, list):
        return try_list(raw)
    if isinstance(raw, dict):
        for key in ("klines", "data", "list", "lines", "items"):
            if key in raw and isinstance(raw[key], list):
                r = try_list(raw[key])
                if r:
                    return r
        r = parse_parallel(code, raw)
        if r:
            return r
        for k, sub in raw.items():  # 嵌套一层 {code: {...}}
            if isinstance(sub, dict):
                for key in ("klines", "data", "list"):
                    if key in sub and isinstance(sub[key], list):
                        r = try_list(sub[key])
                        if r:
                            return r
                r = parse_parallel(code, sub)
                if r:
                    return r
    return None


# ---------- 账本 is_real 索引（尽力而为，读不到不崩） ----------

def _extract_real(entry):
    if isinstance(entry, dict):
        for key in ("is_real", "real", "truth"):
            if key in entry:
                val = entry[key]
                if val is None:
                    return "null(疑似)"
                return "true(真金白银)" if val in (True, "true", 1) else "false(对倒)"
    return None


def load_ledger_index():
    idx = {}
    for path in glob.glob(os.path.join(LEDGER_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict) or not data:
            continue
        k0 = next(iter(data))
        if isinstance(data[k0], dict):
            if _is_date_key(k0):  # date -> {code: entry}
                for dt, sub in data.items():
                    if isinstance(sub, dict):
                        for cd, entry in sub.items():
                            ir = _extract_real(entry)
                            if ir:
                                idx[(norm_code(cd), norm_date(dt))] = ir
            else:  # code -> {date: entry}
                for cd, sub in data.items():
                    if isinstance(sub, dict):
                        for dt, entry in sub.items():
                            ir = _extract_real(entry)
                            if ir:
                                idx[(norm_code(cd), norm_date(dt))] = ir
        elif isinstance(data[k0], list):  # list of entries
            for entry in data[k0]:
                if isinstance(entry, dict):
                    cd = entry.get("code") or entry.get("stock") or entry.get("symbol")
                    dt = entry.get("date") or entry.get("day")
                    ir = _extract_real(entry)
                    if cd and dt and ir:
                        idx[(norm_code(cd), norm_date(dt))] = ir
    return idx


def load_names():
    names = {}
    try:
        with open(HS300_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return names
    if isinstance(raw, dict):
        for k, val in raw.items():
            if isinstance(val, str):
                names[norm_code(k)] = val
            elif isinstance(val, dict):
                nm = val.get("name") or val.get("名称")
                if nm:
                    names[norm_code(k)] = nm
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                cd = it.get("code") or it.get("股票代码") or it.get("ts_code")
                nm = it.get("name") or it.get("名称") or it.get("股票名称")
                if cd and nm:
                    names[norm_code(cd)] = nm
    return names


# ---------- 形态扫描 ----------

def case(d, i, extra, priority=0):
    n = len(d["close"])
    perf = {}
    for k in (5, 10, 20):
        if i + k < n:
            perf[f"+{k}日"] = f"{(d['close'][i + k] / d['close'][i] - 1) * 100:+.2f}%"
        else:
            perf[f"+{k}日"] = "—"
    return {"code": d["code"], "date": d["dates"][i], "idx": i,
            "close": d["close"][i], "perf": perf,
            "extra": extra, "sort": (priority, -i)}


def scan_stock(d, forms):
    n = len(d["close"])
    start = max(1, n - LOOKBACK)
    v, o, c, h, l = d["vol"], d["open"], d["close"], d["high"], d["low"]

    for i in range(start, n):
        if v[i - 1] <= 0 or v[i] <= 0:
            continue
        ratio = v[i] / v[i - 1]
        yang = c[i] > o[i]
        yin = c[i] < o[i]

        # 1) 倍量柱（《量柱》第7讲：激进1.75x / 保守2x，阳线，低位加分）
        if yang and ratio >= 1.75:
            low60 = min(l[max(0, i - 60):i + 1])
            is_low = c[i] <= low60 * 1.2
            forms["beiliang"].append(case(d, i, {
                "量比": f"{ratio:.2f}x",
                "口径": "保守" if ratio >= 2 else "激进",
                "位置": "低位" if is_low else "非低位",
            }, priority=0 if is_low else 1))

        # 3) 缩倍量（《量柱》第9/12讲：休克疗法）
        if ratio <= 0.5:
            forms["suobei"].append(case(d, i, {"缩量比": f"{ratio:.2f}x"}))

        # 4) 并肩平量柱（《量柱》第11讲：误差≤3%）
        if abs(ratio - 1) <= 0.03:
            forms["pingliang"].append(case(d, i, {"并肩误差": f"{abs(ratio - 1) * 100:.2f}%"}))

        # 7) 长腿踩线（《密码》第10章：下影≥2倍实体 + 踩60日阶段底线±0.5%）
        body = abs(c[i] - o[i])
        shadow = min(o[i], c[i]) - l[i]
        if body > 0 and shadow >= 2 * body and i >= 60:
            base = min(l[i - 60:i])
            if base > 0 and abs(l[i] - base) / base <= 0.005:
                forms["changtui"].append(case(d, i, {"下影/实体": f"{shadow / body:.1f}倍"}))

        # 8) 九阴真经·倍量阴（《密码》第18章：出货警报，反面案例）
        if yin and ratio >= 2:
            forms["jiuyin"].append(case(d, i, {"阴量比": f"{ratio:.2f}x"}))

    scan_gaoliang(d, forms, start)
    scan_shencuo(d, forms, start)
    scan_jiangjun(d, forms, start)
    scan_jingzhun(d, forms, start)


def scan_gaoliang(d, forms, start):
    """高量黄金柱（《量柱》第8/13讲）：40日最高量阳柱 + 后三日喇叭口"""
    v, o, c, l = d["vol"], d["open"], d["close"], d["low"]
    n = len(c)
    for i in range(max(start, 41), n - 3):
        if v[i] <= 0 or c[i] <= o[i]:
            continue
        if v[i] < max(v[max(0, i - 40):i]):
            continue
        if sum(v[i + 1:i + 4]) < v[i] * 2.2 and min(l[i + 1:i + 4]) > o[i]:
            forms["gaoliang"].append(case(d, i, {
                "40日最高量": "是",
                "后三日量/当日": f"{sum(v[i + 1:i + 4]) / v[i]:.2f}",
            }))


def scan_shencuo(d, forms, start):
    """倍量伸缩（《密码》第16章）：伸倍量后3日内现缩倍量"""
    v, o, c = d["vol"], d["open"], d["close"]
    n = len(c)
    for i in range(start, n - 4):
        if not (c[i] > o[i] and v[i] >= v[i - 1] * 1.75 and v[i - 1] > 0):
            continue
        for j in range(i + 1, min(i + 4, n)):
            if v[j] > 0 and v[j] <= v[j - 1] * 0.5:
                forms["shencuo"].append(case(d, i, {
                    "伸倍比": f"{v[i] / v[i - 1]:.2f}x",
                    "缩日间隔": f"{j - i}日",
                    "缩量比": f"{v[j] / v[j - 1]:.2f}x",
                }))
                break


def scan_jiangjun(d, forms, start):
    """将军柱/黄金柱（《伏击》第8章判别树简化版）"""
    v, o, c, l = d["vol"], d["open"], d["close"], d["low"]
    n = len(c)
    for i in range(max(start, 1), n - 3):
        if not (c[i] > o[i] and v[i] > v[i - 1] > 0):
            continue
        if c[i - 1] >= o[i - 1]:  # 前一日须为阴线
            continue
        if max(o[i], c[i]) <= max(o[i - 1], c[i - 1]):  # 实顶未胜
            continue
        lo3 = min(l[i + 1:i + 4])
        mc3 = sum(c[i + 1:i + 4]) / 3
        mv3 = max(v[i + 1:i + 4])
        mean_v3 = sum(v[i + 1:i + 4]) / 3
        # 将军柱：三日不破实底 且 三日量不过顶
        if (lo3 >= o[i] or mc3 >= o[i]) and (mv3 < v[i] or mean_v3 < v[i]):
            forms["jiangjun"].append(case(d, i, {"基柱量比": f"{v[i] / v[i - 1]:.2f}x"}))
            # 黄金柱：三日收盘不破基柱收盘 且 量群缩小
            if (min(c[i + 1:i + 4]) > c[i] or mc3 > c[i]) and sum(v[i + 1:i + 4]) < v[i] * 2.4:
                forms["huangjin"].append(case(d, i, {"基柱量比": f"{v[i] / v[i - 1]:.2f}x"}))


def scan_jingzhun(d, forms, start):
    """精准线（《量线》第9讲/《密码》第7章）：1分钱网格触碰≥3次"""
    l = d["low"]
    n = len(l)
    lo_start = max(0, n - LOOKBACK)
    lows = l[lo_start:]
    cnt = Counter()
    for x in lows:
        if x > 0:
            cnt[int(x * 100 + 0.5)] += 1
    merged = {k: cnt.get(k - 1, 0) + cnt.get(k, 0) + cnt.get(k + 1, 0) for k in cnt}
    hits = {k: num for k, num in merged.items() if num >= 3}
    if not hits:
        return
    seen = {}
    for idx in range(len(lows) - 1, -1, -1):  # 每价位只取最近触碰日
        k = int(lows[idx] * 100 + 0.5)
        if k in hits and k not in seen:
            seen[k] = idx
    for k, idx in seen.items():
        i = lo_start + idx
        forms["jingzhun"].append(case(d, i, {
            "精准价位": f"{k / 100:.2f}",
            "触碰次数": f"{hits[k]}次",
        }))


# ---------- 输出 ----------

FORM_META = [
    ("beiliang", "倍量柱", "《量柱擒涨停》第7讲", "广博股份A-G柱 / 维维股份（2008-2009）",
     "比前日量≥1.75倍（激进口径）的阳量柱；[低位]=收盘距60日最低点≤20%"),
    ("gaoliang", "高量黄金柱（喇叭口）", "《量柱擒涨停》第8/13讲", "中银绒业 / 凌云股份（2009）",
     "40日内最高量的阳柱，且后三日量柱总和<2.2倍当日量、最低价不破当日开盘"),
    ("suobei", "缩倍量（休克疗法）", "《量柱擒涨停》第9/12讲", "通宝能源 / 靖远煤电区间222%（2009）",
     "量柱缩至前日一半以下"),
    ("pingliang", "并肩平量柱", "《量柱擒涨停》第11讲", "四维控股 / 渝开发（2009-2010）",
     "与前日量柱误差≤3%"),
    ("jiangjun", "将军柱", "《伏击涨停》第8章判别树", "莱茵生物 / 中兵光电（2009）",
     "阳胜阴基柱（阳线+量胜+实顶胜）+ 后三日不破实底 + 后三日量不过顶"),
    ("huangjin", "黄金柱", "《伏击涨停》第8章判别树", "海博股份12柱全黄金（2013）",
     "将军柱 + 后三日收盘不破基柱收盘 + 后三日量群缩小（<2.4倍基柱量）"),
    ("shencuo", "倍量伸缩", "《涨停密码》第16章", "东宝生物 / 创业环保区间86.97%（2012-2013）",
     "伸倍量（≥1.75x阳线）后3日内出现缩倍量（≤0.5x）"),
    ("changtui", "长腿踩线", "《涨停密码》第10章", "特尔佳 / 建投能源（2010）",
     "下影线≥2倍实体 + 最低点踩60日阶段底线（±0.5%）"),
    ("jingzhun", "精准线（1分钱网格）", "《量线捉涨停》第9讲 / 《密码》第7章", "长电科技8.60三点重合（2009）⭐本项目自选股",
     "60日内最低价在±1分钱网格内触碰≥3次的价位（取最近触碰日为锚点）"),
    ("jiuyin", "九阴真经·倍量阴（反面案例）", "《涨停密码》第18章", "原书图18-2之A/E点",
     "阴线且量≥前日2倍——出货警报案例。本形态的案例应看到下跌居多，若涨多则说明口径需复核"),
]


def write_casebook(all_cases, stats, names, ledger_index, meta, parse_fail, total_files):
    lines = []
    lines.append("# 量学现代案例集（自动扫描生成）")
    lines.append("")
    lines.append(f"> 生成时间：{meta['now']}")
    lines.append(f"> 数据范围：{total_files} 只日线文件，成功解析 {total_files - parse_fail} 只（解析失败 {parse_fail} 只）")
    lines.append(f"> 日线窗口：约 {meta['date_min']} ~ {meta['date_max']}（每只扫描最近 {LOOKBACK} 根）")
    lines.append(f"> 真伪标注：is_real 来自真假量柱账本（覆盖 2025-11 起；此前的案例显示 —）")
    lines.append("")
    lines.append("> **方法论声明（重要）**：")
    lines.append("> 1. 本案例集由**先定参数、后扫全市场**生成——与原书「事后挑选成功案例」不同，")
    lines.append(">    本集**如实收录涨跌两种结果**，+20日为负的案例同样保留（幸存者偏差规避）。")
    lines.append("> 2. 某形态命中数为 0 ≠ 形态无效，可能是口径过严——留待 11-01 体检校准。")
    lines.append("> 3. 高/低量级暂未做市值分桶（无市值数据，mcap_bucket 落地后升级 v2）。")
    lines.append("> 4. 精准线用 1 分钱网格（原书低价股口径）；高价股（>50元）容差应放宽至5分，v2 升级。")
    lines.append("")
    for key, title, src, old, desc in FORM_META:
        cases = all_cases.get(key, [])
        total = stats.get(key, 0)
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"> **原书案例**：{old} —— 距今十余年")
        lines.append(f"> **扫描口径**：{desc}（{src}）")
        lines.append(f"> **全市场命中**：{total} 例，收录最新 {len(cases)} 例")
        lines.append("")
        if cases:
            lines.append("| 股票 | 日期 | 关键参数 | 当日收盘 | +5日 | +10日 | +20日 | is_real |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for cs in cases:
                name = names.get(norm_code(cs["code"]), "")
                label = f"{cs['code']}（{name}）" if name else cs["code"]
                real = ledger_index.get((norm_code(cs["code"]), norm_date(cs["date"])), "—")
                params = "，".join(f"{k}={v}" for k, v in cs["extra"].items())
                p = cs["perf"]
                lines.append(f"| {label} | {cs['date']} | {params} | {cs['close']:.2f} | "
                             f"{p['+5日']} | {p['+10日']} | {p['+20日']} | {real} |")
        else:
            lines.append("*（本形态全市场 0 命中——口径可能过严，或与数据格式有关，留待体检）*")
        lines.append("")
    lines.append("## 尾注")
    lines.append("")
    lines.append("- 本案例集是 12 月考场的**预演**：脚本扫得出案例 = 四册的参数化定义可计算。")
    lines.append("- 案例的 +5/+10/+20 日表现由真实日线计算，**未做任何前视或挑选**。")
    lines.append("- is_real 列是本项目对量学案例的独有增维——原书案例永远无法回答「这根量柱是真的吗」。")
    lines.append("")
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    files = sorted(glob.glob(os.path.join(KLINE_DIR, "*.json")))
    print(f"[case_finder] 发现日线文件 {len(files)} 只")
    if not files:
        print(f"[case_finder] 错误：{KLINE_DIR} 下没有 json 文件，退出")
        return

    all_cases = defaultdict(list)
    parse_fail = 0
    debug_shown = 0
    date_min, date_max = "9999-99-99", "0000-00-00"

    ledger_index = load_ledger_index()
    names = load_names()
    print(f"[case_finder] 账本索引 {len(ledger_index)} 条；名称映射 {len(names)} 条")

    for idx, path in enumerate(files, 1):
        d = load_kline_file(path)
        if not d:
            parse_fail += 1
            if debug_shown < 3:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        head = f.read(400)
                    print(f"[诊断] 无法解析 {os.path.basename(path)}，文件开头：{head}")
                except Exception:
                    pass
                debug_shown += 1
            continue
        if d["dates"]:
            date_min = min(date_min, d["dates"][0])
            date_max = max(date_max, d["dates"][-1])
        scan_stock(d, all_cases)
        if idx % 500 == 0:
            print(f"[case_finder] 进度 {idx}/{len(files)}")

    stats = {}
    for key, _, _, _, _ in FORM_META:
        lst = all_cases.get(key, [])
        stats[key] = len(lst)
        lst.sort(key=lambda x: x["sort"])
        all_cases[key] = lst[:TOP_N]

    meta = {
        "now": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_min": date_min if date_min != "9999-99-99" else "N/A",
        "date_max": date_max if date_max != "0000-00-00" else "N/A",
    }
    write_casebook(all_cases, stats, names, ledger_index, meta, parse_fail, len(files))
    print(f"[case_finder] 完成 → {OUT_PATH}")
    print(f"[case_finder] 各形态命中：{json.dumps(stats, ensure_ascii=False)}")
    if parse_fail > len(files) * 0.5:
        print(f"[case_finder] ⚠ 警告：解析失败 {parse_fail}/{len(files)}，疑似格式不匹配，请把上方[诊断]输出发给AI")


if __name__ == "__main__":
    main()
