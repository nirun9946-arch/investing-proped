# -*- coding: utf-8 -*-
"""
Investing Pro — Technical Stock Analyzer & Alert System
========================================================
วิเคราะห์หุ้นเชิงเทคนิค + แจ้งเตือนสัญญาณสำคัญ
ข้อมูลจาก Yahoo Finance (yfinance) — รองรับหุ้น US และหุ้นไทย (.BK)

การใช้งาน:
    python investing_pro.py                  สแกน watchlist ทั้งหมด 1 รอบ
    python investing_pro.py --ticker NVDA    วิเคราะห์เจาะลึกตัวเดียว
    python investing_pro.py --watch          รันวนต่อเนื่อง แจ้งเตือนเมื่อเกิดสัญญาณ
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "alert_state.json")
REPORT_DIR = os.path.join(BASE_DIR, "reports")


# ----------------------------------------------------------------------
# Config & state
# ----------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Indicators (คำนวณเองด้วย pandas — สูตรมาตรฐาน)
# ----------------------------------------------------------------------
def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(close, fast=12, slow=26, signal=9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(close, period=20, num_std=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + num_std * std, mid, mid - num_std * std


def atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def support_resistance(df, lookback=60, window=5):
    """หาแนวรับ/แนวต้านจาก swing highs/lows ล่าสุด"""
    recent = df.tail(lookback)
    highs, lows = [], []
    h, l = recent["High"], recent["Low"]
    for i in range(window, len(recent) - window):
        seg_h = h.iloc[i - window : i + window + 1]
        seg_l = l.iloc[i - window : i + window + 1]
        if h.iloc[i] == seg_h.max():
            highs.append(float(h.iloc[i]))
        if l.iloc[i] == seg_l.min():
            lows.append(float(l.iloc[i]))
    price = float(df["Close"].iloc[-1])
    resistance = min([x for x in highs if x > price], default=None)
    support = max([x for x in lows if x < price], default=None)
    return support, resistance


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------
def fetch(ticker, period="1y", interval="1d"):
    tk = yf.Ticker(ticker)
    df = tk.history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty or len(df) < 60:
        raise ValueError(f"ข้อมูลไม่พอสำหรับ {ticker} (ได้ {0 if df is None else len(df)} แท่ง)")
    return tk, df


def fundamentals(tk, price):
    """ดึงข้อมูลพื้นฐาน: ราคาปิด, P/E, เป้านักวิเคราะห์ + ประเมินความคุ้มค่าของราคา"""
    out = {"prev_close": None, "pe": None, "fwd_pe": None, "peg": None,
           "target": None, "upside": None, "w52h": None, "w52l": None,
           "market_state": None, "value_label": "N/A",
           "value_desc": "ไม่มีข้อมูลพื้นฐาน",
           "pre_price": None, "pre_chg": None, "post_price": None, "post_chg": None}
    try:
        info = tk.info or {}
    except Exception:
        return out

    out["prev_close"] = info.get("previousClose")

    # ราคานอกเวลาทำการ: ก่อนเปิด (pre-market) / หลังปิด-ข้ามคืน (post-market)
    pre = info.get("preMarketPrice")
    post = info.get("postMarketPrice")
    if pre and price:
        out["pre_price"] = float(pre)
        out["pre_chg"] = (float(pre) / price - 1) * 100
    if post and price:
        out["post_price"] = float(post)
        out["post_chg"] = (float(post) / price - 1) * 100
    out["pe"] = info.get("trailingPE")
    out["fwd_pe"] = info.get("forwardPE")
    peg = info.get("trailingPegRatio") or info.get("pegRatio")
    out["peg"] = float(peg) if peg else None
    out["target"] = info.get("targetMeanPrice")
    out["w52h"] = info.get("fiftyTwoWeekHigh")
    out["w52l"] = info.get("fiftyTwoWeekLow")
    out["market_state"] = info.get("marketState")
    if out["target"]:
        out["upside"] = (out["target"] / price - 1) * 100

    pe = out["fwd_pe"] or out["pe"]
    qt = info.get("quoteType", "")
    parts = []
    if qt == "ETF":
        out["value_label"] = "ETF"
        parts.append("กองทุน ETF — ไม่มีค่า P/E ประเมินตามแนวโน้มดัชนี/สินทรัพย์อ้างอิง")
    elif pe is None:
        out["value_label"] = "N/A"
        parts.append("ไม่มีค่า P/E (กำไรติดลบหรือไม่มีข้อมูล) — ประเมินมูลค่าด้วย P/E ไม่ได้")
    else:
        if pe < 12:
            out["value_label"] = "ถูก"
            parts.append(f"P/E {pe:.1f} ต่ำกว่าค่าเฉลี่ยตลาด (~20) มาก")
        elif pe < 22:
            out["value_label"] = "เหมาะสม"
            parts.append(f"P/E {pe:.1f} ใกล้ค่าเฉลี่ยตลาด (~20)")
        elif pe < 35:
            out["value_label"] = "ค่อนข้างแพง"
            parts.append(f"P/E {pe:.1f} สูงกว่าค่าเฉลี่ยตลาด — ต้องโตให้สมราคา")
        else:
            out["value_label"] = "แพง"
            parts.append(f"P/E {pe:.1f} สูงมาก — ราคาสะท้อนความคาดหวังการเติบโตสูง")
        if out["peg"]:
            if out["peg"] < 1:
                parts.append(f"PEG {out['peg']:.2f} ถูกเมื่อเทียบอัตราการเติบโต")
            elif out["peg"] > 2:
                parts.append(f"PEG {out['peg']:.2f} แพงเมื่อเทียบอัตราการเติบโต")
    if out["upside"] is not None:
        parts.append(f"เป้านักวิเคราะห์เฉลี่ย {out['target']:,.2f} ({out['upside']:+.1f}% จากราคาปัจจุบัน)")
    out["value_desc"] = " | ".join(parts)
    return out


# ----------------------------------------------------------------------
# Analysis engine
# ----------------------------------------------------------------------
def analyze(ticker, cfg):
    s = cfg["settings"]
    tk, df = fetch(ticker, s.get("period", "1y"), s.get("interval", "1d"))

    close = df["Close"]
    df["EMA20"] = ema(close, 20)
    df["EMA50"] = ema(close, 50)
    df["EMA200"] = ema(close, 200)
    df["RSI"] = rsi(close)
    df["MACD"], df["MACD_SIG"], df["MACD_HIST"] = macd(close)
    df["BB_UP"], df["BB_MID"], df["BB_LOW"] = bollinger(close)
    df["ATR"] = atr(df)
    df["VOL_AVG20"] = df["Volume"].rolling(20).mean()

    last, prev = df.iloc[-1], df.iloc[-2]
    price = float(last["Close"])
    support, resistance = support_resistance(df)

    signals = []   # (name, direction, weight, description)
    score = 0

    def add(name, direction, weight, desc):
        nonlocal score
        signals.append({"name": name, "dir": direction, "weight": weight, "desc": desc})
        score += weight if direction == "bull" else -weight

    # --- Trend structure ---
    if price > last["EMA200"]:
        add("Above EMA200", "bull", 2, "ราคายืนเหนือ EMA200 = แนวโน้มใหญ่ยังเป็นขาขึ้น")
    else:
        add("Below EMA200", "bear", 2, "ราคาต่ำกว่า EMA200 = แนวโน้มใหญ่เป็นขาลง")

    if last["EMA20"] > last["EMA50"]:
        add("EMA20>EMA50", "bull", 1, "แนวโน้มระยะสั้น-กลางเป็นบวก")
    else:
        add("EMA20<EMA50", "bear", 1, "แนวโน้มระยะสั้น-กลางเป็นลบ")

    # --- Crosses (เหตุการณ์ ณ แท่งล่าสุด = สัญญาณแจ้งเตือน) ---
    events = []
    if prev["EMA50"] <= prev["EMA200"] and last["EMA50"] > last["EMA200"]:
        add("Golden Cross", "bull", 3, "EMA50 ตัดขึ้น EMA200 — สัญญาณขาขึ้นระยะยาว")
        events.append("golden_cross")
    if prev["EMA50"] >= prev["EMA200"] and last["EMA50"] < last["EMA200"]:
        add("Death Cross", "bear", 3, "EMA50 ตัดลง EMA200 — สัญญาณขาลงระยะยาว")
        events.append("death_cross")
    if prev["MACD"] <= prev["MACD_SIG"] and last["MACD"] > last["MACD_SIG"]:
        add("MACD Bullish Cross", "bull", 2, "MACD ตัดขึ้นเส้นสัญญาณ — โมเมนตัมกลับเป็นบวก")
        events.append("macd_bull")
    if prev["MACD"] >= prev["MACD_SIG"] and last["MACD"] < last["MACD_SIG"]:
        add("MACD Bearish Cross", "bear", 2, "MACD ตัดลงเส้นสัญญาณ — โมเมนตัมกลับเป็นลบ")
        events.append("macd_bear")

    # --- RSI ---
    rsi_now = float(last["RSI"])
    if rsi_now <= s["rsi_oversold"]:
        add("RSI Oversold", "bull", 2, f"RSI {rsi_now:.1f} เข้าเขต oversold — ลุ้นเด้ง/จุดสะสม")
        events.append("rsi_oversold")
    elif rsi_now >= s["rsi_overbought"]:
        add("RSI Overbought", "bear", 2, f"RSI {rsi_now:.1f} เข้าเขต overbought — ระวังแรงขาย")
        events.append("rsi_overbought")
    elif rsi_now > 50:
        add("RSI>50", "bull", 1, f"RSI {rsi_now:.1f} ฝั่งกระทิง")
    else:
        add("RSI<50", "bear", 1, f"RSI {rsi_now:.1f} ฝั่งหมี")

    # --- Breakout 20 วัน ---
    high20 = float(df["High"].iloc[-21:-1].max())
    low20 = float(df["Low"].iloc[-21:-1].min())
    if price > high20:
        add("Breakout 20D High", "bull", 3, f"ราคาทะลุ high 20 วัน ({high20:,.2f})")
        events.append("breakout_high")
    if price < low20:
        add("Breakdown 20D Low", "bear", 3, f"ราคาหลุด low 20 วัน ({low20:,.2f})")
        events.append("breakdown_low")

    # --- Bollinger ---
    if price <= last["BB_LOW"]:
        add("Touch Lower BB", "bull", 1, "ราคาแตะขอบล่าง Bollinger — oversold ระยะสั้น")
    if price >= last["BB_UP"]:
        add("Touch Upper BB", "bear", 1, "ราคาแตะขอบบน Bollinger — ตึงตัวระยะสั้น")

    # --- Volume ---
    vol_ratio = float(last["Volume"] / last["VOL_AVG20"]) if last["VOL_AVG20"] else 1.0
    if vol_ratio >= s["volume_spike_factor"]:
        direction = "bull" if last["Close"] >= last["Open"] else "bear"
        add("Volume Spike", direction, 2,
            f"วอลุ่ม {vol_ratio:.1f} เท่าของค่าเฉลี่ย 20 วัน (แท่ง{'เขียว' if direction=='bull' else 'แดง'})")
        events.append("vol_spike_" + direction)

    # --- Verdict ---
    if score >= 6:
        verdict = "STRONG BUY SIGNAL"
    elif score >= 3:
        verdict = "BUY / ACCUMULATE"
    elif score <= -6:
        verdict = "STRONG SELL SIGNAL"
    elif score <= -3:
        verdict = "SELL / REDUCE"
    else:
        verdict = "HOLD / WAIT"

    max_score = sum(x["weight"] for x in signals)
    confidence = abs(score) / max_score * 100 if max_score else 0

    atr_now = float(last["ATR"])
    fund = fundamentals(tk, price)
    result = {
        **fund,
        "ticker": ticker,
        "price": price,
        "change_pct": (price / float(prev["Close"]) - 1) * 100,
        "rsi": rsi_now,
        "ema20": float(last["EMA20"]),
        "ema50": float(last["EMA50"]),
        "ema200": float(last["EMA200"]),
        "macd_hist": float(last["MACD_HIST"]),
        "support": support,
        "resistance": resistance,
        "atr": atr_now,
        "stop_suggest": price - 2 * atr_now,
        "target_suggest": price + 3 * atr_now,
        "vol_ratio": vol_ratio,
        "score": score,
        "confidence": confidence,
        "verdict": verdict,
        "signals": signals,
        "events": events,
        "asof": str(df.index[-1].date()),
    }
    result.update(make_advice(result))
    return result


def make_advice(r):
    """สรุปคำแนะนำ: ควรซื้อไหม — รวมสัญญาณเทคนิค + ความคุ้มค่า + เป้านักวิเคราะห์"""
    score = r["score"]
    val = r["value_label"]
    upside = r.get("upside")
    reasons = []

    if score >= 4:
        if val in ("แพง", "ค่อนข้างแพง"):
            label, tone = "ซื้อได้ แต่แบ่งไม้", "warn"
            reasons.append(f"เทคนิคแข็งแรง (score +{score}) แต่มูลค่า{val} — ไม่ควรซื้อไม้เดียวหมด ทยอยเข้าเป็นส่วนๆ")
        else:
            label, tone = "ควรซื้อ / ทยอยสะสม", "buy"
            reasons.append(f"สัญญาณเทคนิคเป็นบวกชัดเจน (score +{score}) และมูลค่าไม่แพง")
    elif score >= 1:
        label, tone = "รอจังหวะย่อ", "wait"
        reasons.append(f"แนวโน้มเอียงบวกแต่ยังไม่แรงพอ (score +{score}) — รอราคาย่อใกล้แนวรับแล้วค่อยเข้า จะได้ต้นทุนดีกว่า")
    elif score >= -3:
        label, tone = "ยังไม่ควรซื้อ", "avoid"
        reasons.append(f"สัญญาณเทคนิคอ่อนแอ (score {score:+d}) — รอสัญญาณกลับตัวก่อน เช่น ยืนเหนือ EMA20/50 หรือ MACD ตัดขึ้น")
    else:
        label, tone = "หลีกเลี่ยง", "avoid"
        reasons.append(f"สัญญาณขายชัดเจน (score {score:+d}) — อย่าเพิ่งรับมีดที่กำลังตก รอฐานราคาให้เห็นก่อน")

    if r["rsi"] <= 30:
        reasons.append(f"RSI {r['rsi']:.0f} เข้าเขต oversold — อาจมีเด้งสั้น แต่ต้องรอแท่งยืนยันก่อนเข้า")
    if r["rsi"] >= 70:
        reasons.append(f"RSI {r['rsi']:.0f} เข้าเขต overbought — ซื้อตอนนี้เสี่ยงติดดอย รอย่อก่อน")
    if upside is not None:
        if upside >= 20:
            reasons.append(f"นักวิเคราะห์ให้เป้าเฉลี่ยสูงกว่าราคาปัจจุบัน {upside:+.0f}% — ระยะยาวยังมี upside")
        elif upside <= 0:
            reasons.append(f"ราคาปัจจุบันสูงกว่าเป้านักวิเคราะห์แล้ว ({upside:+.0f}%) — upside จำกัด")

    sup = r.get("support")
    entry = f"{sup:,.2f}" if sup else f"{r['ema20']:,.2f} (EMA20)"
    plan = (f"จุดเข้าที่น่าสนใจ: แถวแนวรับ {entry} | "
            f"ตัดขาดทุนถ้าหลุด {r['stop_suggest']:,.2f} | "
            f"เป้าทำกำไรแรก {r['target_suggest']:,.2f}")

    return {"advice_label": label, "advice_tone": tone,
            "advice_reasons": reasons, "advice_plan": plan}


# ----------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------
def notify_windows(title, msg):
    try:
        from winotify import Notification
        Notification(app_id="Investing Pro", title=title, msg=msg[:250]).show()
    except Exception as e:
        print(f"  [toast error: {e}]")


def notify_telegram(cfg, text):
    token = cfg["notify"].get("telegram_bot_token")
    chat_id = cfg["notify"].get("telegram_chat_id")
    if not token or not chat_id:
        return
    try:
        import urllib.parse
        import urllib.request
        url = (f"https://api.telegram.org/bot{token}/sendMessage?"
               + urllib.parse.urlencode({"chat_id": chat_id, "text": text}))
        urllib.request.urlopen(url, timeout=10)
    except Exception as e:
        print(f"  [telegram error: {e}]")


def send_alerts(result, cfg, state):
    """แจ้งเตือนเฉพาะเหตุการณ์ใหม่ (ไม่ซ้ำในวันเดียวกัน) หรือคะแนนถึงเกณฑ์"""
    ticker = result["ticker"]
    today = result["asof"]
    sent = state.setdefault(ticker, {})
    to_alert = []

    for ev in result["events"]:
        if sent.get(ev) != today:
            to_alert.append(ev)
            sent[ev] = today

    strong = abs(result["score"]) >= cfg["settings"]["min_alert_score"]
    if strong and sent.get("verdict_" + result["verdict"]) != today:
        to_alert.append("verdict")
        sent["verdict_" + result["verdict"]] = today

    if not to_alert:
        return False

    lines = [f"{ticker}  {result['price']:,.2f} ({result['change_pct']:+.2f}%)",
             f"สรุป: {result['verdict']} (score {result['score']:+d}, มั่นใจ {result['confidence']:.0f}%)"]
    for sg in result["signals"]:
        if sg["weight"] >= 2:
            arrow = "▲" if sg["dir"] == "bull" else "▼"
            lines.append(f"{arrow} {sg['name']}: {sg['desc']}")
    text = "\n".join(lines)

    print(f"\n🔔 ALERT: {ticker}")
    if cfg["notify"].get("windows_toast", True):
        notify_windows(f"📈 {ticker}: {result['verdict']}", text)
    notify_telegram(cfg, "📈 Investing Pro\n" + text)
    return True


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def print_report(r):
    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  {r['ticker']}   ราคา {r['price']:,.2f}  ({r['change_pct']:+.2f}%)   ข้อมูล ณ {r['asof']}")
    print(bar)
    print(f"  สรุป        : {r['verdict']}   (score {r['score']:+d} | ความเชื่อมั่น {r['confidence']:.0f}%)")
    state = {"REGULAR": "ตลาดเปิดอยู่", "CLOSED": "ตลาดปิดแล้ว", "PRE": "ก่อนเปิดตลาด", "POST": "หลังปิดตลาด"}.get(r.get("market_state"), "-")
    pc = f"{r['prev_close']:,.2f}" if r.get("prev_close") else "-"
    print(f"  สถานะตลาด   : {state}   ราคาปิดก่อนหน้า: {pc}")
    if r.get("pre_price"):
        print(f"  ก่อนเปิดตลาด: {r['pre_price']:,.2f} ({r['pre_chg']:+.2f}%)")
    if r.get("post_price"):
        print(f"  หลังปิด/ข้ามคืน: {r['post_price']:,.2f} ({r['post_chg']:+.2f}%)")
    pe = f"{r['pe']:.1f}" if r.get("pe") else "-"
    fpe = f"{r['fwd_pe']:.1f}" if r.get("fwd_pe") else "-"
    print(f"  P/E         : {pe}   Forward P/E: {fpe}")
    print(f"  ความคุ้มค่า : [{r['value_label']}] {r['value_desc']}")
    print(f"  💡 ควรซื้อไหม: {r['advice_label']}")
    for reason in r["advice_reasons"]:
        print(f"     - {reason}")
    print(f"     - {r['advice_plan']}")
    print(f"  RSI(14)     : {r['rsi']:.1f}")
    print(f"  EMA 20/50/200: {r['ema20']:,.2f} / {r['ema50']:,.2f} / {r['ema200']:,.2f}")
    sup = f"{r['support']:,.2f}" if r["support"] else "-"
    res = f"{r['resistance']:,.2f}" if r["resistance"] else "-"
    print(f"  แนวรับ/แนวต้าน: {sup} / {res}")
    print(f"  วอลุ่มเทียบเฉลี่ย: {r['vol_ratio']:.2f}x   ATR: {r['atr']:,.2f}")
    print(f"  จุดตัดขาดทุนแนะนำ (2xATR): {r['stop_suggest']:,.2f}")
    print(f"  เป้าหมายแนะนำ (3xATR)   : {r['target_suggest']:,.2f}")
    print("  สัญญาณ:")
    for sg in r["signals"]:
        arrow = "▲" if sg["dir"] == "bull" else "▼"
        print(f"    {arrow} [{sg['weight']}] {sg['name']} — {sg['desc']}")


def save_markdown(results):
    os.makedirs(REPORT_DIR, exist_ok=True)
    now = datetime.now()
    path = os.path.join(REPORT_DIR, f"scan_{now:%Y-%m-%d_%H%M}.md")
    lines = [f"# Investing Pro — รายงานสแกน {now:%Y-%m-%d %H:%M}\n",
             "| หุ้น | ราคา | เปลี่ยน% | P/E | ความคุ้มค่า | RSI | Score | สรุป |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        pe = r.get("fwd_pe") or r.get("pe")
        pe_s = f"{pe:.1f}" if pe else "-"
        lines.append(f"| {r['ticker']} | {r['price']:,.2f} | {r['change_pct']:+.2f}% "
                     f"| {pe_s} | {r.get('value_label', '-')} "
                     f"| {r['rsi']:.1f} | {r['score']:+d} | **{r['verdict']}** |")
    lines.append("\n## รายละเอียดสัญญาณ\n")
    for r in results:
        lines.append(f"### {r['ticker']} — {r['verdict']}")
        lines.append(f"- 💡 **ควรซื้อไหม: {r['advice_label']}** — {' / '.join(r['advice_reasons'])}")
        lines.append(f"- 📋 {r['advice_plan']}")
        for sg in r["signals"]:
            arrow = "🟢" if sg["dir"] == "bull" else "🔴"
            lines.append(f"- {arrow} **{sg['name']}** ({sg['weight']}): {sg['desc']}")
        sup = f"{r['support']:,.2f}" if r["support"] else "-"
        res = f"{r['resistance']:,.2f}" if r["resistance"] else "-"
        lines.append(f"- แนวรับ {sup} / แนวต้าน {res} | Stop {r['stop_suggest']:,.2f} | Target {r['target_suggest']:,.2f}\n")
    lines.append("\n> ⚠️ เครื่องมือนี้วิเคราะห์เชิงเทคนิคเพื่อประกอบการตัดสินใจเท่านั้น ไม่ใช่คำแนะนำการลงทุน")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def scan(cfg, tickers, alert=True):
    state = load_state()
    results, alerted = [], 0
    for t in tickers:
        try:
            r = analyze(t, cfg)
            results.append(r)
            print_report(r)
            if alert and send_alerts(r, cfg, state):
                alerted += 1
        except Exception as e:
            print(f"\n  ❌ {t}: {e}")
    save_state(state)
    if results:
        path = save_markdown(results)
        print(f"\n📄 บันทึกรายงาน: {path}")
    print(f"\n✅ สแกน {len(results)}/{len(tickers)} ตัว | แจ้งเตือน {alerted} รายการ")
    return results


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Investing Pro — Technical Analyzer & Alerts")
    ap.add_argument("--ticker", help="วิเคราะห์หุ้นตัวเดียว เช่น NVDA หรือ PTT.BK")
    ap.add_argument("--watch", action="store_true", help="รันวนต่อเนื่องตามรอบเวลาใน config")
    ap.add_argument("--no-alert", action="store_true", help="ไม่ส่งแจ้งเตือน (ดูรายงานอย่างเดียว)")
    args = ap.parse_args()

    cfg = load_config()
    tickers = [args.ticker.upper()] if args.ticker else cfg["watchlist"]

    if args.watch:
        interval = cfg["settings"].get("watch_interval_minutes", 30)
        print(f"👁  โหมดเฝ้าระวัง: สแกนทุก {interval} นาที (Ctrl+C เพื่อหยุด)")
        while True:
            print(f"\n===== รอบสแกน {datetime.now():%H:%M:%S} =====")
            scan(cfg, tickers, alert=not args.no_alert)
            time.sleep(interval * 60)
    else:
        scan(cfg, tickers, alert=not args.no_alert)


if __name__ == "__main__":
    main()
