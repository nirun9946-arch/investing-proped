# -*- coding: utf-8 -*-
"""
Investing Pro — Smart Money Flow Module
สัญญาณเชิงโครงสร้างที่รายย่อยมักมองไม่เห็น เท่าที่ "ข้อมูลสาธารณะรองรับจริง"

ทำได้จริงในนี้ (ตรวจคุณภาพข้อมูลแล้วว่าเชื่อถือได้):
  • Options Flow — Put/Call จากวอลุ่มจริง, "เงินจริง" ที่วางแต่ละฝั่ง (premium $),
    สไตรก์ที่เงินไหลเข้าหนักสุด, และ Implied Move จากราคา straddle
  • Quiet Accumulation — สแกนการสะสมหุ้นในกรอบราคาแคบ (วอลุ่มผิดปกติแต่ราคานิ่ง)

ทำไม่ได้ด้วยข้อมูลฟรี — ตรวจแล้ว ไม่ใช่เดา:
  • GEX / Max Pain / Gamma Flip — ต้องใช้ Open Interest ซึ่ง Yahoo ส่งมาว่าง
    (SPY ได้ call OI รวมทั้งเชน 485 สัญญา ทั้งที่ของจริงหลักล้าน)
  • IV Skew / IV Term Structure — Yahoo ส่ง impliedVolatility = 0.00001 และ bid/ask = 0
    ที่สไตรก์ใกล้ ATM แทบทั้งหมด คำนวณออกมาจะได้ตัวเลขที่ไม่มีความหมาย
  • Dark pool prints รายทรานแซกชัน, tick data เรียลไทม์, Bloomberg IB Chat
  ทั้งหมดนี้ต้องใช้ฟีดที่เสียเงิน (CBOE / ORATS / Polygon / Bloomberg)
"""

import math
import time

import yfinance as yf

_cache = {}
_TTL = 900          # ออปชันเปลี่ยนเร็ว — แคช 15 นาทีพอ


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except Exception:
        return None


def _usable_iv(series):
    """IV ที่ใช้ได้จริง — ตัดค่าขยะ (0.00001 หรือ >300%) ที่ Yahoo ใส่มาเวลาไม่มีราคา"""
    return series[(series > 0.05) & (series < 3.0)]


