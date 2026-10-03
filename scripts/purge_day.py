#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
purge_day.py — 删除指定日期的账目（供重新分析用）

用法（在电脑上运行）：
    python scripts/purge_day.py 2026-09 2026-09-30
        → 删除 2026-09-30 当天全部股票的账目

    python scripts/purge_day.py 2026-09 2026-09-30 sh601138 sz002156
        → 只删除指定股票当天的账目

删除后，该日期会在下次运行 record_truth.py 时用新数据重新分析。
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LEDGER_DIR = BASE_DIR / 'data' / 'analysis' / 'truth_ledger'


def main():
    if len(sys.argv) < 3:
        print('用法: python scripts/purge_day.py 月份 日期 [代码1 代码2 ...]')
        print('示例: python scripts/purge_day.py 2026-09 2026-09-30')
        return 1
    month, date = sys.argv[1], sys.argv[2]
    codes = set(sys.argv[3:]) or None

    p = LEDGER_DIR / f'{month}.json'
    if not p.exists():
        print(f'账本不存在: {p}')
        return 1

    data = json.loads(p.read_text(encoding='utf-8'))
    n = 0
    for code in list(data):
        if codes and code not in codes:
            continue
        if date in data.get(code, {}):
            del data[code][date]
            n += 1
        if code in data and not data[code]:
            del data[code]
    p.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')),
                 encoding='utf-8')
    print(f'已删除 {n} 条 {date} 账目')
    return 0


if __name__ == '__main__':
    sys.exit(main())
