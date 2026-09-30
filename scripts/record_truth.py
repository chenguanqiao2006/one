#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真假量柱账本记录器 (record_truth.py)
=================================================
版本: v1.1 (2026-09-23)
职责: 读取1分钟数据 → 计算真假特征 → 写入按月分片账本 → 清理原始数据

上游: daily-quote.yml（每天20:00自动触发）
下游: backtest_4d.py（回测时优先查账本）
输出: data/analysis/truth_ledger/YYYY-MM.json


╔══════════════════════════════════════════════════════════════╗
║           ★★★ 核心资产写入协议 (不可违背) ★★★                ║
╠══════════════════════════════════════════════════════════════╣
║  1. 本脚本是 truth_ledger/ 目录的【唯一写入者】。            ║
║  2. 写入策略必须严格遵守【只追加，不覆盖】原则。             ║
║  3. 账本数据代表【历史真相】，严禁任何脚本对其进行：         ║
║     - 修改 (modify)                                          ║
║     - 删除 (delete)                                          ║
║     - 回滚 (rollback)                                        ║
║     - 清洗 (clean)                                           ║
║  4. 禁止任何 AI Agent（如 OpenMinis/MonkeyCode）直接读写      ║
║     或 git push 本目录。账本只能通过本脚本的【确定性逻辑】    ║
║     写入，绝不能交给大模型的"灵活处理"。                     ║
║  5. 如发现账本异常，唯一正确的做法是【回滚 Git 提交】，       ║
║     而不是手动修改 JSON 文件。                               ║
║  6. 1分钟数据"用一天少一天"：错过记账窗口，历史真相将        ║
║     永久丢失，无法补录。                                     ║
╚══════════════════════════════════════════════════════════════╝


核心逻辑:
  1. 按日期分组1分钟K线
  2. 只记账"完整"交易日（≥230根）
  3. 跳过盘中不完整数据
  4. 按月分片存储
  5. 记账后删除原始1分钟数据

判定规则:
  3个指标（CV、量价相关、尾盘占比）命中≥2个 → 量化对倒
  命中1个 → 疑似量化
  0个 → 真金白银

时间规则（北京时间）:
  - 盘中：数据不完整 → 自动跳过
  - 盘后：数据完整 → 正常记账
  - 周末/节假日：接口返回最后交易日数据 → 自动识别
"""
import json
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ============================================================
# 路径配置
# ============================================================
DATA_DIR = Path(__file__).parent.parent / "data"
KLINE_1MIN_DIR = DATA_DIR / "kline_1min"
LEDGER_DIR = DATA_DIR / "analysis" / "truth_ledger"

# 完整交易日所需的1分钟K线数量
MIN_FULL_DAY_BARS = 230


# ============================================================
# 看门狗：在账本目录写入只读声明文件
# ============================================================
def write_watchdog():
    """在账本目录写入 DO_NOT_MODIFY.md，作为对任何外部读写者的警告。"""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    watchdog_path = LEDGER_DIR / "DO_NOT_MODIFY.md"
    content = """# ⚠️ 请勿手动修改此目录 ⚠️

本目录为量化系统的【历史账本】。账本由 `scripts/record_truth.py` 
以【只追加，不覆盖】的方式写入，代表历史真相。

## 严禁操作
- ❌ 手动修改任意 .json 文件
- ❌ 删除任意 .json 文件
- ❌ 重命名或移动文件
- ❌ 通过 AI Agent（如 OpenMinis/MonkeyCode）直接写入或 git push

## 如发现异常
- ✅ 唯一正确的做法：回滚 Git 提交 (git revert)
- ✅ 联系脚本维护者，检查 record_truth.py 的写入逻辑