def options_flow(ticker, force=False):
    """อ่านแรงซื้อขายฝั่งออปชันจากวอลุ่มจริง + โครงสร้าง IV"""
    ticker = ticker.strip().upper()
    now = time.time()
    c = _cache.get(ticker)
    if c and not force and now - c[0] < _TTL:
        return c[1]

    tk = yf.Ticker(ticker)
    try:
        exps = list(tk.options or [])
    except Exception:
        exps = []
    if not exps:
        # แยกให้ชัดว่า "หุ้นไม่มีออปชันจริง" กับ "แหล่งข้อมูลไม่ยอมให้"
        # Yahoo บล็อก endpoint ออปชันจาก IP ดาต้าเซ็นเตอร์ (เช่นเว็บบน Render)
        # แต่ยังให้ราคา/กราฟตามปกติ — ถ้าราคาดึงได้แสดงว่าโดนบล็อกเฉพาะออปชัน
        price_ok = False
        try:
            h = tk.history(period="5d", interval="1d")
            price_ok = h is not None and not h.empty
        except Exception:
            pass
        if price_ok:
            return {"ok": False, "blocked": True,
                    "error": ("แหล่งข้อมูลไม่ส่งข้อมูลออปชันมาให้เซิร์ฟเวอร์นี้ "
                              "(Yahoo ปิดกั้นการดึง option chain จากไอพีของดาต้าเซ็นเตอร์) — "
                              "ส่วนนี้จะใช้ได้เมื่อรันโปรแกรมในเครื่องตัวเอง")}
        return {"ok": False, "error": f"{ticker} ไม่มีตลาดออปชัน หรือดึงข้อมูลไม่สำเร็จ"}

    try:
        spot = _num(tk.fast_info.get("lastPrice")) or _num(tk.fast_info.get("last_price"))
    except Exception:
        spot = None
    if not spot:
        try:
            spot = _num((tk.info or {}).get("regularMarketPrice"))
        except Exception:
            spot = None
    if not spot:
        return {"ok": False, "error": "ดึงราคาปัจจุบันไม่ได้"}

    call_vol = put_vol = 0.0
    call_prem = put_prem = 0.0        # เงินจริงที่วางแต่ละฝั่ง = วอลุ่ม × ราคา × 100
    strikes = {}                      # strike -> {'c','p','cp','pp'}
    scanned = []
    implied_move = None               # จากราคา straddle ที่ ATM (ไม่ต้องใช้ IV)

    for i, exp in enumerate(exps[:5]):        # 5 วันหมดอายุแรก = ภาพแรงซื้อขายระยะสั้น
        try:
            oc = tk.option_chain(exp)
        except Exception:
            continue
        cdf, pdf = oc.calls, oc.puts
        cv = float(cdf["volume"].fillna(0).sum())
        pv = float(pdf["volume"].fillna(0).sum())
        cpm = float((cdf["volume"].fillna(0) * cdf["lastPrice"].fillna(0)).sum()) * 100
        ppm = float((pdf["volume"].fillna(0) * pdf["lastPrice"].fillna(0)).sum()) * 100
        call_vol += cv; put_vol += pv
        call_prem += cpm; put_prem += ppm
        scanned.append({"exp": exp, "call_vol": int(cv), "put_vol": int(pv),
                        "call_prem": int(cpm), "put_prem": int(ppm)})

        for df, vk, pk in ((cdf, "c", "cp"), (pdf, "p", "pp")):
            for st, vol, px in zip(df["strike"].values,
                                   df["volume"].fillna(0).values,
                                   df["lastPrice"].fillna(0).values):
                v = _num(vol) or 0
                if v <= 0:
                    continue
                s = round(float(st), 2)
                strikes.setdefault(s, {"c": 0.0, "p": 0.0, "cp": 0.0, "pp": 0.0})
                strikes[s][vk] += v
                strikes[s][pk] += v * (_num(px) or 0) * 100

        # Implied Move จากราคา straddle ที่ ATM — ตลาดคิดว่าราคาจะวิ่งกี่ % ถึงวันหมดอายุ
        # ใช้ราคาซื้อขายจริง ไม่ต้องพึ่ง IV ที่ Yahoo ส่งมาเสีย
        if implied_move is None:
            try:
                ci = cdf.iloc[(cdf["strike"] - spot).abs().argsort().iloc[0]]
                pi = pdf.iloc[(pdf["strike"] - spot).abs().argsort().iloc[0]]
                cpx, ppx = _num(ci["lastPrice"]), _num(pi["lastPrice"])
                if cpx and ppx and cpx > 0 and ppx > 0:
                    implied_move = {
                        "exp": exp,
                        "pct": round((cpx + ppx) / spot * 100, 2),
                        "abs": round(cpx + ppx, 2),
                        "strike": round(float(ci["strike"]), 2),
                    }
            except Exception:
                pass

    if call_vol + put_vol <= 0:
        return {"ok": False, "error": f"ยังไม่มีวอลุ่มออปชันของ {ticker} ในรอบนี้"}

    pc = put_vol / call_vol if call_vol else None
    prem_total = call_prem + put_prem
    call_share = (call_prem / prem_total * 100) if prem_total else None

    # ตีความ Put/Call จากวอลุ่มจริง
    if pc is None:
        pc_label, pc_tone = "—", "info"
    elif pc >= 1.3:
        pc_label, pc_tone = "ฝั่งพุตหนากว่ามาก — ตลาดซื้อประกันความเสี่ยงขาลงเยอะ", "warn"
    elif pc >= 0.9:
        pc_label, pc_tone = "พุต/คอลใกล้เคียงกัน — ยังไม่เอียงข้างชัด", "ok"
    elif pc >= 0.6:
        pc_label, pc_tone = "ฝั่งคอลหนากว่า — เอียงไปทางเก็งขาขึ้น", "good"
    else:
        pc_label, pc_tone = "คอลหนากว่ามาก — เก็งขาขึ้นร้อนแรง (ระวังกลับตัวถ้าสุดโต่ง)", "warn"

    # เงินจริงที่วางแต่ละฝั่ง — สำคัญกว่าจำนวนสัญญา เพราะสะท้อนขนาดเดิมพันจริง
    prem_label = prem_tone = None
    if call_share is not None:
        if call_share >= 70:
            prem_label, prem_tone = "เงินส่วนใหญ่วางฝั่งคอล — เดิมพันขาขึ้นด้วยเม็ดเงินจริง", "good"
        elif call_share >= 55:
            prem_label, prem_tone = "เงินเอียงไปฝั่งคอลเล็กน้อย", "ok"
        elif call_share >= 45:
            prem_label, prem_tone = "เงินสองฝั่งพอกัน — ยังไม่มีข้างไหนกดดันชัด", "ok"
        elif call_share >= 30:
            prem_label, prem_tone = "เงินเอียงไปฝั่งพุต — เริ่มมีการป้องกันขาลง", "warn"
        else:
            prem_label, prem_tone = "เงินส่วนใหญ่วางฝั่งพุต — จ่ายหนักเพื่อกันขาลง/เดิมพันลง", "warn"

    # สไตรก์ที่ "เงิน" ไหลเข้าหนักสุด — บอกกรอบราคาที่ตลาดกำลังเล่นจริง
    tops = sorted(strikes.items(), key=lambda kv: -(kv[1]["cp"] + kv[1]["pp"]))[:6]
    top_strikes = [{
        "strike": s,
        "call_vol": int(v["c"]), "put_vol": int(v["p"]),
        "total_vol": int(v["c"] + v["p"]),
        "premium": int(v["cp"] + v["pp"]),
        "side": "call" if v["cp"] > v["pp"] * 1.2 else ("put" if v["pp"] > v["cp"] * 1.2 else "mixed"),
        "vs_spot": round((s / spot - 1) * 100, 1),
    } for s, v in tops]

    payload = {
        "ok": True, "ticker": ticker, "spot": spot,
        "call_vol": int(call_vol), "put_vol": int(put_vol),
        "call_prem": int(call_prem), "put_prem": int(put_prem),
        "call_share": round(call_share, 1) if call_share is not None else None,
        "prem_label": prem_label, "prem_tone": prem_tone,
        "pc_ratio": round(pc, 2) if pc else None,
        "pc_label": pc_label, "pc_tone": pc_tone,
        "implied_move": implied_move,
        "top_strikes": top_strikes,
        "expiries_scanned": scanned,
        # บอกตรง ๆ ว่าอะไรคำนวณไม่ได้ และเพราะอะไร — กันเข้าใจผิดว่าระบบมีครบ
        "limits_note": ("GEX / Max Pain / Gamma Flip และ IV Skew คำนวณไม่ได้จากฟีดฟรี — "
                        "Yahoo ส่ง Open Interest มาว่าง และ IV ใกล้ ATM เป็น 0.00001 "
                        "(ตรวจแล้วทั้ง NVDA/SPY/AAPL) ต้องใช้ CBOE / ORATS / Polygon"),
    }
    _cache[ticker] = (now, payload)
    if len(_cache) > 40:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:20]:
            _cache.pop(k, None)
    return payload


