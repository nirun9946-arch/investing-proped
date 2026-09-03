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


def volume_profile(df, lookback=120, bins=40):
    """Volume Profile: POC, Value Area 70%, HVN/LVN + สถิติพฤติกรรมราคาที่โซน POC"""
    import numpy as np
    # กันแถวข้อมูลว่างจาก Yahoo ซ้ำอีกชั้น — ถ้ามี NaN หลุดมา int() จะพังทั้งการวิเคราะห์
    data = df.dropna(subset=["Low", "High", "Volume"]).tail(lookback)
    if len(data) < 40:
        return None
    lo, hi = float(data["Low"].min()), float(data["High"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(bins)
    span = hi - lo
    for l, h, v in zip(data["Low"].values, data["High"].values, data["Volume"].values):
        if v <= 0:
            continue
        i0 = max(0, min(bins - 1, int((l - lo) / span * bins)))
        i1 = max(0, min(bins - 1, int((h - lo) / span * bins)))
        vol[i0:i1 + 1] += v / (i1 - i0 + 1)  # เกลี่ยวอลุ่มตามช่วงราคาของแท่ง

    total = vol.sum()
    if total <= 0:
        return None

    # POC = ระดับราคาที่วอลุ่มสะสมหนาแน่นที่สุด
    poc_i = int(vol.argmax())
    poc = float(centers[poc_i])

    # Value Area 70%: ขยายจาก POC ไปฝั่งที่วอลุ่มมากกว่า จนครบ 70% ของทั้งหมด
    included = {poc_i}
    acc = vol[poc_i]
    up, dn = poc_i + 1, poc_i - 1
    while acc < 0.70 * total and (up < bins or dn >= 0):
        vu = vol[up] if up < bins else -1.0
        vd = vol[dn] if dn >= 0 else -1.0
        if vu >= vd:
            included.add(up); acc += vu; up += 1
        else:
            included.add(dn); acc += vd; dn -= 1
    vah, val = float(centers[max(included)]), float(centers[min(included)])

    # HVN/LVN: ยอด/หลุมวอลุ่มเฉพาะจุด (local peaks/valleys)
    thr_h, thr_l = np.percentile(vol, 75), np.percentile(vol, 25)
    hvn, lvn = [], []
    for i in range(1, bins - 1):
        if vol[i] >= vol[i - 1] and vol[i] >= vol[i + 1] and vol[i] >= thr_h:
            hvn.append(float(centers[i]))
        elif vol[i] <= vol[i - 1] and vol[i] <= vol[i + 1] and vol[i] <= thr_l and vol[i] > 0:
            lvn.append(float(centers[i]))

    # Pattern Recognition: ในอดีตเมื่อราคาวิ่งเข้าโซน POC เกิดอะไรขึ้นใน 3 แท่งถัดมา
    band = span / bins * 1.5
    closes = data["Close"].values
    touches = bounces = breaks = 0
    for i in range(1, len(closes) - 3):
        prev, cur = closes[i - 1], closes[i]
        if abs(cur - poc) <= band and abs(prev - poc) > band:
            touches += 1
            fut = closes[i + 3]
            if prev > poc:      # เข้ามาจากด้านบน
                bounces += int(fut > poc + band)   # เด้งกลับขึ้น = POC เป็นแนวรับ
                breaks += int(fut < poc - band)    # ทะลุลง
            else:               # เข้ามาจากด้านล่าง
                bounces += int(fut < poc - band)   # โดนกดกลับลง = POC เป็นแนวต้าน
                breaks += int(fut > poc + band)    # ทะลุขึ้น

    vmax = vol.max()
    profile = [{"p": round(float(c), 4), "v": round(float(v / vmax), 3)}
               for c, v in zip(centers, vol)]
    return {"poc": poc, "vah": vah, "val": val, "band": band,
            "hvn": sorted(hvn), "lvn": sorted(lvn),
            "touches": touches, "bounces": bounces, "breaks": breaks,
            "profile": profile}


def trapped_zone(vp, price):
    """ประมาณสัดส่วน "คนติดดอย" จาก Volume Profile

    หลักการ: วอลุ่มที่ซื้อขายกันที่ระดับราคา "สูงกว่า" ราคาปัจจุบัน = ต้นทุนของคน
    ที่ซื้อแล้วยังขาดทุนอยู่ (ติดดอย) ส่วนวอลุ่มใต้ราคาปัจจุบัน = คนที่มีกำไร
    เป็นการประมาณจากราคาที่ซื้อขายจริงย้อนหลัง ~120 วัน ไม่ใช่ต้นทุนรายบุคคลจริง
    """
    prof = (vp or {}).get("profile") or []
    if not prof or not price:
        return None
    above = sum(b["v"] for b in prof if b["p"] > price)
    below = sum(b["v"] for b in prof if b["p"] < price)
    total = above + below
    if total <= 0:
        return None
    above_pct = above / total * 100

    # ต้นทุนเฉลี่ยถ่วงน้ำหนักวอลุ่ม — จุดที่ "คนส่วนใหญ่ซื้อมา"
    wsum = sum(b["p"] * b["v"] for b in prof)
    vsum = sum(b["v"] for b in prof)
    avg_cost = wsum / vsum if vsum else None

    # แนวต้านจากคนติดดอย: โซนวอลุ่มหนาสุดที่อยู่เหนือราคา (คนรอ "เท่าทุนแล้วขาย")
    above_bins = [b for b in prof if b["p"] > price]
    heavy = max(above_bins, key=lambda b: b["v"]) if above_bins else None

    if above_pct >= 70:
        label, tone = "ติดดอยหนัก — แรงขายรอเท่าทุนเยอะ", "bad"
    elif above_pct >= 45:
        label, tone = "ติดดอยปานกลาง — มีแรงขายเหนือราคา", "warn"
    elif above_pct >= 25:
        label, tone = "ติดดอยน้อย — ทางขึ้นค่อนข้างโล่ง", "ok"
    else:
        label, tone = "แทบไม่มีคนติดดอย — ส่วนใหญ่มีกำไร", "good"

    return {
        "above_pct": round(above_pct, 1),
        "below_pct": round(100 - above_pct, 1),
        "avg_cost": round(avg_cost, 4) if avg_cost else None,
        "vs_avg_cost": round((price / avg_cost - 1) * 100, 1) if avg_cost else None,
        "heavy_resist": round(heavy["p"], 4) if heavy else None,
        "label": label, "tone": tone,
        "profile": prof,
    }


def predict_5d(df, price):
    """Predictive Analytics: หาวันในอดีตที่สภาวะตลาดเหมือนวันนี้ (เทรนด์/RSI/MACD)
    แล้ววัดสถิติจริงว่า 5 วันถัดมาราคาขึ้นกี่เปอร์เซ็นต์ของครั้งทั้งหมด"""
    d = df.dropna(subset=["RSI", "EMA50", "MACD_HIST"]).copy()
    if len(d) < 60:
        return None
    d["fwd5"] = d["Close"].shift(-5) / d["Close"] - 1
    cur = d.iloc[-1]
    hist = d.iloc[:-5]
    mask = (
        ((hist["Close"] > hist["EMA50"]) == bool(cur["Close"] > cur["EMA50"]))
        & ((hist["RSI"] // 20) == (cur["RSI"] // 20))
        & ((hist["MACD_HIST"] > 0) == bool(cur["MACD_HIST"] > 0))
    )
    sample = hist.loc[mask, "fwd5"].dropna()
    if len(sample) < 12:
        return None
    return {"prob_up": float((sample > 0).mean()),
            "avg_move": float(sample.mean()),
            "n": int(len(sample))}


def _infer_market_state(df):
    """เดาสถานะตลาดจากเวลาจริงของ timezone ในกราฟ — ใช้เมื่อ Yahoo info โดนบล็อก (เช่นบนคลาวด์)"""
    try:
        tz = df.index.tz
        if tz is None:
            return None
        from datetime import datetime
        now = datetime.now(tz)
        if now.weekday() >= 5:
            return "CLOSED"
        hm = now.hour * 60 + now.minute
        tzname = str(tz)
        if "Bangkok" in tzname:  # ตลาดหุ้นไทย ~10:00-16:30
            return "REGULAR" if 600 <= hm <= 990 else "CLOSED"
        if "New_York" in tzname:
            if 240 <= hm < 570:
                return "PRE"
            if 570 <= hm < 960:
                return "REGULAR"
            if 960 <= hm < 1200:
                return "POST"
            return "CLOSED"
        return None
    except Exception:
        return None


# ----------------------------------------------------------------------
# Volume pace — เทียบวอลุ่มระหว่างวันให้เป็นธรรม
# ----------------------------------------------------------------------
# เวลาเปิด-ปิดตลาดตาม timezone ของกราฟ (นาทีนับจากเที่ยงคืน) — ใช้เฉพาะทางถอย
_SESSION_HOURS = {"New_York": (570, 960), "Bangkok": (600, 990)}

_pace_cache = {}   # ticker -> (ts, dict) ผลของ session_pace()
_PACE_TTL = 300    # วอลุ่มสะสมของวันนี้อยู่ในนี้ด้วย — ต้องสดพอควรระหว่างตลาดเปิด


def session_pace(tk, sessions=20):
    """ภาพวอลุ่มระดับ 5 นาทีของหุ้นตัวนั้น — ทั้ง baseline และของวันนี้ จากแหล่งเดียวกัน

    ตั้งใจให้ตัวเศษกับตัวหารมาจาก feed เดียวกัน เพราะวอลุ่มของแท่งรายวันที่ Yahoo ให้
    ไม่เท่ากับผลรวมแท่ง 5 นาทีในเซสชันปกติ (วัดจริงได้ราว 0.83-0.96 เท่า — ต่างกันแล้วแต่ตัว
    เพราะแท่งรายวันรวมพวก auction/off-exchange เข้าไปด้วย) เอามาหารกันข้ามแหล่งจะเพี้ยน 10-20%

    คืน dict:
      abs   นาทีของวัน -> ค่าเฉลี่ยวอลุ่มสะสม (หุ้นจริง) ของ ~20 เซสชันที่จบแล้ว
      frac  นาทีของวัน -> ค่าเฉลี่ยสัดส่วนของวอลุ่มทั้งวันที่เดินไปแล้ว (0-1)
      today วอลุ่มสะสมของวันนี้ถึงแท่งล่าสุด — None ถ้าวันนี้ยังไม่มีแท่ง
      today_min  เวลาจบของแท่งล่าสุดวันนี้ (นาทีของวัน) — ใช้เป็น "ตอนนี้" แทนนาฬิกาเครื่อง
                 เพื่อไม่ให้ feed 5 นาทีที่มาช้าถูกเทียบกับ baseline ของเวลาที่ล้ำหน้าไปแล้ว
    คืน None ถ้าข้อมูลอินทราเดย์ไม่พอ — ผู้เรียกจะถอยไปใช้สัดส่วนเวลาที่ผ่านไปแทน
    """
    sym = getattr(tk, "ticker", "")
    now = time.time()
    c = _pace_cache.get(sym)
    if c and now - c[0] < _PACE_TTL:
        return c[1]
    try:
        # 1mo = ~21 เซสชัน ซึ่งพอดีกับหน้าต่าง 20 วันของ VOL_AVG20
        # (Yahoo ให้แท่ง 5 นาทีย้อนหลังได้ไม่เกิน 60 วัน — ขอมากกว่านี้จะโดนปฏิเสธทั้งก้อน)
        h = tk.history(period="1mo", interval="5m", prepost=False)
    except Exception:
        h = None
    if h is None or h.empty or "Volume" not in h.columns:
        return None
    h = h[h["Volume"].notna()]
    if h.empty:
        return None

    tz = getattr(h.index, "tz", None)
    today = (pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()).date()
    per_day = {d: g for d, g in h.groupby(h.index.date)}

    cur = per_day.pop(today, None)
    # เซสชันวันนี้ยังไม่จบ → ยังไม่รู้วอลุ่มทั้งวัน จึงเป็นได้แค่ตัวเศษ ไม่ใช่ส่วนหนึ่งของ baseline
    today_vol = float(cur["Volume"].sum()) if cur is not None and len(cur) else None
    today_min = (cur.index[-1].hour * 60 + cur.index[-1].minute + 5) if today_vol else None

    if len(per_day) < 3:
        return None
    # ตัดวันครึ่ง (เช่นก่อนวันหยุดยาว) ทิ้ง — เส้นโค้งคนละรูปกับวันเต็ม
    counts = sorted(len(g) for g in per_day.values())
    min_bars = counts[len(counts) // 2] * 0.9
    days = [g for d, g in sorted(per_day.items()) if len(g) >= min_bars][-sessions:]

    abs_c, frac_c = [], []
    for g in days:
        v = g["Volume"].astype(float)
        total = float(v.sum())
        if total <= 0:
            continue
        mins = [t.hour * 60 + t.minute for t in g.index]
        cum = v.cumsum()
        abs_c.append(pd.Series(cum.values, index=mins))
        frac_c.append(pd.Series((cum / total).values, index=mins))
    if len(abs_c) < 3:
        return None

    # ใช้ mean ไม่ใช่ median ทั้งที่ median ทนวันข่าวใหญ่ได้ดีกว่า — เพราะตัวเลขนี้ถูกเรียกว่า
    # "เทียบค่าเฉลี่ย 20 วัน" ทุกที่ (การ์ด/CLI/AI) และตอนตลาดปิดก็หารด้วย VOL_AVG20 ซึ่งเป็น mean
    # ถ้าใช้คนละสถิติกัน ตัวเลขจะกระโดดตอน 16:00 (วัดจริง PLTR 2.49 -> 1.68) ทั้งที่ไม่มีอะไรเกิดขึ้น
    out = {"abs": pd.concat(abs_c, axis=1).mean(axis=1).sort_index().ffill(),
           "frac": pd.concat(frac_c, axis=1).mean(axis=1).sort_index().ffill().clip(upper=1.0),
           "today": today_vol, "today_min": today_min}
    _pace_cache[sym] = (now, out)
    return out


def _curve_at(curve, now_min):
    """อ่านค่าสะสมจากเส้นโค้ง ณ นาทีที่กำหนด

    ค่าใน index คือเวลา "เริ่ม" แท่ง 5 นาที ส่วนค่าสะสมคือ ณ เวลา "จบ" แท่ง
    จึงลากเส้นตรงจาก (เวลาเปิด, 0) ผ่านจุดจบของแต่ละแท่ง — ไม่งั้นช่วงเปิดตลาด
    จะอ่านเกินจริงจนตัวหารเล็กเกินไปและ ratio พุ่ง
    """
    if curve is None or len(curve) == 0:
        return None
    idx = list(curve.index)
    xs = [idx[0]] + [m + 5 for m in idx]
    ys = [0.0] + [float(v) for v in curve.values]
    if now_min >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if now_min <= xs[i]:
            span = xs[i] - xs[i - 1]
            w = (now_min - xs[i - 1]) / span if span else 1.0
            return ys[i - 1] + (ys[i] - ys[i - 1]) * w
    return ys[-1]


def _elapsed_fraction(sym, tz, now_min):
    """ทางถอย: สัดส่วน "เวลา" ที่ผ่านไปของเซสชัน ใช้แทนสัดส่วนวอลุ่ม

    หยาบกว่าเส้นโค้งจริงเพราะวอลุ่มไม่ได้เดินเป็นเส้นตรง (หนาตอนเปิดกับตอนปิด
    บางกลางวัน) → กลางวันจะประเมินสูงไปนิด ทำให้ ratio อ่านต่ำกว่าความจริงเล็กน้อย
    และไม่ได้หักพักเที่ยงของตลาดไทย — แต่ยังดีกว่าเทียบกับทั้งวันหลายเท่า

    ใช้ได้เฉพาะของที่เทรดเป็นรอบเวลาทำการจริงๆ: ฟิวเจอร์ส (=F) ค่าเงิน (=X)
    คริปโต (-USD) และดัชนี (^) เทรดเกือบ 24 ชม. เอาตาราง 9:30-16:00 ไปทาบแล้วเพี้ยนหนัก
    → คืน None ให้ผู้เรียกไปใช้เซสชันที่จบแล้วแทน ซึ่งถูกเสมอแม้จะช้าไปหนึ่งวัน
    """
    s = str(sym).upper()
    if "=" in s or s.startswith("^") or s.endswith("-USD"):
        return None
    span = next((v for k, v in _SESSION_HOURS.items() if k in str(tz)), None)
    if not span:
        return None
    open_m, close_m = span
    if now_min <= open_m or close_m <= open_m:
        return None
    return min((now_min - open_m) / (close_m - open_m), 1.0)


def volume_pace(tk, df, market_state):
    """เทียบวอลุ่มกับค่าเฉลี่ยอย่างเป็นธรรม แม้แท่งของวันนี้จะยังเดินไม่จบ

    ปัญหาที่แก้: ระหว่างตลาดเปิด แท่งรายวันแท่งสุดท้ายคือวอลุ่ม "เท่าที่สะสมมาถึงตอนนี้"
    แต่ถูกหารด้วยค่าเฉลี่ยของวันที่ครบ 6.5 ชั่วโมง → ค่าที่ได้แตะ 1.0 ไม่ได้เลยก่อนตลาดปิด
    และอ่านต่ำเหมือนกันหมดทุกตัวตลอดเช้า (ราว 11:05 ET วันปกติเดินไปแค่ ~48% ของวัน
    ตัวเลขจึงออกมาราว 0.48 ทั้งกระดาน) จนสรุปผิดว่า "ทั้งตลาดวอลุ่มเบา"

    ทางแก้: หารด้วย baseline ณ เวลาเดียวกันของหุ้นตัวนั้นเอง แล้วบอกใน response ให้ชัด
    ว่าตัวเลขที่ให้มาเทียบเวลาแล้วหรือยัง (clock_matched) และเป็นของเซสชันไหน (asof)

    คืน dict:
      ratio          ตัวเลขที่ควรใช้แสดง/ให้คะแนน
      basis          clock_matched | elapsed_fraction | last_complete_session | full_session
      clock_matched  True เมื่อตัวเศษกับตัวหารครอบคลุมช่วงเวลาเท่ากัน
      session_pct    สัดส่วนของเซสชันที่เดินไปแล้ว (0-1) — None ถ้าไม่รู้
      raw_ratio      ตัวเลขแบบเดิมที่ยังไม่ปรับเวลา (ไว้ให้เทียบ/ดีบัก)
      asof           วันที่ของเซสชันที่ ratio นี้พูดถึง
    """
    last = df.iloc[-1]
    vol_now = float(last["Volume"])
    avg_all = float(last["VOL_AVG20"]) if last["VOL_AVG20"] == last["VOL_AVG20"] else 0.0

    tz = getattr(df.index, "tz", None)
    now_ts = pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()
    partial = market_state == "REGULAR" and df.index[-1].date() == now_ts.date()

    if not partial:
        ratio = vol_now / avg_all if avg_all else 1.0
        return {"ratio": ratio, "raw_ratio": ratio, "basis": "full_session",
                "clock_matched": True, "session_pct": 1.0,
                "asof": str(df.index[-1].date()),
                "scope": "ของค่าเฉลี่ย 20 วัน",
                "label": "เทียบค่าเฉลี่ย 20 วัน (เซสชันเต็มวัน)"}

    # ค่าเฉลี่ยต้องเป็น 20 วันที่ "จบแล้ว" เท่านั้น — VOL_AVG20 ปกติรวมแท่งวันนี้ที่ยัง
    # เดินไม่จบเข้าไปด้วย ซึ่งกดตัวหารให้ต่ำลงอีกชั้นหนึ่ง (คนละทางกับ bias หลัก)
    avg20 = float(df["Volume"].iloc[-21:-1].mean()) if len(df) >= 21 else avg_all
    raw = vol_now / avg20 if avg20 else 1.0
    today_date = str(df.index[-1].date())
    asof = {"asof": today_date, "raw_ratio": raw}

    # ทางหลัก: เทียบวอลุ่มสะสมของวันนี้กับ median ของ ~20 เซสชันที่จบแล้ว ณ นาฬิกาเดียวกัน
    # ทั้งสองฝั่งมาจากแท่ง 5 นาทีชุดเดียวกัน จึงไม่มีปัญหาสเกลข้ามแหล่งข้อมูล
    p = session_pace(tk)
    if p and p["today"] and p["today_min"]:
        base = _curve_at(p["abs"], p["today_min"])
        if base and base > 0:
            frac = _curve_at(p["frac"], p["today_min"]) or 0.0
            pct = round(min(max(frac, 0.0), 1.0) * 100)
            return {**asof, "ratio": p["today"] / base, "basis": "clock_matched",
                    "clock_matched": True, "session_pct": frac,
                    "scope": "ของวอลุ่มปกติ ณ เวลานี้ของวัน",
                    "label": f"เทียบ ณ เวลาเดียวกันของวัน (วันนี้เดินมา {pct}% ของเซสชัน)"}

    # ทางถอย 1: ไม่มีอินทราเดย์ → ปรับแท่งวันนี้ด้วยสัดส่วน "เวลา" ที่ผ่านไป
    frac = _elapsed_fraction(getattr(tk, "ticker", ""), tz, now_ts.hour * 60 + now_ts.minute)
    if frac and frac > 0:
        frac = min(max(frac, 0.02), 1.0)
        return {**asof, "ratio": raw / frac, "basis": "elapsed_fraction",
                "clock_matched": True, "session_pct": frac,
                "scope": "ของวอลุ่มปกติ ณ เวลานี้ของวัน (ประมาณ)",
                "label": (f"เทียบ ณ เวลาเดียวกันของวัน — ประมาณจากเวลาที่ผ่านไป "
                          f"{round(frac * 100)}% ของเซสชัน")}

    # ทางถอย 2: ไม่รู้ว่าวันนี้เดินไปเท่าไหร่ → ไม่เดา ใช้เซสชันที่จบแล้วและบอกให้ชัด
    prev_avg = float(df["Volume"].iloc[-22:-2].mean()) if len(df) >= 22 else avg20
    prev_vol = float(df["Volume"].iloc[-2])
    return {**asof, "ratio": (prev_vol / prev_avg if prev_avg else 1.0),
            "basis": "last_complete_session", "clock_matched": False,
            "session_pct": None, "asof": str(df.index[-2].date()),
            "scope": "ของค่าเฉลี่ย 20 วัน (เซสชันก่อนหน้า)",
            "label": "เซสชันก่อนหน้าที่จบแล้ว — วอลุ่มวันนี้ยังเทียบไม่ได้"}


def support_resistance(df, lookback=60, window=5):
    """หาแนวรับ/แนวต้านจาก swing highs/lows ล่าสุด

    คืน (support, resistance, sup_basis, res_basis) — สอง basis บอกว่าระดับนั้นมาจากไหน
    "swing" = จุดกลับตัวจริงในกรอบ 60 แท่ง · "w52" = ถอยไปใช้จุดสูง/ต่ำสุด 52 สัปดาห์
    · None = ไม่มีจริง ๆ (ราคาทำจุดสูงสุด/ต่ำสุดของข้อมูลทั้งชุด)

    เดิมคืน None เฉย ๆ เมื่อราคาทำจุดสูงสุดของกรอบ 60 แท่ง ทำให้การ์ดขึ้นว่า
    "ไม่มีแนวต้าน" ทั้งที่จุดสูงสุด 52 สัปดาห์ยังลอยอยู่เหนือราคา (เคยรายงานผิด
    ให้ PLTR และ NVDA มาแล้ว) จึงถอยไปใช้กรอบ 52 สัปดาห์ก่อนจะยอมแพ้
    """
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
    res_basis = "swing" if resistance is not None else None
    sup_basis = "swing" if support is not None else None

    if resistance is None:
        hi = float(df["High"].tail(252).max())
        if hi > price:
            resistance, res_basis = hi, "w52"
    if support is None:
        lo = float(df["Low"].tail(252).min())
        if lo < price:
            support, sup_basis = lo, "w52"
    return support, resistance, sup_basis, res_basis


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------
def _patch_unsettled_last_bar(tk, df):
    """เติมราคาปิดของแท่งรายวันล่าสุดที่ Yahoo ยังไม่ settle

    Yahoo คืนแท่งของ "วันที่เทรดจบแล้ว" โดยมี Volume ครบแต่ Close เป็น NaN ได้
    (เจอ 28 ส.ค. 2569 พร้อมกันทั้ง 15 ตัวใน watchlist และดัชนีหลักทุกตัว)
    ถ้าปล่อยให้ dropna ทิ้งแท่งนั้น ระบบจะถอยไปใช้ข้อมูลของเซสชันก่อนหน้า
    "โดยเงียบ ๆ" — asof/RSI/EMA/แนวรับ-แนวต้าน/score กลายเป็นของวันเก่าทั้งชุด
    โดยหน้าจอไม่มีอะไรบอก ซึ่งเคยทำให้รายงานเช้าพลาดทั้งฉบับมาแล้ว

    จึงกู้แท่งนั้นคืนจากข้อมูลราย 1 นาทีของเซสชันปกติ (prepost=False
    เพื่อให้ได้ราคาปิดจริงที่ 16:00 ET ไม่ใช่ราคาหลังตลาด)
    ถ้ากู้ไม่ได้คืน df เดิม — ยอมข้อมูลช้า ดีกว่าข้อมูลมั่ว
    """
    try:
        if df is None or df.empty or "Close" not in df:
            return df
        if not pd.isna(df["Close"].iloc[-1]):
            return df
        # ต้องมี Volume จริงจึงเชื่อว่าเป็นวันที่เทรดแล้ว ไม่ใช่แถวว่างของวันหยุด
        vol = df["Volume"].iloc[-1] if "Volume" in df else None
        if vol is None or pd.isna(vol) or float(vol) <= 0:
            return df
        target = df.index[-1].date()
        m = tk.history(period="5d", interval="1m", prepost=False)
        if m is None or m.empty:
            return df
        m = m[[i.date() == target for i in m.index]].dropna(subset=["Close"])
        if m.empty:
            return df
        df.loc[df.index[-1], ["Open", "High", "Low", "Close"]] = [
            float(m["Open"].iloc[0]), float(m["High"].max()),
            float(m["Low"].min()), float(m["Close"].iloc[-1]),
        ]
    except Exception:
        # กู้ไม่สำเร็จก็ปล่อยให้ dropna ทำงานตามเดิม — ไม่ให้ทั้งการ์ดพังเพราะการกู้ล้ม
        pass
    return df


def fetch(ticker, period="1y", interval="1d"):
    tk = yf.Ticker(ticker)
    df = tk.history(period=period, interval=interval, auto_adjust=True)
    # กู้แท่งล่าสุดที่ราคาปิดยังไม่ settle "ก่อน" dropna มิฉะนั้นจะเสียเซสชันล่าสุดไปเงียบ ๆ
    if interval == "1d":
        df = _patch_unsettled_last_bar(tk, df)
    # Yahoo แถมแถวเปล่า (NaN) มาเป็นครั้งคราว เช่นวันหยุดพิเศษหรือช่วงข้อมูลกำลังอัปเดต
    # ถ้าไม่ตัดทิ้ง จะทำให้การคำนวณที่แปลงเป็นจำนวนเต็ม (Volume Profile) พังทั้งการ์ด
    if df is not None and not df.empty:
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[df["Close"] > 0]
    # เกณฑ์ขั้นต่ำ 15 แท่ง — พอคำนวณ RSI/EMA พื้นฐาน และรองรับหุ้นเพิ่งเข้าตลาดใหม่
    # (อินดิเคเตอร์ระยะยาว เช่น EMA200, Volume Profile จะข้ามเองถ้าข้อมูลไม่พอ)
    if df is None or df.empty or len(df) < 15:
        n = 0 if df is None else len(df)
        raise ValueError(f"ข้อมูลไม่พอสำหรับ {ticker} (ได้ {n} แท่ง — หุ้นใหม่มากหรือชื่อไม่ถูก)")
    return tk, df


_fund_cache = {}  # ticker -> (ts, out) ค่าล่าสุดที่สำเร็จ — ใช้แทนเมื่อ Yahoo ล่มชั่วคราว

# ราคาปิดวันก่อนหน้า: ticker -> (ts, prev) แคช 10 นาที
# (ราคาปิดเปลี่ยนแค่วันละครั้ง แต่ live_quotes ถูกเรียกทุก 15 วิ — ไม่ควรดึง daily history ทุกรอบ)
_prev_cache = {}
_PREV_TTL = 600


def _futures_prev_close(tk):
    """ราคาปิดเซสชันล่าสุดของฟิวเจอร์ส คำนวณจากแท่งราย 1 ชม. ที่ 16:00 ET

    บั๊ก (เจอ 3 ก.ย. 2569): ฟิวเจอร์สเทรดเกือบ 24 ชม. ทำให้แท่ง "รายวัน" ของ Yahoo
    สำหรับ GC=F/SI=F/CL=F เชื่อไม่ได้เลย — Volume ต่ำผิดปกติ (ทองได้ 191-1,202 สัญญา
    ทั้งที่ของจริงหลักแสน) และราคาปิดไม่ตรงกับการปิดเซสชันจริง ทั้งยังปนเซสชัน
    กลางคืนของ "วันถัดไป" เข้ามาด้วย เช่นแท่งวันที่ 2 ก.ย. 2569 ปิดที่ 4,472.3
    ซึ่งคือราคาตอน 23:00 ET = เซสชันที่จะ settle วันที่ 3 ไม่ใช่ราคาปิดของวันที่ 2

    ผลต่อเนื่อง: prev_close() เห็นแท่งสุดท้ายลงวันที่ตรงกับ "วันนี้" จึงถอยไปหยิบ
    แท่งวันที่ 1 ก.ย. (4,348.0) มาเป็นฐาน แล้วเอาราคาสดของเซสชันวันที่ 3 มาเทียบ
    = คร่อม 2 เซสชันรวด รายงานว่าทองขึ้น +2.94% และเงิน +2.97% ทั้งที่เทียบ
    ราคาปิด RTH จริง (ทอง 4,434.3) ขึ้นแค่ +0.94% — ขนาดผิดไปเกิน 3 เท่า

    → เลี่ยงแท่งรายวันทั้งหมด ใช้แท่งราย 1 ชม. ที่ 16:00 ET (ปิด RTH ของ COMEX/NYMEX)
    เป็นฐานแทน ซึ่งเป็นตัวเลขเดียวกับที่สำนักข่าวรายงานเป็น "ราคาปิด"
    """
    try:
        h = tk.history(period="1mo", interval="1h", auto_adjust=False)
        if h is None or h.empty or "Close" not in h:
            return None
        h = h.dropna(subset=["Close"])
        if "Volume" in h:
            h = h[h["Volume"] > 0]          # ตัดแท่งกลวงช่วงตลาดพัก
        rth = h[h.index.hour == 16]          # แท่ง 16:00 ET = ปิดเซสชันปกติ
        if rth.empty:
            return None
        now_ts = pd.Timestamp.now(tz=getattr(rth.index, "tz", None))
        past = rth[rth.index < now_ts]       # ต้องเป็นเซสชันที่จบไปแล้วเท่านั้น
        if past.empty:
            return None
        return float(past["Close"].iloc[-1])
    except Exception:
        return None


def prev_close(ticker, tk=None):
    """ราคาปิดล่าสุดที่ 'จบวันแล้ว' — ฐานที่ถูกต้องสำหรับคำนวณ % เปลี่ยนแปลง

    ไม่ใช้ fast_info.previous_close และ info.previousClose เพราะทั้งคู่ไม่น่าเชื่อถือ:
    ค่าเพี้ยนไปจากราคาปิดจริง หรือช้าไป 1 วัน ทำให้ % บนการ์ดผิด (บางตัวเครื่องหมายกลับข้าง)
    → หาจาก daily history โดยตรง ซึ่งเป็นแหล่งเดียวกับราคาปิดที่แสดงบนการ์ด
    """
    now = time.time()
    c = _prev_cache.get(ticker)
    if c and now - c[0] < _PREV_TTL:
        return c[1]
    try:
        tk = tk or yf.Ticker(ticker)
        # ฟิวเจอร์ส (=F) เทรดเกือบ 24 ชม. → แท่งรายวันของ Yahoo ใช้ไม่ได้ ดู _futures_prev_close()
        if str(ticker).endswith("=F"):
            fp = _futures_prev_close(tk)
            if fp is not None:
                _prev_cache[ticker] = (now, fp)
                return fp
            # กู้ไม่ได้ก็ตกไปใช้ทางเดิม ดีกว่าไม่มีค่าเลย แต่ % อาจคร่อมเซสชัน
        # auto_adjust=False = ราคากระดานจริง — ถ้าใช้ราคาปรับปันผล ตัวที่จ่ายปันผลถี่
        # (เช่น covered-call ETF) จะได้ฐาน % ผิดในวัน ex-dividend
        h = tk.history(period="5d", interval="1d", auto_adjust=False)
        # บั๊ก (เจอ 31 ส.ค. 2569): เส้นทางนี้ไม่ได้กู้แท่งที่ Yahoo ยังไม่ settle เหมือน fetch()
        # เช้าวันจันทร์ Yahoo ส่งแท่งศุกร์ 28 ส.ค. มาโดย Close = NaN, dropna จึงทิ้งทั้งแท่ง
        # แล้ว prev_close ถอยไปใช้ราคาปิดวันพฤหัสฯ — ข้ามทั้งเซสชันวันศุกร์ไปเงียบ ๆ
        # ผลคือ % บนการ์ด/quotes เทียบผิดฐาน (NVDA โชว์ -4.00% ทั้งที่เทียบปิดศุกร์จริงคือ +0.61%)
        # → เรียกตัวกู้แท่งตัวเดียวกับที่ fetch() ใช้ "ก่อน" dropna
        h = _patch_unsettled_last_bar(tk, h)
        if h is not None and not h.empty:
            h = h.dropna(subset=["Close"])     # กันแถวเปล่าจาก Yahoo → ไม่งั้นได้ NaN
        if h is None or h.empty:
            return c[1] if c else None
        # ใช้ timezone จาก index ของ history เอง — หุ้นไทย (.BK) คนละโซนกับ NY
        tz = getattr(h.index, "tz", None)
        today = (pd.Timestamp.now(tz=tz) if tz is not None else pd.Timestamp.now()).date()
        # แท่งวันสุดท้ายเป็นของ "วันนี้" (ตลาดเปิดแล้ว) → ปิดก่อนหน้าคือแท่งรองสุดท้าย
        if h.index[-1].date() == today and len(h) >= 2:
            prev = float(h["Close"].iloc[-2])
        else:
            prev = float(h["Close"].iloc[-1])
        _prev_cache[ticker] = (now, prev)
        return prev
    except Exception:
        return c[1] if c else None


def fundamentals(tk, price):
    """ดึงข้อมูลพื้นฐาน: ราคาปิด, P/E, เป้านักวิเคราะห์ + ประเมินความคุ้มค่าของราคา"""
    sym = getattr(tk, "ticker", "")
    out = {"prev_close": None, "pe": None, "fwd_pe": None, "peg": None,
           "target": None, "upside": None, "w52h": None, "w52l": None,
           "market_state": None, "value_label": "N/A",
           "value_desc": "ไม่มีข้อมูลพื้นฐาน",
           "sector": None, "industry": None, "company": None,
           "holders": None,
           "pre_price": None, "pre_chg": None, "post_price": None, "post_chg": None}
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    # Yahoo ตอบว่าง/ล้มเหลว (โดน rate-limit เป็นช่วงๆ) → ใช้ค่าล่าสุดที่เคยได้ ไม่ปล่อยให้หาย
    if not info.get("previousClose") and not info.get("regularMarketPrice"):
        cached = _fund_cache.get(sym)
        return dict(cached[1]) if cached else out

    # ใช้ค่าเดียวกับที่ live_quotes ใช้เป็นฐาน % — ทั้งการ์ดจึงสอดคล้องกัน
    # (info.previousClose ช้าไป 1 วัน เช่น NVDA โชว์ 212.5 แทนที่จะเป็น 207.40)
    out["prev_close"] = prev_close(sym, tk) or info.get("previousClose")

    # ราคานอกเวลาทำการ: ก่อนเปิด (pre-market) / หลังปิด-ข้ามคืน (post-market)
    pre = info.get("preMarketPrice")
    post = info.get("postMarketPrice")
    if pre and price:
        out["pre_price"] = float(pre)
        out["pre_chg"] = (float(pre) / price - 1) * 100
    if post and price:
        out["post_price"] = float(post)
        out["post_chg"] = (float(post) / price - 1) * 100

    # แผนสำรอง: บางช่วง (เช่นสุดสัปดาห์) Yahoo ไม่ส่ง pre/post มา —
    # ดึงราคาซื้อขายนอกเวลาล่าสุดจากกราฟราย 15 นาที (prepost) แทน
    if price and not out["pre_price"] and not out["post_price"] \
            and info.get("marketState") != "REGULAR":
        try:
            h = tk.history(period="1d", interval="15m", prepost=True)
            if h is not None and not h.empty:
                last_px = float(h["Close"].iloc[-1])
                if abs(last_px - price) / price > 0.0005:
                    out["post_price"] = last_px
                    out["post_chg"] = (last_px / price - 1) * 100
        except Exception:
            pass
    out["pe"] = info.get("trailingPE")
    out["fwd_pe"] = info.get("forwardPE")
    peg = info.get("trailingPegRatio") or info.get("pegRatio")
    out["peg"] = float(peg) if peg else None
    out["target"] = info.get("targetMeanPrice")
    out["w52h"] = info.get("fiftyTwoWeekHigh")
    out["w52l"] = info.get("fiftyTwoWeekLow")
    out["market_state"] = info.get("marketState")
    # กลุ่มธุรกิจ — ใช้เลือกลายกราฟฟิกประจำการ์ดบนหน้าเว็บ
    out["sector"] = info.get("sector")
    out["industry"] = info.get("industry")
    out["company"] = info.get("shortName") or info.get("longName")
    if info.get("quoteType") == "ETF" and not out["sector"]:
        out["sector"] = "ETF"

    # โครงสร้างผู้ถือหุ้น: รายใหญ่ (สถาบัน) / ผู้บริหาร / รายย่อยที่เหลือ
    # ตัวเลขจากแบบรายงาน 13F+SEC รายไตรมาส — เป็น "สัดส่วนการถือครอง" ไม่ใช่ยอดซื้อขายรายวัน
    inst = info.get("heldPercentInstitutions")
    insd = info.get("heldPercentInsiders")
    if inst is not None or insd is not None:
        i_pct = (inst or 0) * 100
        n_pct = (insd or 0) * 100
        # Yahoo บางตัวรายงานเกิน 100% (นับซ้ำหุ้นที่ให้ยืม) — บีบให้อยู่ในกรอบที่สมเหตุสมผล
        if i_pct + n_pct > 100:
            scale = 100 / (i_pct + n_pct)
            i_pct, n_pct = i_pct * scale, n_pct * scale
        retail = max(0.0, 100 - i_pct - n_pct)
        out["holders"] = {"inst": round(i_pct, 1), "insider": round(n_pct, 1),
                          "retail": round(retail, 1)}
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
    _fund_cache[sym] = (time.time(), dict(out))
    return out


# ----------------------------------------------------------------------
# Analysis engine
# ----------------------------------------------------------------------
_quote_cache = {}   # ticker -> (ts, quote)
_quote_miss = {}    # ticker -> ts ที่ดึงไม่ได้ครั้งล่าสุด (กันสัญลักษณ์เสียถ่วงทุกรอบ poll)
QUOTE_TTL = 4.0     # วิ — แคชสั้นได้เพราะ batch ยิงครั้งเดียวได้ทุกตัว (เดิมยิงตัวละคำขอ ต้องแคช 20 วิ)

# ราคาสดแบบ batch: ทุกสัญลักษณ์ในคำขอเดียว
# วัดจริง 3 ก.ย. 69 — 8 ตัว: ทางเดิม (history รายตัว) 3.72 วิ · ทางนี้ 0.11 วิ (เร็วกว่า 34 เท่า)
# และได้ของที่ history ไม่มี: ราคาปิด session ทางการ, % ทางการ, ราคา pre/post แยกช่อง,
# marketState รายตัว และ exchangeDataDelayedBy (ดีเลย์จริงของแต่ละตลาด)
_BATCH_URL = "https://query2.finance.yahoo.com/v7/finance/quote"


def _fmt_ts(epoch, tzname):
    """epoch → 'YYYY-MM-DD HH:MM:SS+07:00' ตามโซนของตลาดนั้น

    ฝั่งเว็บอ่าน ts นี้ 2 ทาง: Date.parse() (ต้องมี offset ถึงจะไม่เพี้ยน)
    และ regex จับ HH:MM เพื่อโชว์ "ดีลสุดท้าย HH:MM น. NY" → ต้องเป็นเวลาตลาด ไม่ใช่ UTC
    """
    if not epoch:
        return None
    try:
        from datetime import datetime, timezone
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzname) if tzname else timezone.utc
        except Exception:
            tz = timezone.utc
        d = datetime.fromtimestamp(float(epoch), tz)
        off = d.strftime("%z")                      # '+0700'
        off = (off[:3] + ":" + off[3:]) if off else "+00:00"
        return d.strftime("%Y-%m-%d %H:%M:%S") + off
    except Exception:
        return None


def _batch_quotes(tickers):
    """ยิงคำขอเดียวได้ราคาทุกตัว — คืน dict {ticker: quote} (ตัวที่ไม่มีข้อมูลจะไม่อยู่ใน dict)"""
    from yfinance.data import YfData
    r = YfData().get_raw_json(_BATCH_URL, params={"symbols": ",".join(tickers)})
    out = {}
    for q in (r.get("quoteResponse") or {}).get("result") or []:
        sym = q.get("symbol")
        reg = q.get("regularMarketPrice")
        if not sym or reg is None:
            continue
        tz = q.get("exchangeTimezoneName")
        st = q.get("marketState") or ""
        # ราคา "สดที่สุด" ที่จะเอาไปโชว์: ถ้ามีดีลนอกเวลาให้ใช้ดีลนั้น ไม่งั้นใช้ราคาปกติ
        # PREPRE/CLOSED ยังยึด postMarketPrice เพราะเป็นดีลล่าสุดที่เกิดขึ้นจริง
        px, ts_ep = reg, q.get("regularMarketTime")
        if st.startswith("PRE") and q.get("preMarketPrice") is not None:
            px, ts_ep = q["preMarketPrice"], q.get("preMarketTime")
        elif q.get("postMarketPrice") is not None and st in ("POST", "POSTPOST", "PREPRE", "CLOSED"):
            px, ts_ep = q["postMarketPrice"], q.get("postMarketTime")
        prev = q.get("regularMarketPreviousClose")
        out[sym] = {
            "ticker": sym,
            "price": float(px),
            "chg": ((float(px) / prev - 1) * 100) if prev else None,
            "ts": _fmt_ts(ts_ep, tz),
            # ราคาปิด session ทางการ + % ทางการของ Yahoo — ตรงกับที่ Google/โบรกโชว์
            # ฝั่งเว็บเอาไปแทน r.price ตอนนอกเวลา ทำให้พาดหัวไม่ค้างเป็นของรอบสแกนก่อน
            "reg_price": float(reg),
            "reg_chg": q.get("regularMarketChangePercent"),
            "prev_close": prev,
            "state": st or None,
            "delay": q.get("exchangeDataDelayedBy"),   # นาที — 0 = เรียลไทม์จริง
        }
    return out


def _quote_one_slow(t):
    """ทางสำรองรายตัว (แท่ง 1 นาทีล่าสุด) — ใช้เมื่อ batch ใช้ไม่ได้"""
    try:
        tk = yf.Ticker(t)
        h = tk.history(period="1d", interval="1m", prepost=True)
        if h is None or h.empty:
            return None
        last = float(h["Close"].iloc[-1])
        prev = prev_close(t, tk)
        return {"ticker": t, "price": last,
                "chg": (last / prev - 1) * 100 if prev else None,
                "ts": str(h.index[-1]), "prev_close": prev}
    except Exception:
        return None


def live_quotes(tickers, max_age=None):
    """ราคาสดของทุกตัวที่ขอมา — batch ก่อน ถ้าไม่ได้ค่อยถอยไปทางเดิมรายตัว

    max_age = ยอมให้ค่าในแคชเก่าได้กี่วินาที (ไม่ใส่ = QUOTE_TTL)
    เธรดอุ่นเบื้องหลังส่งค่าที่สั้นกว่ามา เพื่อ "รีเฟรชก่อนแคชหมดอายุ" —
    ถ้าใช้ TTL เดียวกับคำขอของผู้ใช้ เธรดอุ่นจะเจอแคชที่ยังไม่หมดอายุแล้วไม่ทำอะไรเลย
    กลายเป็นว่าผู้ใช้ยังต้องเป็นคนรอ Yahoo เองอยู่ดี (วัดได้: ช้าสลับเร็ว 180/18 มิลลิวินาที)
    """
    now = time.time()
    ttl = QUOTE_TTL if max_age is None else max_age
    tickers = [t.upper() for t in tickers][:40]
    need = [t for t in tickers
            if (t not in _quote_cache or now - _quote_cache[t][0] > ttl)
            # สัญลักษณ์ที่เพิ่งดึงไม่ได้ พัก 10 นาทีค่อยลองใหม่ — ไม่งั้นตัวเสียตัวเดียว
            # ลากทางสำรอง (ยิงทีละตัว + retry) เข้ามาในทุกรอบ poll ทำให้ราคาทั้งชุดช้าตาม
            and now - _quote_miss.get(t, 0) > 600]

    if need:
        got = {}
        try:
            got = _batch_quotes(need)
        except Exception:
            got = {}
        for t, q in got.items():
            _quote_cache[t] = (now, q)

        # ตัวที่ batch ไม่คืนมา (สัญลักษณ์แปลก/ตลาดปิดยาว) → ลองทางเดิมทีละตัว
        missing = [t for t in need if t not in got]
        if missing:
            import concurrent.futures
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=8)
            try:
                futs = {ex.submit(_quote_one_slow, t): t for t in missing}
                done, _ = concurrent.futures.wait(futs, timeout=20)
                for f in done:
                    t = futs[f]
                    try:
                        q = f.result()
                    except Exception:
                        q = None
                    if q:
                        _quote_cache[t] = (now, q)
                        _quote_miss.pop(t, None)
                    else:
                        _quote_miss[t] = now
                for f, t in futs.items():
                    if f not in done:
                        _quote_miss[t] = now
            finally:
                ex.shutdown(wait=False)

    return [_quote_cache[t][1] for t in tickers if t in _quote_cache]


def detect_reversal(df, vol_ratio=None):
    """สัญญาณกลับตัวหลังลงติดต่อกันหลายวัน (bottom reversal)

    ขั้นแรกต้องมี "การลงจริง" ก่อน: ลงอย่างน้อย 4 ใน 6 แท่งก่อนหน้า
    หรือราคาร่วง >=5% จาก high 10 วัน — จากนั้นนับสัญญาณกลับตัว 7 ข้อ
    (แท่งค้อน, engulfing, RSI เงยจาก oversold, bullish divergence,
     MACD histogram ยกตัว, วอลุ่มเข้าแท่งเขียว, ปิดเหนือ high เมื่อวาน)
    ยิ่งเข้าหลายข้อยิ่งน่าเชื่อ — คืน None ถ้าไม่เข้าเงื่อนไขตั้งต้นหรือได้ต่ำกว่า 2 ข้อ
    """
    if len(df) < 30:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]

    # --- เงื่อนไขตั้งต้น: ต้องลงมาหลายวันจริงก่อน (ไม่นับแท่งวันนี้ที่อาจเป็นวันกลับตัว) ---
    diffs = df["Close"].diff().iloc[-7:-1]           # 6 แท่งก่อนหน้า
    down_days = int((diffs < 0).sum())
    high10 = float(df["High"].iloc[-11:-1].max())
    drop_pct = (high10 - float(prev["Close"])) / high10 * 100 if high10 else 0.0
    if down_days < 4 and drop_pct < 5:
        return None

    sigs = []
    o, c = float(last["Open"]), float(last["Close"])
    h, l = float(last["High"]), float(last["Low"])
    body, rng = abs(c - o), h - l

    # 1) แท่งค้อน — ไส้ล่างยาว = แรงขายระหว่างวันโดนซื้อกลับหมด
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if rng > 0 and body > 0 and lower_wick >= 2 * body and upper_wick <= body:
        sigs.append("แท่งค้อน (Hammer): ไส้ล่างยาว แรงขายถูกซื้อคืน — แท่งกลับตัวคลาสสิก")

    # 2) แท่งเขียวกลืนแท่งแดงเมื่อวาน
    po, pc = float(prev["Open"]), float(prev["Close"])
    if c > o and pc < po and c >= po and o <= pc:
        sigs.append("แท่งเขียวกลืนแท่งแดง (Bullish Engulfing): แรงซื้อกลับมาคุมเกม")

    # 3) RSI เงยหัวขึ้นจากเขต oversold
    rsi_recent = df["RSI"].iloc[-6:]
    rsi_now, rsi_prev = float(last["RSI"]), float(prev["RSI"])
    if float(rsi_recent.min()) <= 35 and rsi_now > rsi_prev:
        sigs.append(f"RSI เงยหัวขึ้นจากเขต oversold (ต่ำสุด {rsi_recent.min():.0f} → ตอนนี้ {rsi_now:.0f})")

    # 4) Bullish Divergence — ราคาทำ low ใหม่ แต่ RSI ไม่ลงตาม = แรงขายเริ่มหมด
    lows, rsis = df["Low"], df["RSI"]
    recent_i = lows.iloc[-5:].idxmin()
    prior_i = lows.iloc[-20:-5].idxmin()
    if (float(lows[recent_i]) < float(lows[prior_i])
            and float(rsis[recent_i]) > float(rsis[prior_i]) + 2):
        sigs.append("Bullish Divergence: ราคาทำจุดต่ำใหม่แต่ RSI ยกตัวสวนทาง — แรงขายอ่อนกำลัง")

    # 5) MACD histogram ยกตัว 2 วันติด — โมเมนตัมลบเริ่มอ่อน
    hist = df["MACD_HIST"].iloc[-3:]
    if float(hist.iloc[2]) > float(hist.iloc[1]) > float(hist.iloc[0]):
        sigs.append("MACD histogram ยกตัว 2 วันติด — โมเมนตัมฝั่งลบอ่อนแรงลง")

    # 6) วอลุ่มเข้าแท่งเขียว — การเด้งต้องมีแรงซื้อจริงถึงน่าเชื่อ
    #    ใช้ ratio ที่ปรับเวลาแล้วจาก volume_pace ถ้าผู้เรียกส่งมาให้ ไม่งั้นแท่งที่ยัง
    #    เดินไม่จบจะเทียบกับค่าเฉลี่ยทั้งวันแล้วไม่มีวันถึง 1.5 เท่าก่อนตลาดปิด
    vr = vol_ratio
    if vr is None:
        vol_avg = float(last["VOL_AVG20"]) if last["VOL_AVG20"] == last["VOL_AVG20"] else 0.0
        vr = float(last["Volume"]) / vol_avg if vol_avg > 0 else 0.0
    if c > o and vr >= 1.5:
        sigs.append(f"วอลุ่มเข้าแท่งเขียว {vr:.1f} เท่าของปกติ — มีแรงซื้อจริง")

    # 7) ปิดเหนือ high เมื่อวาน — ความแข็งแรงวันแรกหลังลงต่อเนื่อง
    if c > float(prev["High"]):
        sigs.append("ปิดเหนือ high เมื่อวาน — วันแรกที่ฝั่งซื้อชนะเต็มแท่งหลังลงมาหลายวัน")

    n = len(sigs)
    if n < 2:
        return None
    label = ("สัญญาณกลับตัวชัดเจน" if n >= 4
             else "เริ่มมีสัญญาณกลับตัว" if n == 3
             else "สัญญาณกลับตัวอ่อนๆ กำลังก่อตัว")
    return {"down_days": down_days, "drop_pct": round(drop_pct, 1),
            "score": n, "max": 7, "signals": sigs, "label": label}


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
    support, resistance, sup_basis, res_basis = support_resistance(df)

    # ต้องรู้สถานะตลาดก่อนคิดวอลุ่ม — ระหว่างตลาดเปิด แท่งสุดท้ายยังเดินไม่จบ
    fund = fundamentals(tk, price)
    market_state = fund.get("market_state") or _infer_market_state(df)
    vol = volume_pace(tk, df, market_state)
    vol_ratio = vol["ratio"]

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

    # --- Volume (เทียบ ณ เวลาเดียวกันของวัน — ดูเหตุผลใน volume_pace) ---
    if vol_ratio >= s["volume_spike_factor"]:
        direction = "bull" if last["Close"] >= last["Open"] else "bear"
        add("Volume Spike", direction, 2,
            f"วอลุ่ม {vol_ratio:.1f} เท่า{vol['scope']} (แท่ง{'เขียว' if direction=='bull' else 'แดง'})")
        events.append("vol_spike_" + direction)

    # --- Volume Profile Fusion: ตำแหน่งราคาเทียบโซนวอลุ่ม ---
    vp = volume_profile(df)
    if vp:
        if price > vp["vah"]:
            add("Above Value Area", "bull", 1,
                f"ราคายืนเหนือ Value Area (VAH {vp['vah']:,.2f}) — ตลาดยอมรับราคาสูงขึ้น")
        elif price < vp["val"]:
            add("Below Value Area", "bear", 1,
                f"ราคาหลุดใต้ Value Area (VAL {vp['val']:,.2f}) — ตลาดปฏิเสธราคา ระวังไหลต่อ")
        hvn_below = [h for h in vp["hvn"] if h < price]
        if hvn_below and (price - hvn_below[-1]) / price <= 0.05:
            add("HVN Support", "bull", 1,
                f"มีโซนวอลุ่มหนาแน่น (HVN {hvn_below[-1]:,.2f}) รองรับใต้ราคา — แนวรับเชิงวอลุ่ม")
        lvn_below = [l for l in vp["lvn"] if l < price]
        if price < vp["poc"] and lvn_below and (price - lvn_below[-1]) / price <= 0.04:
            add("LVN Below", "bear", 1,
                f"ใต้ราคาเป็นโซนวอลุ่มบาง (LVN {lvn_below[-1]:,.2f}) — ถ้าหลุดอาจไหลเร็ว")

    prediction = predict_5d(df, price)

    # --- สัญญาณกลับตัวหลังลงหลายวัน ---
    # ส่ง ratio ที่ปรับเวลาแล้วเข้าไปด้วย — ไม่งั้นข้อ "วอลุ่มเข้าแท่งเขียว" แทบไม่มีวัน
    # เข้าเงื่อนไขระหว่างตลาดเปิด (ยกเว้นตอนที่ ratio พูดถึงคนละเซสชันกับแท่งที่กำลังดู)
    reversal = detect_reversal(df, vol_ratio if vol["basis"] != "last_complete_session" else None)
    if reversal and reversal["score"] >= 3:
        add("Reversal Setup", "bull", 2,
            f"🔄 {reversal['label']} — ลงมา {reversal['down_days']}/6 วัน "
            f"(-{reversal['drop_pct']}% จาก high 10 วัน) เข้าเงื่อนไขกลับตัว {reversal['score']}/7 ข้อ")
        events.append("reversal")

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

    # ฐาน % รายวันต้องเป็น "ราคากระดานจริง" (ไม่ปรับปันผล) — df หลักเป็นราคาปรับ
    # ซึ่งเหมาะกับอินดิเคเตอร์ แต่ทำให้ % ผิดในวัน ex-dividend ของตัวจ่ายปันผลถี่
    sess_prev_val = float(prev["Close"])
    try:
        raw = tk.history(period="7d", interval="1d", auto_adjust=False)["Close"].dropna()
        raw = raw[raw > 0]
        # จับคู่ด้วย "วันที่" ไม่ใช่ตำแหน่ง: ถ้า Yahoo ยังไม่ settle แท่งล่าสุด (Close = NaN)
        # dropna จะทิ้งแท่งนั้น ทำให้ iloc[-2] เลื่อนย้อนไปอีกหนึ่งเซสชัน
        # แล้ว % รายวันผิดทั้งขนาดและบางครั้งผิดเครื่องหมาย
        # (28 ส.ค. 2569: NVDA รายงาน +3.76% ทั้งที่จริงคือ -4.58%)
        asof_date = df.index[-1].date()
        before = raw[[i.date() < asof_date for i in raw.index]]
        if len(before):
            sess_prev_val = float(before.iloc[-1])
        elif len(raw) >= 2:
            sess_prev_val = float(raw.iloc[-2])
    except Exception:
        pass

    result = {
        **fund,
        "ticker": ticker,
        "price": price,
        "change_pct": (price / sess_prev_val - 1) * 100,
        # ราคาปิดของ session ก่อนหน้าราคาพาดหัว — เป็นฐานของ change_pct จริงๆ
        # (ต่างจาก prev_close ที่เป็น "ปิดล่าสุดที่จบแล้ว" ซึ่งนอกเวลาทำการจะเท่ากับ price)
        "sess_prev": sess_prev_val,
        "rsi": rsi_now,
        "ema20": float(last["EMA20"]),
        "ema50": float(last["EMA50"]),
        "ema200": float(last["EMA200"]),
        "macd_hist": float(last["MACD_HIST"]),
        "support": support,
        "resistance": resistance,
        # ระดับนี้มาจากไหน: "swing" = จุดกลับตัวจริงในกรอบ 60 แท่ง
        # "w52" = ถอยไปใช้กรอบ 52 สัปดาห์เพราะราคาทำจุดสูง/ต่ำสุดของกรอบ 60 แท่งไปแล้ว
        # None = ไม่มีจริง ๆ — อย่าอ่านว่า "ไม่มีแนวต้าน" โดยไม่ดู basis ก่อน
        "support_basis": sup_basis,
        "resistance_basis": res_basis,
        "atr": atr_now,
        "stop_suggest": price - 2 * atr_now,
        "target_suggest": price + 3 * atr_now,
        "vol_ratio": vol_ratio,
        # บอกให้ชัดว่า vol_ratio เทียบกับอะไร — ระหว่างตลาดเปิดเป็นการเทียบ "ณ เวลาเดียวกัน"
        # ไม่ใช่เทียบกับทั้งวัน ผู้ใช้ปลายทางจะได้ไม่อ่านผิดว่าวอลุ่มเบา
        "vol_ratio_basis": vol["basis"],
        "vol_ratio_clock_matched": vol["clock_matched"],
        "vol_ratio_label": vol["label"],
        "vol_ratio_raw": vol["raw_ratio"],
        "vol_session_pct": vol["session_pct"],
        "vol_asof": vol["asof"],
        "score": score,
        "confidence": confidence,
        "verdict": verdict,
        "signals": signals,
        "events": events,
        "asof": str(df.index[-1].date()),
        "vp": vp,
        "prediction": prediction,
        "reversal": reversal,
        "trapped": trapped_zone(vp, price),
        # กราฟจิ๋ว 30 วันสำหรับ sparkline บนการ์ด
        "spark": [round(float(x), 4) for x in df["Close"].tail(30)],
    }

    # เติมข้อมูลที่หายเมื่อ Yahoo info โดนบล็อก (เช่นบนเซิร์ฟเวอร์คลาวด์)
    # — คำนวณจากกราฟราคาที่มีอยู่แล้วแทน เพื่อให้เว็บสาธารณะได้ข้อมูลครบเท่าเครื่อง local
    if result["prev_close"] is None and len(df) >= 2:
        result["prev_close"] = float(df["Close"].iloc[-2])
    if result["w52h"] is None and len(df) >= 30:
        result["w52h"] = float(df["High"].tail(252).max())
        result["w52l"] = float(df["Low"].tail(252).min())
    if result["market_state"] is None:
        result["market_state"] = market_state

    # ชื่อพ้องของกรอบ 52 สัปดาห์ — เอกสาร/สคริปต์ภายนอกเรียกด้วยชื่อนี้กันบ่อย
    # ก่อนหน้านี้ผู้เรียกที่ใช้ชื่อ high_52w จะได้ None แล้วเข้าใจผิดว่าเครื่องมือคำนวณไม่ได้
    result["high_52w"] = result["w52h"]
    result["low_52w"] = result["w52l"]

    # ป้ายความสอดคล้องของสัญญาณ (ภาษาคน แทนตัวเลข % ที่ชวนเข้าใจผิดว่าคือโอกาสถูก)
    if confidence >= 70:
        result["confidence_label"] = "สัญญาณส่วนใหญ่ชี้ทางเดียวกัน"
    elif confidence >= 40:
        result["confidence_label"] = "สัญญาณค่อนข้างสอดคล้องกัน"
    else:
        result["confidence_label"] = "สัญญาณยังขัดแย้งกัน — ไม่ชัดเจน"

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

    pred = r.get("prediction")
    if pred:
        pct = pred["prob_up"] * 100
        if pct >= 58:
            reasons.append(f"สถิติย้อนหลัง: สภาวะแบบวันนี้เคยเกิด {pred['n']} ครั้ง — ครั้งที่ราคาสูงขึ้นใน 5 วันถัดมามี {pct:.0f}% (อัตราชนะ ไม่ใช่ขนาดการขึ้น)")
        elif pct <= 42:
            reasons.append(f"สถิติย้อนหลัง: สภาวะแบบวันนี้เคยเกิด {pred['n']} ครั้ง — ครั้งที่ราคาต่ำลงใน 5 วันถัดมามี {100-pct:.0f}% (อัตราแพ้ ไม่ใช่ขนาดการลง)")

    vp = r.get("vp")
    sup = r.get("support")
    entry = f"{sup:,.2f}" if sup else f"{r['ema20']:,.2f} (EMA20)"
    plan = (f"จุดเข้าที่น่าสนใจ: แถวแนวรับ {entry} | "
            f"ตัดขาดทุนถ้าหลุด {r['stop_suggest']:,.2f} | "
            f"เป้าทำกำไรแรก {r['target_suggest']:,.2f}")
    if vp:
        plan += f" | โซนวอลุ่ม: POC {vp['poc']:,.2f} · VA {vp['val']:,.2f}-{vp['vah']:,.2f}"

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

    # สัญญาณเงินใหญ่ (สถาบัน/กองทุน/ผู้บริหาร) — เตือนเฉพาะระดับ "แรง" และไม่ซ้ำในวันเดียวกัน
    smart_lines = []
    for a in ((result.get("smart") or {}).get("alerts") or []):
        if a["level"] != "strong":
            continue
        key = f"sm_{a['kind']}_{a['dir']}"
        if sent.get(key) == today:
            continue
        sent[key] = today
        to_alert.append(key)
        icon = {"inst": "🏛", "fund": "💼", "insider": "👔"}.get(a["kind"], "•")
        smart_lines.append(f"{icon}{'🟢' if a['dir'] == 'buy' else '🔴'} {a['text']}")

    if not to_alert:
        return False

    lines = [f"{ticker}  {result['price']:,.2f} ({result['change_pct']:+.2f}%)",
             f"สรุป: {result['verdict']} (score {result['score']:+d}, มั่นใจ {result['confidence']:.0f}%)"]
    lines.extend(smart_lines)
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
    if r.get("vol_ratio_label"):
        print(f"                  ({r['vol_ratio_label']})")
    print(f"  จุดตัดขาดทุนแนะนำ (2xATR): {r['stop_suggest']:,.2f}")
    print(f"  เป้าหมายแนะนำ (3xATR)   : {r['target_suggest']:,.2f}")
    vp = r.get("vp")
    if vp:
        print(f"  Volume Profile: POC {vp['poc']:,.2f} | VA {vp['val']:,.2f} - {vp['vah']:,.2f}")
        if vp["touches"]:
            print(f"    สถิติโซน POC: แตะ {vp['touches']} ครั้ง เด้งกลับ {vp['bounces']} ทะลุ {vp['breaks']}")
    pred = r.get("prediction")
    if pred:
        print(f"  🔮 โอกาสขึ้นใน 5 วัน: {pred['prob_up']*100:.0f}% (จากเหตุการณ์คล้ายกัน {pred['n']} ครั้ง)")
    sm = r.get("smart")
    if sm and sm.get("alerts"):
        print("  🔔 สัญญาณเงินใหญ่:")
        for a in sm["alerts"]:
            icon = {"inst": "🏛", "fund": "💼", "insider": "👔"}.get(a["kind"], "•")
            print(f"     {icon}{'🟢' if a['dir'] == 'buy' else '🔴'} {a['text']}")
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

    # สัญญาณเงินใหญ่: ดึงครั้งเดียวสำหรับทั้ง watchlist (แคช 12 ชม. ในโมดูล news)
    smart = {}
    if alert:
        try:
            import news
            smart = {s["ticker"]: s for s in news.smart_money_signals(tickers)}
        except Exception as e:
            print(f"  [สัญญาณเงินใหญ่ดึงไม่ได้: {e}]")

    for t in tickers:
        try:
            r = analyze(t, cfg)
            r["smart"] = smart.get(t)
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