## 为什么如此严格
1分钟数据"用一天少一天"。错过记账窗口，历史真相将永久丢失。
账本一旦被污染，所有回测胜率、真假判定都会失真。
"""
    # 只在文件不存在时写入，避免每天重复
    if not watchdog_path.exists():
        with open(watchdog_path, 'w', encoding='utf-8') as f:
            f.write(content)


def extract_date_from_timestamp(ts):
    """从时间戳提取日期（YYYY-MM-DD）"""
    s = str(ts).strip()
    if not s:
        return None
    if '-' in s:
        return s[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def analyze_1min_volatility(day_klines):
    """输入单日完整的1分钟K线，返回真假特征"""
    if not day_klines or len(day_klines) < MIN_FULL_DAY_BARS:
        return None, None, None, None, None, None

    volumes, closes = [], []
    for k in day_klines:
        try:
            if len(k) > 5:
                volumes.append(float(k[5]))
                closes.append(float(k[2]))
        except Exception:
            continue

    if len(volumes) < 30:
        return None, None, None, None, None, None

    vol_mean = np.mean(volumes)
    vol_std = np.std(volumes)
    cv = vol_std / vol_mean if vol_mean > 0 else 999

    if len(closes) == len(volumes) and len(closes) > 10:
        price_changes = np.diff(closes)
        vol_changes = np.diff(volumes)
        if np.std(price_changes) > 0 and np.std(vol_changes) > 0:
            corr = float(np.corrcoef(price_changes, vol_changes)[0, 1])
            if np.isnan(corr):
                corr = 0.0
        else:
            corr = 0.0
    else:
        corr = 0.0

    tail_vol = sum(volumes[-30:])
    total_vol = sum(volumes)
    tail_ratio = tail_vol / total_vol if total_vol > 0 else 0

    quant_score = 0
    if cv < 0.5: quant_score += 1
    if abs(corr) < 0.3: quant_score += 1
    if tail_ratio > 0.3: quant_score += 1

    if quant_score >= 2:
        verdict = "量化对倒"
        is_real = False
    elif quant_score == 1:
        verdict = "疑似量化"
        is_real = None
    else:
        verdict = "真金白银"
        is_real = True

    cv_contrib = 70 if cv < 0.5 else (40 if cv < 1.0 else 10)
    corr_contrib = 60 if abs(corr) < 0.2 else (30 if abs(corr) < 0.5 else 10)
    tail_contrib = 70 if tail_ratio > 0.4 else (40 if tail_ratio > 0.2 else 10)
    quant_pct = (cv_contrib + corr_contrib + tail_contrib) / 3

    return (verdict, is_real, round(quant_pct, 1),
            round(cv, 2), round(corr, 2), round(tail_ratio, 3))


def get_ledger_path(date_str):
    """根据日期返回账本文件路径（按月分片）"""
    month = date_str[:7]
    return LEDGER_DIR / f"{month}.json"


def load_ledger(date_str):
    """加载指定月份的账本"""
    path = get_ledger_path(date_str)
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_ledger(date_str, ledger):
    """安全保存账本（原子操作，防止中途崩溃损坏文件）"""
    path = get_ledger_path(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix('.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def main():
    # ============ 写入看门狗声明 ============
    write_watchdog()

    bj_now = datetime.now(timezone.utc) + timedelta(hours=8)
    print(f"[账本] 北京时间: {bj_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[账本] 完整交易日标准: ≥{MIN_FULL_DAY_BARS}根1分钟K线")
    print(f"[账本] 写入协议: 只追加，不覆盖")

    if not KLINE_1MIN_DIR.exists():
        print(f"[账本] 1分钟数据目录不存在，跳过")
        return

    files = list(KLINE_1MIN_DIR.glob("*.json"))
    if not files:
        print("[账本] 无1分钟数据文件，跳过")
        return

    print(f"[账本] 待处理 {len(files)} 个文件\n")

    monthly_ledgers = {}
    failed_files = {}   # 记录"含完整交易日却记账失败"的文件：f -> [失败日期,...]，这些文件稍后保留不删
    updated = 0
    skipped_incomplete = 0
    skipped_exists = 0

    for f in files:
        code = f.stem
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
            klines = data.get('klines', [])
            if not klines:
                continue

            by_date = {}
            for k in klines:
                d = extract_date_from_timestamp(k[0])
                if d:
                    by_date.setdefault(d, []).append(k)

            for d, day_klines in sorted(by_date.items()):
                if len(day_klines) < MIN_FULL_DAY_BARS:
                    skipped_incomplete += 1
                    continue

                month = d[:7]
                if month not in monthly_ledgers:
                    monthly_ledgers[month] = load_ledger(d)

                ledger = monthly_ledgers[month]

                # ★★★ 只追加，不覆盖：已存在的日期直接跳过 ★★★
                if code in ledger and d in ledger[code]:
                    skipped_exists += 1
                    continue

                res = analyze_1min_volatility(day_klines)
                if res[0] is None:
                    # 完整交易日却分析失败：记入失败清单，稍后保留该文件，防止"没记账又被删"丢真相
                    failed_files.setdefault(f, []).append(d)
                    continue

                verdict, is_real, quant_pct, cv, corr, tail = res

                if code not in ledger:
                    ledger[code] = {}
                ledger[code][d] = {
                    "is_real": is_real,
                    "verdict": verdict,
                    "quant_pct": quant_pct,
                    "cv": cv,
                    "corr": corr,
                    "tail_ratio": tail,
                }
                updated += 1
                if updated <= 20 or updated % 500 == 0:
                    print(f"  {code} {d} ✅ {verdict} (量化{quant_pct}%)")

        except Exception as e:
            print(f"  {code} 处理失败: {e}")
            # 整个文件处理异常也视为失败，保留文件以便次日重试，不直接删除
            failed_files.setdefault(f, []).append(f"异常:{type(e).__name__}")

    for month, ledger in monthly_ledgers.items():
        save_ledger(f"{month}-01", ledger)
        total = sum(len(v) for v in ledger.values())
        print(f"[账本] {month}.json: {len(ledger)} 只股票, {total} 条记录")

    print(f"\n[统计] 更新: {updated} 条，跳过不完整: {skipped_incomplete} 条，已存在: {skipped_exists} 条")

    deleted = 0
    kept = 0
    for f in files:
        # 安全删除：仅当该文件没有"完整交易日记账失败"时才删；
        # 失败文件保留并告警，避免"既没记账、文件也被删"导致历史真相永久丢失、无法补录
        if failed_files.get(f):
            kept += 1
            print(f"[清理] ⚠️ 保留 {f.name}，完整交易日记账失败、需人工排查: {failed_files[f]}")
            continue
        try:
            f.unlink()
            deleted += 1
        except Exception as e:
            print(f"[清理] 删除失败 {f.name}: {e}")
    print(f"[清理] 已删除 {deleted} 个1分钟原始数据文件，保留 {kept} 个待排查文件")


if __name__ == "__main__":
    main()