def accumulation_scan(df, price, lookback=60):
    """สแกน 'การสะสมเงียบ' — ราคาแกว่งในกรอบแคบ แต่วอลุ่มหนากว่าปกติ

    เป็นรูปแบบที่เงินก้อนใหญ่ใช้เก็บของโดยไม่ดันราคา (Wyckoff accumulation)
    ถ้าเจอพร้อมกันหลายข้อ = มีโอกาสมีคนเก็บของอยู่ก่อนราคาจะออกจากกรอบ
    """
    if df is None or len(df) < lookback + 20:
        return None
    import numpy as np

    d = df.tail(lookback)
    hi, lo = float(d["High"].max()), float(d["Low"].min())
    if hi <= 0 or lo <= 0:
        return None
    rng_pct = (hi - lo) / lo * 100

    # กรอบราคาช่วงนี้ เทียบกับกรอบปกติของหุ้นตัวนี้เอง (ย้อนหลัง 1 ปี)
    prev = df.tail(252).head(max(0, 252 - lookback))
    base_rng = None
    if len(prev) >= 60:
        rolls = []
        for i in range(0, len(prev) - lookback, 10):
            w = prev.iloc[i:i + lookback]
            h2, l2 = float(w["High"].max()), float(w["Low"].min())
            if l2 > 0:
                rolls.append((h2 - l2) / l2 * 100)
        if rolls:
            base_rng = float(np.median(rolls))

    vol_recent = float(d["Volume"].mean())
    vol_prev = float(prev["Volume"].mean()) if len(prev) else None
    vol_ratio = (vol_recent / vol_prev) if vol_prev else None

    # แท่งเขียวกินวอลุ่มมากกว่าแท่งแดงไหม (แรงซื้อดูดของ)
    up_vol = float(d.loc[d["Close"] >= d["Open"], "Volume"].sum())
    dn_vol = float(d.loc[d["Close"] < d["Open"], "Volume"].sum())
    buy_pressure = (up_vol / (up_vol + dn_vol) * 100) if (up_vol + dn_vol) else None

    net_move = (float(d["Close"].iloc[-1]) / float(d["Close"].iloc[0]) - 1) * 100

    # ประตูด่านแรก: กรอบต้อง "แคบจริง" ในเชิงสัมบูรณ์ด้วย ไม่ใช่แค่แคบกว่าตัวเองที่เคยเหวี่ยงบ้าคลั่ง
    # (ไม่งั้นหุ้นผันผวนจัดอย่าง AXTI ที่กรอบ 218% จะถูกนับว่า "ราคานิ่ง" ซึ่งผิดความจริง)
    # เกณฑ์ตั้งจากการกระจายจริงของตลาด (สุ่มวัด 12 ตัว: กรอบ 60 วันมัธยฐาน ~23%,
    # สัดส่วนวอลุ่มฝั่งซื้อมัธยฐาน ~48%) จึงใช้ค่าที่ "ผิดปกติจริง" ไม่ใช่ค่ากลาง
    TIGHT_MAX = 30.0
    if rng_pct > TIGHT_MAX:
        return None
    # วอลุ่มเทไปฝั่งแท่งแดงชัดเจน = เป็นการกระจายของ ไม่ใช่สะสม
    if buy_pressure is not None and buy_pressure < 42:
        return None

    signals = []
    if base_rng and rng_pct < base_rng * 0.7:
        signals.append(f"กรอบราคาแคบกว่าปกติ {rng_pct:.1f}% (ปกติของตัวนี้ ~{base_rng:.1f}%) — ราคาถูกกดให้นิ่ง")
    elif rng_pct <= 18:
        signals.append(f"ราคาแกว่งในกรอบแคบเพียง {rng_pct:.1f}% ตลอด {lookback} วัน")
    if vol_ratio and vol_ratio >= 1.25:
        signals.append(f"วอลุ่มหนากว่าช่วงก่อน {vol_ratio:.2f} เท่า ทั้งที่ราคาไม่ไปไหน — มีคนรับของ")
    if buy_pressure and buy_pressure >= 55:
        signals.append(f"วอลุ่มไปอยู่ฝั่งแท่งเขียว {buy_pressure:.0f}% — แรงซื้อดูดของมากกว่าแรงขาย")
    if abs(net_move) < 4 and (vol_ratio or 0) >= 1.1:
        signals.append(f"ราคาสุทธิ {net_move:+.1f}% ในช่วง {lookback} วัน แต่วอลุ่มไม่ลด — สะสมแบบไม่ดันราคา")

    if len(signals) < 2:
        return None
    score = len(signals)
    label = ("มีร่องรอยการสะสมชัดเจน" if score >= 4
             else "น่าจะมีการสะสมอยู่" if score == 3 else "เริ่มมีสัญญาณสะสม")
    return {
        "score": score, "max": 4, "label": label,
        "range_pct": round(rng_pct, 1),
        "base_range_pct": round(base_rng, 1) if base_rng else None,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "buy_pressure": round(buy_pressure, 1) if buy_pressure else None,
        "net_move": round(net_move, 1),
        "box_low": round(lo, 2), "box_high": round(hi, 2),
        "signals": signals,
        "note": ("เป็นการอนุมานจากพฤติกรรมราคา-วอลุ่มบนกระดาน ไม่ใช่ข้อมูลดีลนอกกระดานจริง "
                 "(dark pool รายทรานแซกชันต้องใช้ฟีดที่เสียเงิน)"),
    }
