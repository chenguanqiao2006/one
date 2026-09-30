#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 研判模块 - 调用 Agnes AI 生成个股研报
日期逻辑：从第一只持仓股的K线数据中提取最新日期，确保与报告脚本读的日期一致
"""

import os
import json
import time
import datetime
import requests

# ============================================================
# 配置区
# ============================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINRATE_PATH = os.path.join(BASE_DIR, "data", "analysis", "winrate.json")
KLINE_DIR = os.path.join(BASE_DIR, "data", "kline")
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "analysis")

# Agnes AI 配置
AGNES_API_URL = "https://apihub.agnes-ai.cn/v1/chat/completions"
AGNES_MODEL = "agnes-3.0-flash"
API_KEY_ENV = "AGNES_API_KEY"

# 你的持仓股（包含市场前缀，方便拼凑文件路径）
HOLDINGS = [
    {"market": "sh", "code": "600584", "name": "长电科技"},
    {"market": "sz", "code": "002156", "name": "通富微电"},
    {"market": "sh", "code": "603283", "name": "赛腾股份"},
    {"market": "sz", "code": "300394", "name": "天孚通信"},
    {"market": "sh", "code": "601138", "name": "工业富联"},
    {"market": "sh", "code": "601231", "name": "环旭电子"},
    {"market": "sz", "code": "300476", "name": "胜宏科技"},
    {"market": "sh", "code": "603516", "name": "淳中科技"},
]

# ============================================================
# 【统一配置接入】优先使用 config.py 的持仓股（字典格式）；缺失则用内置默认
# 为什么这么做：AI 研判与报告/回测共用同一份持仓，改 config.py 一处即可，
# 避免换股时漏改本脚本；try/except 保证 config 不在也能运行。
# ============================================================
try:
    from config import HOLDINGS_DICT as _CFG_HOLDINGS
    HOLDINGS = list(_CFG_HOLDINGS)
    print("[config] 已从 config.py 加载持仓股（字典格式）")
except Exception as _cfg_err:
    print(f"[config] 未加载 config.py，使用脚本内置持仓（{_cfg_err}）")

# ============================================================
# 工具函数
# ============================================================
def flatten_winrate(node, ctx=None, acc=None):
    """递归展平嵌套的胜率JSON结构"""
    if ctx is None: ctx = {}
    if acc is None: acc = []
    
    if isinstance(node, dict):
        local = dict(ctx)
        rate = None
        for k, v in node.items():
            lk = str(k).lower()
            if isinstance(v, (str, int, float)):
                if any(t in lk for t in ("position", "位置", "loc", "level")):
                    local["位置"] = str(v)
                elif any(t in lk for t in ("trend", "趋势", "direction")):
                    local["趋势"] = str(v)
                elif any(t in lk for t in ("signal", "信号", "pattern", "形态", "name", "名称")):
                    local["信号"] = str(v)
            if any(t in lk for t in ("win_rate", "winrate", "胜率", "rate")):
                rate = float(v) if isinstance(v, (int, float)) else None
                if rate is None and isinstance(v, str) and v.endswith("%"):
                    try: rate = float(v.rstrip("%"))
                    except: pass
        
        if rate is not None:
            entry = dict(local)
            entry["胜率"] = rate
            acc.append(entry)
        
        for v in node.values():
            if isinstance(v, (dict, list)):
                flatten_winrate(v, local, acc)
    elif isinstance(node, list):
        for item in node:
            flatten_winrate(item, ctx, acc)
    return acc

def load_kline(market, code):
    """读取本地 K 线数据（支持子目录路径和二维数组格式）"""
    # 你真实的数据路径是 data/kline/sh/sh600584.json
    path = os.path.join(KLINE_DIR, market, f"{market}{code}.json")
    if not os.path.exists(path):
        # 备用路径（预防旧数据残留）
        path = os.path.join(KLINE_DIR, f"{market}{code}.json")
        if not os.path.exists(path):
            return None, None

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    name = data.get("name", code)
    klines = data.get("klines", [])
    if len(klines) < 2:
        return None, None
    
    # 你的数据是二维数组: [日期, 开盘, 收盘, 最高, 最低, 成交量]
    return klines, name

def extract_features(klines):
    """提取最新一天特征"""
    today = klines[-1]
    yesterday = klines[-2]
    
    date_str = today[0]
    close = float(today[2])
    open_p = float(today[1])
    vol = float(today[5])
    prev_close = float(yesterday[2])
    prev_vol = float(yesterday[5])
    
    pct_chg = (close - prev_close) / prev_close * 100 if prev_close else 0
    vol_ratio = vol / prev_vol if prev_vol else 0
    
    # 简单量能形态判断
    if pct_chg > 0 and vol_ratio >= 1.8:
        vol_state = "倍量柱（放量上涨）"
    elif pct_chg > 0 and vol_ratio <= 0.8:
        vol_state = "缩量柱（缩量上涨）"
    elif pct_chg < 0 and vol_ratio >= 1.8:
        vol_state = "放量下跌"
    else:
        vol_state = "平量柱（普通量能）"
        
    # 趋势（用前20日均线判断）
    closes = [float(k[2]) for k in klines[-20:]]
    ma20 = sum(closes) / len(closes) if len(closes) == 20 else close
    trend = "上升趋势" if close > ma20 else "下降趋势"
    
    return {
        "date": date_str,
        "close": round(close, 2),
        "pct_chg": round(pct_chg, 2),
        "vol_ratio": round(vol_ratio, 2),
        "vol_state": vol_state,
        "trend": trend,
        "ma20": round(ma20, 2)
    }

def match_winrate(entries, trend):
    """匹配胜率数据（简化匹配逻辑，优先匹配趋势）"""
    matched = []
    for e in entries:
        e_trend = str(e.get("趋势", ""))
        if e_trend and trend != "未知" and e_trend not in trend:
            continue
        matched.append(e)
    
    if not matched:
        return None, []
    
    rates = [e["胜率"] for e in matched if e.get("胜率") is not None]
    if not rates:
        return None, []
    
    signals = list(set([e.get("信号") for e in matched if e.get("信号")]))
    return round(sum(rates)/len(rates), 2), signals

# ============================================================
# 核心逻辑
# ============================================================
def call_agnes_api(prompt):
    """调用 Agnes AI"""
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        return "【未配置 AGNES_API_KEY 环境变量，跳过 AI 研判】"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": AGNES_MODEL,
        "messages": [
            {"role": "system", "content": "你是一名严谨的量化投研助手，输出客观、克制、可读的技术研判。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 300,
        "stream": False
    }
    
    try:
        resp = requests.post(AGNES_API_URL, json=payload, headers=headers, timeout=40)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"【AI请求失败: {str(e)[:50]}】"

def main():
    print("=" * 50)
    print("开始生成 AI 个股研判...")
    print("=" * 50)

    # ========== 关键：从第一只持仓股提取最新日期 ==========
    today = None
    first_stock = HOLDINGS[0]
    klines, _ = load_kline(first_stock["market"], first_stock["code"])
    if klines:
        # klines[-1][0] 类似 "2026-09-22" 或 "20260922"
        raw_date = str(klines[-1][0]).strip()
        today = raw_date.replace("-", "")
        if len(today) > 8:
            today = today[:8]
    if not today:
        today = datetime.date.today().strftime("%Y%m%d")  # 兜底
    print(f"[AI研判] 数据日期: {today}")

    # 1. 加载胜率
    entries = []
    if os.path.exists(WINRATE_PATH):
        with open(WINRATE_PATH, "r", encoding="utf-8") as f:
            entries = flatten_winrate(json.load(f))
        print(f"[AI研判] 信号胜率条目: {len(entries)} 条")
    
    results = {}
    for stock in HOLDINGS:
        market, code, name = stock["market"], stock["code"], stock["name"]
        print(f"[AI研判] 处理 {name} ({market}{code}) ...")
        
        klines, real_name = load_kline(market, code)
        if not klines:
            results[f"{market}{code}"] = {"error": "未找到K线数据"}
            continue
            
        try:
            # 2. 提取特征
            features = extract_features(klines)
            
            # 3. 匹配胜率
            winrate, signals = match_winrate(entries, features["trend"])
            
            # 4. 拼凑 Prompt
            winrate_text = f"命中信号: {', '.join(signals)}, 历史同类胜率约 {winrate}%" if winrate else "暂无匹配胜率数据"
            prompt = f"""
