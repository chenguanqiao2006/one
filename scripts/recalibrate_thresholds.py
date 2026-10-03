#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recalibrate_thresholds.py — 阈值离线重校准器（配合 record_truth.py v2）
=====================================================================

职责：批量读取账本 data/analysis/truth_ledger/*.json，对判定阈值做全面体检。

    A. 样本概览        —— v2 快照账目 / v1 历史账目 / 涨跌停豁免 / 判定结构
    B. 快照一致性自检  —— vprofile_24 重算 flatness / tail 与存储值比对；
                          顺带标定 24 桶 CV 与精确 1 分钟 CV 的换算系数
    C. 四指标分布      —— 直方图 + 百分位 + 阈值命中率与分位定位 + 参考建议
    D. 重投票自洽性    —— 基线阈值重放投票，与账本判定比对（发现账本漂移）；
                          附：疑似量化账目的决定性指标分布、对倒命中票数分布
    E. 阈值扫描        ——（--sweep）单指标候选阈值下的三档判定分布
    F. 新阈值模拟      ——（--thresh）基线 → 新阈值的翻转矩阵与翻转样本
    G. 月度漂移        —— 指标均值与判定结构随月份变化
    H. 按股票汇总      ——（--top N）账目数最多的前 N 只股票
    I. JSON 导出       ——（--json）机器可读摘要，可入库做长期趋势跟踪

口径说明：
    [1] v2 账目含 vprofile_24：flatness / tail 可从快照精确重算；cv 只能由
        24 桶估算（B 节自动标定换算系数，MAPE 会如实反映可靠性）；
        corr 涉及价格序列，无法从快照重算，分布统计直接使用存储值。
    [2] v1 账目无快照：仅 cv / corr / tail 参与统计；重投票自动降级为三指
        标投票，与 v1 判定逻辑保持一致。
    [3] 涨跌停豁免账目不计入指标统计与重投票（其量价特征本身无判定意义）。

用法：
    python scripts/recalibrate_thresholds.py
    python scripts/recalibrate_thresholds.py --sweep
    python scripts/recalibrate_thresholds.py --thresh cv=0.6,flatness=0.2
    python scripts/recalibrate_thresholds.py --months 2026-09 --codes sh601138
    python scripts/recalibrate_thresholds.py --json data/analysis/threshold_report.json

纯标准库实现，无第三方依赖。
"""

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER_DIR = BASE_DIR / 'data' / 'analysis' / 'truth_ledger'

# 与 record_truth.py 头部 THRESH 保持一致的基线阈值
DEFAULT_THRESH = {'cv': 0.5, 'corr': 0.3, 'tail': 0.3, 'flatness': 0.25}

# 6 窗口在 24 桶快照中的桶索引（与 record_truth.py 的 SESSION_SPLITS 严格对齐）
SESSION_WINDOWS_BUCKETS = [(0, 3), (3, 9), (9, 12), (12, 18), (18, 21), (21, 24)]
TAIL_BUCKETS = (21, 24)

SWEEP_CANDIDATES = {
    'cv':       [0.3, 0.4, 0.5, 0.6, 0.8, 1.0],
    'corr':     [0.15, 0.2, 0.25, 0.3, 0.4, 0.5],
    'tail':     [0.2, 0.25, 0.3, 0.35, 0.4, 0.5],
    'flatness': [0.15, 0.2, 0.25, 0.3, 0.4, 0.5],
}
FIELD_OF = {'cv': 'cv', 'corr': 'corr', 'tail': 'tail_ratio', 'flatness': 'flatness'}

# 快照重算 vs 存储值容差（双重舍入：存储值 4 位小数 + 快照 6 位小数；
# 不足 240 根的交易日，tail 的 [-30:] 口径与快照桶位口径存在细微差异）
TOL_FLAT = 5e-3
TOL_TAIL = 2e-2

CST = timezone(timedelta(hours=8))
VERDICT_MAP = {'真金白银': 'real', '疑似量化': 'suspect', '量化对倒': 'fake'}
ORDER = ['real', 'suspect', 'fake']
LABEL = {'real': '真金白银', 'suspect': '疑似量化', 'fake': '量化对倒'}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def print_header(title):
    print('\n' + '═' * 66)
    print('  ' + title)
    print('═' * 66)


def mean(vals):
    return sum(vals) / len(vals) if vals else None


def fmt_mean(vals):
    return f'{mean(vals):.3f}' if vals else '-'


def rnd(vals):
    return round(mean(vals), 4) if vals else None


def fmt_pct(x):
    return f'{x * 100:.1f}%'


def percentile(vals, p):
    sv = sorted(vals)
    n = len(sv)
    if n == 0:
        return None
    if n == 1:
        return sv[0]
    idx = p / 100.0 * (n - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sv[lo] * (1 - frac) + sv[hi] * frac


# ---------------------------------------------------------------------------
# 账本加载与快照重算
# ---------------------------------------------------------------------------

def load_entries(ledger_dir, months=None, codes=None):
    entries = []
    files = sorted(ledger_dir.glob('*.json')) if ledger_dir.exists() else []
    for f in files:
        if months and f.stem not in months:
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f'[WARN] {f.name} 解析失败，跳过: {e}')
            continue
        if not isinstance(data, dict):
            continue
        for code, days in data.items():
            if codes and code not in codes:
                continue
            if not isinstance(days, dict):
                continue
            for d, entry in days.items():
                if not isinstance(entry, dict):
                    continue
                e = {'month': f.stem, 'code': code, 'date': d}
                e.update(entry)
                entries.append(e)
    return entries, files


def recompute_from_snapshot(vp):
    """vprofile_24 → {flatness, tail_ratio, bucket_cv}。无效返回 None。"""
    if not vp or not isinstance(vp, list) or len(vp) < 24:
        return None
    try:
        prof = [float(x) for x in vp]
    except (TypeError, ValueError):
        return None
    total = sum(prof)
    if total <= 0:
        return None
    prof = [p / total for p in prof]
    six = [sum(prof[a:b]) for a, b in SESSION_WINDOWS_BUCKETS]
    flatness = sum(abs(w - 1.0 / 6) for w in six)
    tail = sum(prof[TAIL_BUCKETS[0]:TAIL_BUCKETS[1]])
    m = sum(prof) / len(prof)
    var = sum((p - m) ** 2 for p in prof) / len(prof)
    bucket_cv = math.sqrt(var) / m if m > 0 else None
    return {'flatness': flatness, 'tail_ratio': tail, 'bucket_cv': bucket_cv}


# ---------------------------------------------------------------------------
# 报表绘制
# ---------------------------------------------------------------------------

def ascii_hist(values, lo, hi, nbins, label, thresh=None, hit_when='lt'):
    n = len(values)
    if n == 0:
        print(f'\n  {label}: 无数据')
        return
    width = (hi - lo) / nbins
    bins = [0] * nbins
    out = 0
    for v in values:
        if v < lo or v >= hi:
            out += 1
            continue
        bins[min(int((v - lo) / width), nbins - 1)] += 1
    mx = max(bins) or 1
    extra = f'  越界 {out}' if out else ''
    print(f'\n  {label}  n={n}  范围 [{lo:g}, {hi:g})  桶宽 {width:.3g}{extra}')
    tbin = None
    if thresh is not None and lo <= thresh < hi:
        tbin = int((thresh - lo) / width)
    for i, c in enumerate(bins):
        if tbin is not None and i == tbin:
            side = '<' if hit_when == 'lt' else '>'
            print(f'    ────── 阈值 {thresh:g}（命中: 值 {side} 阈值）──────')
        bar = '█' * round(36 * c / mx)
        print(f'    [{lo + i * width:7.3f}, {lo + (i + 1) * width:7.3f}) {c:5d} {fmt_pct(c / n):>6}  {bar}')
    print('    百分位: ' + '  '.join(
        f'P{p}={percentile(values, p):.3f}' for p in (5, 10, 25, 50, 75, 90, 95)))
    if thresh is not None:
        hit = sum(1 for v in values if (v < thresh if hit_when == 'lt' else v > thresh))
        below = sum(1 for v in values if v < thresh) / n
        print(f'    阈值命中: {hit}/{n} = {fmt_pct(hit / n)}  （阈值位于经验分布 P{below * 100:.1f}）')


def suggest(values, thresh, hit_when, name):
    n = len(values)
    if not n:
        return
    hit = sum(1 for v in values if (v < thresh if hit_when == 'lt' else v > thresh))
    q10 = percentile(values, 10) if hit_when == 'lt' else percentile(values, 90)
    q25 = percentile(values, 25) if hit_when == 'lt' else percentile(values, 75)
    print(f'    [参考] {name}: 当前命中 {fmt_pct(hit / n)}；'
          f'若目标命中 10% → 阈值 {q10:.3f}；目标 25% → {q25:.3f}（按当前样本经验分位）')


def ind_report(values, thresh, hit_when):
    if not values:
        return {'n': 0}
    sv = sorted(values)
    hit = sum(1 for v in values if (v < thresh if hit_when == 'lt' else v > thresh))
    return {'n': len(values), 'thresh': thresh,
            'hit_rate': round(hit / len(values), 4),
            'dist': {f'P{p}': round(percentile(sv, p), 4)
                     for p in (5, 10, 25, 50, 75, 90, 95)}}


# ---------------------------------------------------------------------------
# 投票引擎（与 record_truth.py v2 完全一致）
# ---------------------------------------------------------------------------

def revote(cv, corr, tail, flat, th):
    hits = 0
    if cv is not None and cv < th['cv']:
        hits += 1
    if corr is not None and abs(corr) < th['corr']:
        hits += 1
    if tail is not None and tail > th['tail']:
        hits += 1
    if flat is not None and flat < th['flatness']:
        hits += 1
    if hits == 0:
        return 'real'
    if hits == 1:
        return 'suspect'
    return 'fake'


def parse_thresh(s):
    th = dict(DEFAULT_THRESH)
    if s:
        for part in s.split(','):
            part = part.strip()
            if not part:
                continue
            if '=' not in part:
                raise argparse.ArgumentTypeError(f'格式应为 key=value: {part}')
            k, v = part.split('=', 1)
            k = k.strip().lower()
            if k not in th:
                raise argparse.ArgumentTypeError(
                    f'未知阈值项: {k}（可选: cv, corr, tail, flatness）')
            th[k] = float(v)
    return th


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description='真假量柱账本阈值离线重校准器（配合 record_truth.py v2）')
    ap.add_argument('--ledger-dir', type=Path, default=DEFAULT_LEDGER_DIR,
                    help='账本目录（默认 data/analysis/truth_ledger）')
    ap.add_argument('--months', default='',
                    help='限定月份，逗号分隔，如 2026-08,2026-09')
    ap.add_argument('--codes', default='',
                    help='限定股票，逗号分隔，如 sh601138,sz002156')
    ap.add_argument('--thresh', type=parse_thresh, default=None,
                    help='模拟新阈值组合，如 cv=0.6,flatness=0.2（缺省用基线）')
    ap.add_argument('--sweep', action='store_true', help='输出单指标阈值扫描表')
    ap.add_argument('--top', type=int, default=0, metavar='N',
                    help='输出按股票汇总的前 N 名')
    ap.add_argument('--json', type=Path, default=None, metavar='FILE',
                    help='将摘要导出为 JSON（建议入库做趋势跟踪）')
    args = ap.parse_args()

    baseline = dict(DEFAULT_THRESH)
    sim = args.thresh if args.thresh is not None else dict(DEFAULT_THRESH)
    months = {m.strip() for m in args.months.split(',') if m.strip()}
    codes = {c.strip() for c in args.codes.split(',') if c.strip()}

    entries, files = load_entries(args.ledger_dir, months, codes)
    if not entries:
        print(f'[INFO] 账本无数据: {args.ledger_dir}')
        return 0
    print(f'[INFO] 账本目录: {args.ledger_dir}')
    print('[INFO] 读取 ' + str(len(files)) + ' 个文件: ' + ', '.join(f.stem for f in files))
    if months:
        print(f'[INFO] 月份过滤: {sorted(months)}')
    if codes:
        print(f'[INFO] 股票过滤: {sorted(codes)}')

    v2 = [e for e in entries if e.get('vprofile_24')]
    v1 = [e for e in entries if not e.get('vprofile_24')]
    exempt = [e for e in entries if e.get('limit_status')]
    normal = [e for e in entries if not e.get('limit_status')]

    # ---------- A. 概览 ----------
    print_header('A. 样本概览')
    print(f'  账目总数: {len(entries)}   v2(含快照): {len(v2)}   '
          f'v1(历史): {len(v1)}   涨跌停豁免: {len(exempt)}')
    mix = {k: 0 for k in ORDER}
    for e in normal:
        k = VERDICT_MAP.get(e.get('verdict'))
        if k:
            mix[k] += 1
    if normal:
        print('  存储判定结构: ' + '   '.join(
            f'{LABEL[k]} {mix[k]} ({fmt_pct(mix[k] / len(normal))})' for k in ORDER))
    print('  基线阈值: ' + '   '.join(
        f'{k}={baseline[k]}' for k in ('cv', 'corr', 'tail', 'flatness')))

    # ---------- B. 快照一致性自检 ----------
    print_header('B. 快照一致性自检（v2: vprofile_24 重算 vs 存储值）')
    v2n = [e for e in normal if e.get('vprofile_24')]
    recs = []
    for e in v2n:
        r = recompute_from_snapshot(e['vprofile_24'])
        if r:
            recs.append((e, r))
    checked_f = bad_f = checked_t = bad_t = 0
    mism = []
    pairs = []
    k_med = None
    for e, r in recs:
        sf, st, scv = e.get('flatness'), e.get('tail_ratio'), e.get('cv')
        if sf is not None:
            checked_f += 1
            if abs(r['flatness'] - sf) > TOL_FLAT:
                bad_f += 1
                if len(mism) < 8:
                    mism.append(f"    {e['month']} {e['code']} {e['date']}  "
                                f"flatness: 存储 {sf} / 重算 {r['flatness']:.4f}")
        if st is not None:
            checked_t += 1
            if abs(r['tail_ratio'] - st) > TOL_TAIL:
                bad_t += 1
                if len(mism) < 8:
                    mism.append(f"    {e['month']} {e['code']} {e['date']}  "
                                f"tail: 存储 {st} / 重算 {r['tail_ratio']:.4f}")
        if scv is not None and r['bucket_cv']:
            pairs.append((scv, r['bucket_cv']))
    if v2n:
        if checked_f:
            print(f'  flatness 重算一致: {checked_f - bad_f}/{checked_f} '
                  f'({fmt_pct((checked_f - bad_f) / checked_f)})  [容差 {TOL_FLAT}]')
        if checked_t:
            print(f'  tail_ratio 重算一致: {checked_t - bad_t}/{checked_t} '
                  f'({fmt_pct((checked_t - bad_t) / checked_t)})  [容差 {TOL_TAIL}]')
        if mism:
            print('  不一致明细（快照被篡改 / 串档 / 口径异常，需人工核查）:')
            print('\n'.join(mism))
        if pairs:
            ratios = sorted(a / b for a, b in pairs)
            k_med = percentile(ratios, 50)
            mape = sum(abs(b * k_med - a) / a for a, b in pairs) / len(pairs)
            print(f'  cv 估算标定: k = cv精确 / cv桶 = {k_med:.2f}'
                  f'（P25 {percentile(ratios, 25):.2f} / P75 {percentile(ratios, 75):.2f}，n={len(pairs)}）')
            print(f'  cv 估算误差 MAPE = {fmt_pct(mape)}'
                  '  —— 24 桶 CV 仅可作粗略代理，精确判定仍依赖 1 分钟数据')
    else:
        print('  （无 v2 快照账目，本节跳过）')

    # ---------- C. 指标分布 ----------
    print_header('C. 四指标分布（常规账目存储值；flatness 仅 v2 账目）')
    cv_vals = [e['cv'] for e in normal if e.get('cv') is not None]
    ca_vals = [abs(e['corr']) for e in normal if e.get('corr') is not None]
    tl_vals = [e['tail_ratio'] for e in normal if e.get('tail_ratio') is not None]
    fl_vals = [e['flatness'] for e in normal if e.get('flatness') is not None]
    if cv_vals:
        ascii_hist(cv_vals, 0, max(2.5, max(cv_vals) * 1.05), 25,
                   'CV（1 分钟成交量变异系数）', baseline['cv'], 'lt')
        suggest(cv_vals, baseline['cv'], 'lt', 'cv')
    if ca_vals:
        ascii_hist(ca_vals, 0, 1.0, 20,
                   '|corr|（量价相关性绝对值）', baseline['corr'], 'lt')
        suggest(ca_vals, baseline['corr'], 'lt', 'corr')
    if tl_vals:
        ascii_hist(tl_vals, 0, max(0.6, max(tl_vals) * 1.05), 24,
                   'tail_ratio（尾盘 30 分钟量占比）', baseline['tail'], 'gt')
        suggest(tl_vals, baseline['tail'], 'gt', 'tail')
    if fl_vals:
        ascii_hist(fl_vals, 0, max(0.6, max(fl_vals) * 1.05), 24,
                   'flatness（分时均匀度）', baseline['flatness'], 'lt')
        suggest(fl_vals, baseline['flatness'], 'lt', 'flatness')

    # ---------- D. 重投票自洽性 ----------
    print_header('D. 重投票自洽性（基线阈值重放 vs 账本存储判定）')
    ok = bad = 0
    inc_ex = []
    single_hit = {'cv': 0, 'corr': 0, 'tail': 0, 'flatness': 0}
    fake_votes = {2: 0, 3: 0, 4: 0}
    for e in normal:
        cv, corr, tail, flat = (e.get('cv'), e.get('corr'),
                                e.get('tail_ratio'), e.get('flatness'))
        if cv is None and corr is None and tail is None and flat is None:
            continue
        old = VERDICT_MAP.get(e.get('verdict'))
        if old is None:
            continue
        h = []
        if cv is not None and cv < baseline['cv']:
            h.append('cv')
        if corr is not None and abs(corr) < baseline['corr']:
            h.append('corr')
        if tail is not None and tail > baseline['tail']:
            h.append('tail')
        if flat is not None and flat < baseline['flatness']:
            h.append('flatness')
        new = 'real' if not h else ('suspect' if len(h) == 1 else 'fake')
        if new == 'suspect' and len(h) == 1:
            single_hit[h[0]] += 1
        if new == 'fake':
            fake_votes[min(len(h), 4)] += 1
        if new == old:
            ok += 1
        else:
            bad += 1
            if len(inc_ex) < 10:
                inc_ex.append(f"    {e['month']} {e['code']} {e['date']}  "
                              f"账本={LABEL[old]} / 重放={LABEL[new]}  "
                              f"cv={cv} corr={corr} tail={tail} flat={flat}")
    tot_d = ok + bad
    if tot_d:
        msg = f'  一致 {ok}/{tot_d} ({fmt_pct(ok / tot_d)})'
        if bad:
            msg += f'  —— 不一致 {bad} 条（账本漂移 / 手工修改 / 阈值改动史）'
        print(msg)
    if inc_ex:
        print('\n'.join(inc_ex))
    if any(single_hit.values()):
        print('  疑似量化（单票命中）的决定性指标: ' +
              '   '.join(f'{k}={v}' for k, v in single_hit.items()))
    if any(fake_votes.values()):
        print('  量化对倒命中票数分布: ' +
              '   '.join(f'{k}票={v}' for k, v in sorted(fake_votes.items())))
    if v1:
        print('  [说明] v1 账目无 flatness，自动降级为三指标投票（与 v1 逻辑一致）')

    # ---------- E. 阈值扫描 ----------
    if args.sweep:
        print_header('E. 单指标阈值扫描（其余指标固定于基线）')
        for key, cands in SWEEP_CANDIDATES.items():
            field = FIELD_OF[key]
            n_part = sum(1 for e in normal if e.get(field) is not None)
            note = '，仅 v2 账目' if key == 'flatness' else ''
            print(f'\n  {key}（参与账目 {n_part} 条{note}）:')
            print('    阈值    ' + ''.join(f'{LABEL[k]:>10}' for k in ORDER))
            for c in cands:
                th2 = dict(baseline)
                th2[key] = c
                cnt = {k: 0 for k in ORDER}
                for e in normal:
                    cv, corr, tail, flat = (e.get('cv'), e.get('corr'),
                                            e.get('tail_ratio'), e.get('flatness'))
                    if cv is None and corr is None and tail is None and flat is None:
                        continue
                    cnt[revote(cv, corr, tail, flat, th2)] += 1
                tot = sum(cnt.values())
                if not tot:
                    continue
                mark = '   ← 当前' if abs(c - baseline[key]) < 1e-9 else ''
                row = f'    {c:<7g}' + ''.join(
                    f'{fmt_pct(cnt[k] / tot):>10}' for k in ORDER)
                print(row + mark)

    # ---------- F. 新阈值模拟 ----------
    if sim != baseline:
        print_header('F. 新阈值模拟（基线 → 新阈值）')
        print('  新阈值: ' + '   '.join(
            f'{k}={sim[k]}' for k in ('cv', 'corr', 'tail', 'flatness')))
        flip = {a: {b: 0 for b in ORDER} for a in ORDER}
        flip_ex = []
        for e in normal:
            cv, corr, tail, flat = (e.get('cv'), e.get('corr'),
                                    e.get('tail_ratio'), e.get('flatness'))
            if cv is None and corr is None and tail is None and flat is None:
                continue
            old = VERDICT_MAP.get(e.get('verdict'))
            if old is None:
                continue
            new = revote(cv, corr, tail, flat, sim)
            flip[old][new] += 1
            if new != old and len(flip_ex) < 15:
                flip_ex.append(f"    {e['month']} {e['code']} {e['date']}  "
                               f"{LABEL[old]} → {LABEL[new]}  "
                               f"cv={cv} corr={corr} tail={tail} flat={flat}")
        print('\n  翻转矩阵（行=旧判定, 列=新判定）:')
        print('    ' + ' ' * 12 + ''.join(f'{LABEL[b]:>10}' for b in ORDER))
        for a in ORDER:
            print(f"    {LABEL[a]:<12}" + ''.join(f'{flip[a][b]:>10}' for b in ORDER))
        total_n = sum(flip[a][b] for a in ORDER for b in ORDER)
        changed = sum(flip[a][b] for a in ORDER for b in ORDER if a != b)
        print(f'\n  翻转总数: {changed} / {total_n} '
              f'({fmt_pct(changed / total_n if total_n else 0)})')
        if flip_ex:
            print('\n  翻转样本（前 15 条）:')
            print('\n'.join(flip_ex))

    # ---------- G. 月度漂移 ----------
    print_header('G. 月度漂移（指标均值与判定结构）')
    months_sorted = sorted({e['month'] for e in entries})
    print(f"    {'月份':<10}{'n':>5}{'豁免':>5}{'真金':>9}{'疑似':>9}{'对倒':>9}"
          f"{'cv':>9}{'corr':>9}{'tail':>9}{'flat':>9}")
    drift = []
    for m in months_sorted:
        me = [e for e in entries if e['month'] == m]
        mn = [e for e in me if not e.get('limit_status')]
        ex = len(me) - len(mn)
        mixm = {k: 0 for k in ORDER}
        for e in mn:
            kx = VERDICT_MAP.get(e.get('verdict'))
            if kx:
                mixm[kx] += 1
        tot = sum(mixm.values()) or 1
        mcv = [e['cv'] for e in mn if e.get('cv') is not None]
        mcr = [e['corr'] for e in mn if e.get('corr') is not None]
        mtl = [e['tail_ratio'] for e in mn if e.get('tail_ratio') is not None]
        mfl = [e['flatness'] for e in mn if e.get('flatness') is not None]
        print(f"    {m:<10}{len(me):>5}{ex:>5}"
              f"{fmt_pct(mixm['real'] / tot):>9}{fmt_pct(mixm['suspect'] / tot):>9}"
              f"{fmt_pct(mixm['fake'] / tot):>9}"
              f"{fmt_mean(mcv):>9}{fmt_mean(mcr):>9}{fmt_mean(mtl):>9}{fmt_mean(mfl):>9}")
        drift.append({'month': m, 'n': len(me), 'exempt': ex, 'mix': dict(mixm),
                      'mean': {'cv': rnd(mcv), 'corr': rnd(mcr),
                               'tail_ratio': rnd(mtl), 'flatness': rnd(mfl)}})

    # ---------- H. 按股票汇总 ----------
    if args.top:
        print_header(f'H. 按股票汇总（前 {args.top}，按账目数降序）')
        by_code = {}
        for e in entries:
            by_code.setdefault(e['code'], []).append(e)
        rows = sorted(by_code.items(), key=lambda kv: -len(kv[1]))[:args.top]
        print(f"    {'代码':<11}{'n':>5}{'豁免':>5}{'真金':>9}{'疑似':>9}{'对倒':>9}"
              f"{'cv':>9}{'flat':>9}")
        for code, lst in rows:
            mn = [e for e in lst if not e.get('limit_status')]
            ex = len(lst) - len(mn)
            mixc = {k: 0 for k in ORDER}
            for e in mn:
                kx = VERDICT_MAP.get(e.get('verdict'))
                if kx:
                    mixc[kx] += 1
            tot = sum(mixc.values()) or 1
            mcv = [e['cv'] for e in mn if e.get('cv') is not None]
            mfl = [e['flatness'] for e in mn if e.get('flatness') is not None]
            print(f"    {code:<11}{len(lst):>5}{ex:>5}"
                  f"{fmt_pct(mixc['real'] / tot):>9}{fmt_pct(mixc['suspect'] / tot):>9}"
                  f"{fmt_pct(mixc['fake'] / tot):>9}"
                  f"{fmt_mean(mcv):>9}{fmt_mean(mfl):>9}")

    # ---------- I. JSON 导出 ----------
    if args.json:
        report = {
            'generated_at': datetime.now(CST).isoformat(timespec='seconds'),
            'ledger_files': [f.name for f in files],
            'counts': {'total': len(entries), 'v2': len(v2), 'v1': len(v1),
                       'exempt': len(exempt), 'normal': len(normal)},
            'baseline_thresh': baseline,
            'sim_thresh': sim,
            'stored_mix': dict(mix),
            'snapshot_integrity': {
                'flatness_ok': checked_f - bad_f, 'flatness_bad': bad_f,
                'tail_ok': checked_t - bad_t, 'tail_bad': bad_t,
                'cv_calibration_k': round(k_med, 3) if k_med else None},
            'indicators': {
                'cv': ind_report(cv_vals, baseline['cv'], 'lt'),
                'corr_abs': ind_report(ca_vals, baseline['corr'], 'lt'),
                'tail_ratio': ind_report(tl_vals, baseline['tail'], 'gt'),
                'flatness': ind_report(fl_vals, baseline['flatness'], 'lt')},
            'self_consistency': {'ok': ok, 'bad': bad},
            'monthly': drift,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                             encoding='utf-8')
        print(f'\n[INFO] JSON 摘要已导出: {args.json}')

    print('\n' + '─' * 66)
    print('  使用指引:')
    print('   ① C 节看各指标阈值命中率是否落在目标区间（经验上 10%-30% 较有区分度）；')
    print('   ② E 节扫描候选阈值，F 节用 --thresh 预览翻转规模；')
    print('   ③ 定稿后修改 record_truth.py 头部 THRESH 字典并提交；历史判定可凭')
    print('      vprofile_24 快照离线重算，无需重拉 1 分钟数据。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