请对持仓个股 {real_name}({market}{code}) 做客观研判。

【最新交易日特征】
- 日期: {features['date']}
- 最新收盘价: {features['close']} 元
- 当日涨跌幅: {features['pct_chg']}%
- 量能形态: {features['vol_state']}（量比 {features['vol_ratio']} 倍）
- 当前趋势: {features['trend']}（MA20: {features['ma20']} 元）
- 参考信号胜率: {winrate_text}

请给出 100 字以内的客观研判，包含当前多空状态、量价关系与后续关注要点。
要求：客观中立，不构成投资建议，仅基于数据描述，指出量价配合情况和需要注意的风险点。
"""
            # 5. 调用 AI
            comment = call_agnes_api(prompt)
            results[f"{market}{code}"] = {
                "name": real_name,
                "features": features,
                "winrate": winrate,
                "signals": signals,
                "ai_comment": comment
            }
            print(f"  -> {comment[:50]}...")
            
            # 防频限
            time.sleep(1.5)
            
        except Exception as e:
            results[f"{market}{code}"] = {"error": str(e)}
            print(f"  -> 失败: {e}")

    # 6. 保存结果
    output_path = os.path.join(OUTPUT_DIR, f"ai_comment_{today}.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    payload = {
        "date": today,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": AGNES_MODEL,
        "stocks": results
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ AI研判已保存至: {output_path}")

if __name__ == "__main__":
    main()