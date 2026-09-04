# -*- coding: utf-8 -*-
"""
Investing Pro — Fundamentals Module
ข้อมูลพื้นฐานรายตัว: มูลค่า / กระแสเงินสด / มาร์จิ้น / งบดุล / ปันผล
พร้อม "คำอธิบายภาษาคน" ต่อทุกตัวเลข เพื่อให้มือใหม่อ่านแล้วเข้าใจว่าเลขนี้ดีหรือไม่ดี
และกราฟแท่งรายปีย้อนหลัง (รายได้ / กำไรสุทธิ / EPS / FCF)
"""

import time

import yfinance as yf

_cache = {}       # ticker -> (ts, payload)
_TTL = 6 * 3600   # งบการเงินเปลี่ยนไตรมาสละครั้ง — แคช 6 ชม.พอ


def _num(v):
    """แปลงเป็น float ที่ JSON ส่งได้ (กัน NaN/Inf ที่ทำให้เบราว์เซอร์อ่าน JSON ไม่ผ่าน)"""
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except Exception:
        return None


def _cap_label(mc):
    if mc is None:
        return None, None
    if mc >= 200e9:
        return "บริษัทขนาดใหญ่มาก (Mega Cap)", "มั่นคง ผันผวนน้อยกว่า แต่โตช้ากว่า"
    if mc >= 10e9:
        return "บริษัทขนาดใหญ่ (Large Cap)", "ธุรกิจตั้งตัวได้แล้ว ความเสี่ยงปานกลาง"
    if mc >= 2e9:
        return "บริษัทขนาดกลาง (Mid Cap)", "โตได้เร็วกว่าแต่ผันผวนกว่า"
    if mc >= 300e6:
        return "บริษัทขนาดเล็ก (Small Cap)", "โอกาสโตสูง แต่เสี่ยงและผันผวนมาก"
    return "บริษัทขนาดจิ๋ว (Micro Cap)", "เสี่ยงสูงมาก สภาพคล่องน้อย"


def _fmt_money(v):
    if v is None:
        return "—"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a/1e12:.2f}T"
    if a >= 1e9:
        return f"{sign}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:.0f}M"
    return f"{sign}${a:,.0f}"


def _metrics(info, price):
    """สร้างรายการตัวชี้วัด: ค่า + ระดับ (good/ok/warn/bad) + คำอธิบายภาษาคน"""
    out = []

    def add(key, label, value_txt, tone, note):
        out.append({"key": key, "label": label, "value": value_txt,
                    "tone": tone, "note": note})

    # --- มูลค่าตลาด ---
    mc = _num(info.get("marketCap"))
    cap_lbl, cap_note = _cap_label(mc)
    if mc:
        add("mcap", "มูลค่าตลาด", _fmt_money(mc), "info", f"{cap_lbl} — {cap_note}")

    # --- P/E ---
    pe = _num(info.get("trailingPE"))
    if pe and pe > 0:
        if pe < 15:
            tone, note = "good", "ถูกเมื่อเทียบกำไรปัจจุบัน — ตลาดยังไม่คาดหวังการเติบโตสูง"
        elif pe < 25:
            tone, note = "ok", "สมเหตุสมผล ใกล้ค่าเฉลี่ยตลาด (~20)"
        elif pe < 40:
            tone, note = "warn", "แพงกว่าค่าเฉลี่ยตลาด — ต้องโตต่อเนื่องถึงจะคุ้ม"
        else:
            tone, note = "bad", "แพงมากเมื่อเทียบกำไรปัจจุบัน — ต้องโตเร็วจริงถึงคุ้ม"
        add("pe", "P/E", f"{pe:.1f}", tone, note)
    else:
        add("pe", "P/E", "—", "info", "ไม่มีค่า P/E (กำไรติดลบหรือไม่มีข้อมูล) — ประเมินด้วยกำไรไม่ได้")

    # --- Forward P/E เทียบ P/E ปัจจุบัน ---
    fpe = _num(info.get("forwardPE"))
    if fpe and fpe > 0:
        if pe and fpe < pe * 0.9:
            tone, note = "good", "ถูกกว่า P/E ปัจจุบัน แปลว่าตลาดคาดว่ากำไรปีหน้าจะโต"
        elif pe and fpe > pe * 1.1:
            tone, note = "warn", "สูงกว่า P/E ปัจจุบัน แปลว่าตลาดคาดว่ากำไรปีหน้าจะลด"
        else:
            tone, note = "ok", "ใกล้เคียง P/E ปัจจุบัน — ตลาดคาดกำไรทรงตัว"
        add("fpe", "Forward P/E", f"{fpe:.1f}", tone, note)

    # --- P/S ---
    ps = _num(info.get("priceToSalesTrailing12Months"))
    if ps and ps > 0:
        if ps < 2:
            tone, note = "good", "ถูกเมื่อเทียบยอดขาย"
        elif ps < 6:
            tone, note = "ok", "ปานกลางเมื่อเทียบยอดขาย"
        elif ps < 12:
            tone, note = "warn", "สูงเมื่อเทียบยอดขาย — ราคาสะท้อนความคาดหวังการเติบโต"
        else:
            tone, note = "bad", "สูงมากเมื่อเทียบยอดขาย — เสี่ยงถ้าการเติบโตชะลอ"
        add("ps", "P/S", f"{ps:.1f}", tone, note)

    # --- P/B ---
    pb = _num(info.get("priceToBook"))
    if pb and pb > 0:
        tone = "good" if pb < 1.5 else "ok" if pb < 5 else "warn"
        note = ("ต่ำกว่ามูลค่าทางบัญชีไม่มาก" if pb < 1.5
                else "ปกติสำหรับธุรกิจที่ใช้สินทรัพย์ไม่หนัก" if pb < 5
                else "สูงเมื่อเทียบมูลค่าทางบัญชี — มูลค่าอยู่ที่แบรนด์/เทคโนโลยีมากกว่าทรัพย์สิน")
        add("pb", "P/B", f"{pb:.1f}", tone, note)

    # --- FCF ---
    fcf = _num(info.get("freeCashflow"))
    if fcf is not None:
        if fcf > 0:
            tone, note = "good", "สร้างเงินสดได้ ใช้ขยายธุรกิจ จ่ายปันผล หรือซื้อหุ้นคืนได้"
        else:
            tone, note = "bad", "เงินสดติดลบ — ต้องพึ่งเงินกู้หรือเพิ่มทุนมาหมุน"
        add("fcf", "FCF (เงินสดอิสระ)", _fmt_money(fcf), tone, note)

        # --- FCF Yield = เงินสดอิสระเทียบมูลค่าบริษัท ---
        if mc and mc > 0:
            fy = fcf / mc * 100
            if fy >= 5:
                tone, note = "good", "สูง เงินสดที่ได้คุ้มเมื่อเทียบราคา"
            elif fy >= 3:
                tone, note = "ok", "พอใช้ได้เมื่อเทียบราคา"
            elif fy > 0:
                tone, note = "warn", "ต่ำ ราคาหุ้นแพงเมื่อเทียบเงินสดที่ทำได้"
            else:
                tone, note = "bad", "ติดลบ ธุรกิจยังเผาเงินสด"
            add("fcfy", "FCF Yield", f"{fy:.2f}%", tone, note)

    # --- มาร์จิ้น ---
    gm = _num(info.get("grossMargins"))
    if gm is not None:
        g = gm * 100
        tone = "good" if g >= 50 else "ok" if g >= 30 else "warn"
        note = ("สูงมาก มีอำนาจตั้งราคา" if g >= 50
                else "ปานกลาง" if g >= 30 else "บาง แข่งขันด้วยราคาเป็นหลัก")
        add("gm", "Gross Margin", f"{g:.2f}%", tone, note)

    om = _num(info.get("operatingMargins"))
    if om is not None:
        o = om * 100
        if o >= 20:
            tone, note = "good", "แข็งแรง คุมต้นทุนดำเนินงานได้ดี"
        elif o >= 10:
            tone, note = "ok", "พอใช้ได้"
        elif o > 0:
            tone, note = "warn", "มาร์จิ้นบาง กำไรต่อยอดขายไม่มาก"
        else:
            tone, note = "bad", "ขาดทุนจากการดำเนินงาน"
        add("om", "Operating Margin", f"{o:.2f}%", tone, note)

    nm = _num(info.get("profitMargins"))
    if nm is not None:
        n = nm * 100
        if n >= 20:
            tone, note = "good", "เหลือกำไรสุทธิสูง"
        elif n >= 8:
            tone, note = "ok", "เหลือกำไรพอสมควร"
        elif n > 0:
            tone, note = "warn", "กำไรสุทธิบาง"
        else:
            tone, note = "bad", "ขาดทุนสุทธิ"
        add("nm", "Net Margin", f"{n:.2f}%", tone, note)

    # --- การเติบโต ---
    rg = _num(info.get("revenueGrowth"))
    if rg is not None:
        r = rg * 100
        if r >= 20:
            tone, note = "good", "โตเร็วมาก ธุรกิจขยายตัวแรง"
        elif r >= 8:
            tone, note = "ok", "โตต่อเนื่องในระดับดี"
        elif r > 0:
            tone, note = "warn", "โตช้า"
        else:
            tone, note = "bad", "รายได้หดตัว — ธุรกิจกำลังถูกกดดัน"
        add("rg", "โตของรายได้", f"{r:+.1f}% YoY", tone, note)

    eg = _num(info.get("earningsGrowth"))
    if eg is not None:
        e = eg * 100
        if e >= 20:
            tone, note = "good", "กำไรโตแรง"
        elif e > 0:
            tone, note = "ok", "กำไรยังโต"
        else:
            tone, note = "bad", "กำไรหด ระวังคุณภาพผลประกอบการ"
        add("eg", "โตของกำไร", f"{e:+.1f}% YoY", tone, note)

    # --- งบดุล ---
    cash = _num(info.get("totalCash"))
    debt = _num(info.get("totalDebt"))
    if cash is not None and debt is not None:
        net = cash - debt
        if net > 0:
            tone, note = "good", "เงินสดมากกว่าหนี้ — ฐานะการเงินแข็งแรง"
        else:
            tone, note = "warn", "หนี้มากกว่าเงินสด ดูความสามารถชำระหนี้ประกอบ"
        add("netcash", "เงินสดสุทธิ", _fmt_money(net), tone, note)

    cr = _num(info.get("currentRatio"))
    if cr is not None:
        if cr >= 2:
            tone, note = "good", "สภาพคล่องแข็งแรง จ่ายหนี้ระยะสั้นสบาย"
        elif cr >= 1:
            tone, note = "ok", "สภาพคล่องพอเพียง"
        else:
            tone, note = "warn", "ต่ำกว่า 1 หนี้ระยะสั้นอาจกดดัน"
        add("cr", "Current Ratio", f"{cr:.2f}", tone, note)

    de = _num(info.get("debtToEquity"))
    if de is not None:
        tone = "good" if de < 50 else "ok" if de < 120 else "warn"
        note = ("หนี้น้อยเมื่อเทียบทุน" if de < 50
                else "หนี้อยู่ในระดับจัดการได้" if de < 120
                else "หนี้สูงเมื่อเทียบทุน — เสี่ยงถ้าดอกเบี้ยขึ้นหรือกำไรสะดุด")
        add("de", "หนี้ต่อทุน (D/E)", f"{de:.0f}%", tone, note)

    roe = _num(info.get("returnOnEquity"))
    if roe is not None:
        r = roe * 100
        tone = "good" if r >= 15 else "ok" if r >= 8 else "warn"
        note = ("ใช้เงินทุนสร้างกำไรได้เก่ง" if r >= 15
                else "ผลตอบแทนต่อทุนพอใช้" if r >= 8 else "ผลตอบแทนต่อทุนต่ำ")
        add("roe", "ROE (ผลตอบแทนต่อทุน)", f"{r:.1f}%", tone, note)

    # --- ปันผล ---
    # คิดจาก "ปันผลต่อหุ้นต่อปี ÷ ราคา" เป็นหลัก เพราะไม่มีปัญหาเรื่องหน่วย
    # ของเดิมเดาหน่วยจากขนาด (ถ้า <1 ถือว่าเป็นสัดส่วนแล้วคูณ 100) ซึ่งผิดกับหุ้นที่
    # ปันผลต่ำกว่า 1% ทุกตัว — ตรวจแล้ว Yahoo ส่ง dividendYield มาเป็น "เปอร์เซ็นต์" เสมอ
    # (NVDA 0.45 = 0.45% แต่ถูกอ่านเป็น 45%, AAPL 0.33 → 33%) แถมยังไปเข้าเกณฑ์
    # "ปันผลดี ≥3%" ทำให้หุ้นเติบโตถูกป้ายว่าเป็นหุ้นปันผลชั้นดี
    # ยืนยันด้วย dividendRate/price: NVDA 1.00/227.42 = 0.44% · KO 2.12/89.12 = 2.38%
    rate = _num(info.get("dividendRate")) or _num(info.get("trailingAnnualDividendRate"))
    dy = (rate / price * 100) if (rate and price) else _num(info.get("dividendYield"))
    if dy:
        d = dy
        pr = _num(info.get("payoutRatio"))
        tone = "good" if d >= 3 else "ok"
        note = f"ได้ปันผลระหว่างถือ{' · จ่ายจากกำไร ' + format(pr*100, '.0f') + '%' if pr else ''}"
        add("div", "เงินปันผล", f"{d:.2f}%", tone, note)
    else:
        add("div", "เงินปันผล", "ไม่จ่ายปันผล", "info",
            "โฟกัสการเติบโตหรือเก็บเงินสดในธุรกิจ — กำไรมาจากส่วนต่างราคาอย่างเดียว")

    return out


def _annual(tk):
    """กราฟแท่งรายปี: รายได้ / กำไรสุทธิ / EPS / FCF (Yahoo ให้ย้อนหลัง ~5 ปี)"""
    series = {}

    def pick(df, names):
        if df is None or getattr(df, "empty", True):
            return None
        for n in names:
            for idx in df.index:
                if str(idx).strip() == n:
                    return df.loc[idx]
        return None

    def to_points(row):
        if row is None:
            return []
        pts = []
        for col, val in row.items():
            v = _num(val)
            if v is None:
                continue
            pts.append({"year": str(col)[:4], "value": v})
        pts.sort(key=lambda p: p["year"])
        return pts

    try:
        inc = tk.income_stmt
        series["revenue"] = to_points(pick(inc, ["Total Revenue"]))
        series["net_income"] = to_points(pick(inc, ["Net Income", "Net Income Common Stockholders"]))
        series["eps"] = to_points(pick(inc, ["Diluted EPS", "Basic EPS"]))
    except Exception:
        pass
    try:
        cf = tk.cashflow
        series["fcf"] = to_points(pick(cf, ["Free Cash Flow"]))
    except Exception:
        pass
    return {k: v for k, v in series.items() if v}


_earn_cache = {}      # ticker -> (ts, payload)


def _fmt_big(v):
    """ย่อตัวเลขใหญ่เป็นภาษาคน: 41,456,000,000 → 41.46 พันล้าน"""
    if v is None:
        return None
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}{a/1e12:.2f} ล้านล้าน"
    if a >= 1e9:
        return f"{sign}{a/1e9:.2f} พันล้าน"
    if a >= 1e6:
        return f"{sign}{a/1e6:.0f} ล้าน"
    return f"{sign}{a:,.0f}"


def get_earnings(ticker, force=False):
    """สรุปผลประกอบการไตรมาสล่าสุดหลังประกาศ + สถิติย้อนหลัง (ภาษาไทย)

    ใช้ EPS ที่ตลาดคาด เทียบกับที่ประกาศจริง (surprise%) ซึ่งเป็นตัวที่ราคาหุ้น
    ตอบสนองมากที่สุด บวกกับรายได้/กำไรของไตรมาสนั้นเทียบปีก่อน
    """
    ticker = ticker.strip().upper()
    now = time.time()
    c = _earn_cache.get(ticker)
    if c and not force and now - c[0] < _TTL:
        return c[1]

    tk = yf.Ticker(ticker)
    try:
        ed = tk.earnings_dates
    except Exception:
        ed = None
    if ed is None or getattr(ed, "empty", True):
        return {"ok": False, "error": f"ไม่มีข้อมูลผลประกอบการของ {ticker}"}

    rows = []
    for idx, r in ed.iterrows():
        rows.append({
            "date": str(idx)[:10],
            "ts": idx,
            "est": _num(r.get("EPS Estimate")),
            "actual": _num(r.get("Reported EPS")),
            "surprise": _num(r.get("Surprise(%)")),
        })
    rows.sort(key=lambda x: x["date"], reverse=True)

    # Yahoo ปนแถวเก่าที่ไม่มีตัวเลข EPS มาด้วย (บางตัวย้อนไปหลายปี)
    # ถ้าไม่กรองด้วยวันที่ จะไปหยิบ "ประกาศครั้งถัดไป" เป็นวันในอดีต (เจอกับ IREN: ได้ 2024-05-15)
    from datetime import date as _date, datetime as _dt
    today_s = str(_date.today())
    reported = [r for r in rows if r["actual"] is not None]
    upcoming = [r for r in rows if r["actual"] is None and r["date"] >= today_s]
    upcoming.sort(key=lambda x: x["date"])          # ใกล้ที่สุดก่อน
    if not reported:
        return {"ok": False, "error": f"ยังไม่มีผลประกอบการที่ประกาศแล้วของ {ticker}"}

    last = reported[0]
    sp = last["surprise"]
    if sp is None and last["est"]:
        sp = (last["actual"] / last["est"] - 1) * 100

    if sp is None:
        label, tone = "ประกาศแล้ว", "info"
    elif sp >= 10:
        label, tone = "เกินคาดมาก", "good"
    elif sp >= 2:
        label, tone = "ดีกว่าคาด", "good"
    elif sp > -2:
        label, tone = "ใกล้เคียงที่คาด", "ok"
    elif sp > -10:
        label, tone = "ต่ำกว่าคาด", "warn"
    else:
        label, tone = "ต่ำกว่าคาดมาก", "bad"

    # สถิติ: เกินคาดติดต่อกันกี่ไตรมาส + กี่ครั้งจาก 8 ครั้งล่าสุด
    streak = 0
    for r in reported:
        if (r["surprise"] or 0) > 0:
            streak += 1
        else:
            break
    last8 = reported[:8]
    beats = sum(1 for r in last8 if (r["surprise"] or 0) > 0)

    # รายได้/กำไรของไตรมาสนั้น เทียบไตรมาสเดียวกันปีก่อน
    rev = rev_yoy = ni = ni_yoy = None
    try:
        q = tk.quarterly_income_stmt

        def pick(name):
            for i in q.index:
                if str(i).strip() == name:
                    return [_num(v) for v in q.loc[i].values]
            return []
        rv = pick("Total Revenue")
        nis = pick("Net Income")
        if rv:
            rev = rv[0]
            if len(rv) >= 5 and rv[4]:
                rev_yoy = (rv[0] / rv[4] - 1) * 100
        if nis:
            ni = nis[0]
            if len(nis) >= 5 and nis[4]:
                ni_yoy = (nis[0] / nis[4] - 1) * 100
    except Exception:
        pass

    # สรุปเป็นประโยคภาษาคน
    parts = []
    if sp is not None:
        v = "สูงกว่า" if sp > 0 else ("ต่ำกว่า" if sp < 0 else "เท่ากับ")
        parts.append(f"กำไรต่อหุ้นออกมา {last['actual']:.2f} ดอลลาร์ "
                     f"{v}ที่ตลาดคาดไว้ ({last['est']:.2f}) {abs(sp):.1f}%")
    if rev is not None:
        s = f"รายได้ไตรมาสนี้ {_fmt_big(rev)} ดอลลาร์"
        if rev_yoy is not None:
            s += f" {'โต' if rev_yoy >= 0 else 'ลดลง'} {abs(rev_yoy):.1f}% จากไตรมาสเดียวกันปีก่อน"
        parts.append(s)
    if ni is not None and ni_yoy is not None:
        parts.append(f"กำไรสุทธิ {_fmt_big(ni)} ดอลลาร์ "
                     f"{'โต' if ni_yoy >= 0 else 'ลดลง'} {abs(ni_yoy):.1f}% เทียบปีก่อน")
    if len(last8) >= 4:
        parts.append(f"สถิติ 8 ไตรมาสล่าสุด ทำได้เกินคาด {beats} ครั้ง"
                     + (f" (เกินคาดติดต่อกัน {streak} ไตรมาส)" if streak >= 2 else ""))

    payload = {
        "ok": True,
        "ticker": ticker,
        "latest": {
            "date": last["date"], "est": last["est"], "actual": last["actual"],
            "surprise": round(sp, 2) if sp is not None else None,
            "label": label, "tone": tone,
            "revenue": rev, "revenue_txt": _fmt_big(rev),
            "revenue_yoy": round(rev_yoy, 1) if rev_yoy is not None else None,
            "net_income": ni, "net_income_txt": _fmt_big(ni),
            "net_income_yoy": round(ni_yoy, 1) if ni_yoy is not None else None,
        },
        "summary_th": " · ".join(parts),
        "beat_streak": streak,
        "beats_of_8": beats,
        "history": [{"date": r["date"], "est": r["est"], "actual": r["actual"],
                     "surprise": round(r["surprise"], 2) if r["surprise"] is not None else None}
                    for r in reported[:8]],
        "next": ({"date": upcoming[0]["date"], "est": upcoming[0]["est"]}
                 if upcoming else None),
        # บอกอายุข้อมูล เพื่อให้เห็นเองว่า "ยังไม่อัปเดต" หรือ "บริษัทยังไม่ประกาศ"
        "days_since": (_date.today() - _dt.strptime(last["date"], "%Y-%m-%d").date()).days,
    }
    _earn_cache[ticker] = (now, payload)
    if len(_earn_cache) > 60:
        for k in sorted(_earn_cache, key=lambda k: _earn_cache[k][0])[:30]:
            _earn_cache.pop(k, None)
    return payload


def _row(df, names):
    """ดึงแถวจากงบการเงินตามชื่อบรรทัด (Yahoo ตั้งชื่อไม่เหมือนกันทุกบริษัท จึงลองหลายชื่อ)"""
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        for idx in df.index:
            if str(idx).strip() == n:
                return df.loc[idx]
    return None


def _latest(row):
    """ค่าล่าสุด (คอลัมน์ซ้ายสุด = งวดใหม่สุด)"""
    if row is None:
        return None
    for val in row:
        v = _num(val)
        if v is not None:
            return v
    return None


def _prev(row):
    """ค่างวดก่อนหน้า — ใช้คำนวณอัตราเติบโต"""
    if row is None:
        return None
    seen = 0
    for val in row:
        v = _num(val)
        if v is None:
            continue
        seen += 1
        if seen == 2:
            return v
    return None


def _ttm(row, n=4):
    """รวม n ไตรมาสล่าสุด = ตัวเลข 12 เดือนย้อนหลัง

    ต้องใช้ TTM ไม่ใช่งบรายปี เพราะ tk.info รายงานเป็น TTM — ถ้าเทียบกับปีบัญชีล่าสุด
    บริษัทที่โตเร็วจะเพี้ยนมาก (วัดกับ NVDA: มาร์จิ้นสุทธิ 55.6% แบบรายปี เทียบกับ
    63.7% แบบ TTM ที่ Yahoo รายงาน)
    """
    if row is None:
        return None
    vals = [v for v in (_num(x) for x in row) if v is not None][:n]
    return sum(vals) if len(vals) == n else None


def _ttm_prev(row, n=4):
    """TTM ของ 4 ไตรมาสก่อนหน้า — ใช้เทียบอัตราเติบโตแบบ TTM ต่อ TTM"""
    if row is None:
        return None
    vals = [v for v in (_num(x) for x in row) if v is not None][n:n * 2]
    return sum(vals) if len(vals) == n else None


def _yoy(row, n=4):
    """โตเทียบไตรมาสเดียวกันปีก่อน (งวดล่าสุด ÷ งวดที่ n ก่อนหน้า)

    ใช้เมื่อเทียบ TTM ต่อ TTM ไม่ได้ — Yahoo ให้งบรายไตรมาสมาแค่ 5-6 งวด
    ซึ่งไม่พอสำหรับ TTM สองชุด (ต้องใช้ 8 งวด)
    ห้ามถอยไปเทียบ "ไตรมาสก่อนหน้า" เฉยๆ เพราะเป็นคนละตัวชี้วัด และจะเพี้ยนหนัก
    กับธุรกิจที่มีฤดูกาล — วัดกับ NVDA: เทียบไตรมาสติดกันได้ 19.8% แต่ของจริงเทียบปีต่อปี 85%
    """
    if row is None:
        return None
    vals = [v for v in (_num(x) for x in row) if v is not None]
    if len(vals) > n and vals[n]:
        return vals[0] / vals[n] - 1
    return None


def _div(a, b):
    return (a / b) if (a is not None and b) else None


def _growth(row):
    a, b = _latest(row), _prev(row)
    return ((a / b - 1) if (a is not None and b) else None)


def _info_fallback(ticker):
    """สร้าง info ขึ้นมาเองเมื่อ tk.info ว่าง

    ทำไมต้องมี: Yahoo ไม่ส่ง quoteSummary (ซึ่งคือที่มาของ tk.info) ให้ IP ศูนย์ข้อมูล
    หน้า "ข้อมูลพื้นฐาน" บนเว็บออนไลน์เลยพังทั้งหน้า ขึ้น 404 ตลอด (เจอ 3 ก.ย. 69)
    แต่ของอีกสองแหล่งยังใช้ได้ปกติบน Render — ตรวจแล้วไม่ใช่เดา:
      • ฟีดราคาแบบ batch (v7/finance/quote) → มูลค่าตลาด P/E P/B ปันผล EPS ราคา
      • งบการเงิน (income_stmt / balance_sheet / cashflow) → มาร์จิ้น เติบโต หนี้ ROE FCF
    จึงประกอบสองแหล่งนี้ให้ได้ค่าเกือบครบเท่า tk.info
    ที่ได้คืนมาไม่ได้จริง ๆ มีแค่ sector/industry ซึ่งเป็นป้ายชื่อ ไม่ใช่ตัวเลขที่ใช้ตัดสินใจ
    """
    import investing_pro as _core
    out = {}

    q = _core.raw_quotes([ticker]).get(ticker.upper())
    if q and q.get("_raw"):
        r = q["_raw"]
        out.update({
            "marketCap": r.get("marketCap"),
            "regularMarketPrice": r.get("regularMarketPrice"),
            "previousClose": r.get("regularMarketPreviousClose"),
            "trailingPE": r.get("trailingPE"),
            "forwardPE": r.get("forwardPE"),
            "priceToBook": r.get("priceToBook"),
            "longName": r.get("longName"),
            "shortName": r.get("shortName") or r.get("displayName"),
            "fullExchangeName": r.get("fullExchangeName"),
            "exchange": r.get("exchange"),
            "quoteType": r.get("quoteType"),
        })
        # หน่วยเดียวกับ tk.info เป๊ะ: dividendYield เป็นเปอร์เซ็นต์, dividendRate เป็นบาท/ดอลลาร์ต่อหุ้น
        out["dividendYield"] = _num(r.get("dividendYield"))
        out["dividendRate"] = _num(r.get("dividendRate")) or _num(r.get("trailingAnnualDividendRate"))

    tk = yf.Ticker(ticker)

    # ฟีดราคาไม่มาด้วย (บน Render โดนปิดเป็นช่วงๆ ทั้งฟีด) → เอาราคาจากกราฟแทน
    # กราฟ (history) เป็นแหล่งเดียวที่ยังไม่เคยโดนบล็อกเลย จึงใช้เป็นพื้นสุดท้าย
    if not out.get("regularMarketPrice"):
        try:
            h = tk.history(period="5d")
            if h is not None and not h.empty:
                out["regularMarketPrice"] = float(h["Close"].iloc[-1])
                if len(h) > 1:
                    out["previousClose"] = float(h["Close"].iloc[-2])
        except Exception:
            pass
    if not out.get("regularMarketPrice"):
        return out          # ไม่มีแม้แต่ราคา — ทำอะไรต่อไม่ได้

    def grab(name, fallback=None):
        for attr in (name, fallback):
            if not attr:
                continue
            try:
                df = getattr(tk, attr)
                if df is not None and not getattr(df, "empty", True):
                    return df
            except Exception:
                pass
        return None

    # งบ "รายไตรมาส" เป็นหลัก แล้วรวม 4 ไตรมาสเป็น TTM ให้ตรงหน่วยกับ tk.info
    # ถอยไปใช้งบรายปีเมื่อบริษัทไม่มีงบรายไตรมาส (หุ้นเล็ก/ตลาดต่างประเทศบางแห่ง)
    inc = grab("quarterly_income_stmt", "income_stmt")
    bal = grab("quarterly_balance_sheet", "balance_sheet")
    cf = grab("quarterly_cashflow", "cashflow")

    rev = _row(inc, ["Total Revenue"])
    ni = _row(inc, ["Net Income", "Net Income Common Stockholders"])
    gross = _row(inc, ["Gross Profit"])
    op = _row(inc, ["Operating Income", "EBIT"])

    # ถ้ารวม 4 ไตรมาสไม่ครบ (งบรายปี หรือไตรมาสไม่พอ) ให้ใช้ค่างวดล่าสุดแทน
    rev_t = _ttm(rev) or _latest(rev)
    ni_t = _ttm(ni) or _latest(ni)

    out.setdefault("profitMargins", _div(ni_t, rev_t))
    out.setdefault("grossMargins", _div(_ttm(gross) or _latest(gross), rev_t))
    out.setdefault("operatingMargins", _div(_ttm(op) or _latest(op), rev_t))

    rev_p, ni_p = _ttm_prev(rev), _ttm_prev(ni)
    out.setdefault("revenueGrowth",
                   (rev_t / rev_p - 1) if (rev_t and rev_p) else _yoy(rev))
    out.setdefault("earningsGrowth",
                   (ni_t / ni_p - 1) if (ni_t and ni_p) else _yoy(ni))

    # งบดุลเป็นภาพ ณ วันสิ้นงวด ไม่ใช่ยอดสะสม → ใช้ไตรมาสล่าสุดตรงๆ ห้ามรวม
    eq = _latest(_row(bal, ["Stockholders Equity", "Total Equity Gross Minority Interest"]))
    debt = _latest(_row(bal, ["Total Debt"]))
    if debt is None:      # บางบริษัทไม่มีบรรทัด Total Debt ต้องบวกหนี้สั้น+ยาวเอง
        sd = _latest(_row(bal, ["Current Debt", "Current Debt And Capital Lease Obligation"]))
        ld = _latest(_row(bal, ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]))
        debt = (sd or 0) + (ld or 0) if (sd or ld) else None
    # เรียงจาก "กว้าง" ไป "แคบ": tk.info นับเงินสด+เงินลงทุนระยะสั้นเป็น totalCash
    cash = _latest(_row(bal, ["Cash Cash Equivalents And Short Term Investments",
                              "Cash And Cash Equivalents"]))
    ca = _latest(_row(bal, ["Current Assets", "Total Current Assets"]))
    cl = _latest(_row(bal, ["Current Liabilities", "Total Current Liabilities"]))

    out.setdefault("returnOnEquity", _div(ni_t, eq))
    # tk.info ส่ง debtToEquity มาเป็นเปอร์เซ็นต์ (เช่น 45.2) ไม่ใช่สัดส่วน — คูณ 100 ให้ตรงกัน
    dte = _div(debt, eq)
    out.setdefault("debtToEquity", (dte * 100) if dte is not None else None)
    out.setdefault("totalDebt", debt)
    out.setdefault("totalCash", cash)
    out.setdefault("currentRatio", _div(ca, cl))

    ocf_row = _row(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])
    capex_row = _row(cf, ["Capital Expenditure"])
    ocf = _ttm(ocf_row) or _latest(ocf_row)
    capex = _ttm(capex_row) or _latest(capex_row)
    if ocf is not None:
        out.setdefault("freeCashflow", ocf + (capex or 0))   # capex ติดลบมาอยู่แล้ว
    div_row = _row(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"])
    div_paid = _ttm(div_row) or _latest(div_row)
    if div_paid is not None and ni_t:
        out.setdefault("payoutRatio", abs(div_paid) / ni_t)

    # ค่าที่ปกติได้จากฟีดราคา — คำนวณเองจาก "จำนวนหุ้น x ราคา" เมื่อฟีดไม่มา
    px = out.get("regularMarketPrice")
    shares = _latest(_row(bal, ["Ordinary Shares Number", "Share Issued",
                                "Common Stock Shares Outstanding"]))
    if px and shares:
        out.setdefault("marketCap", px * shares)
        if eq:
            out.setdefault("priceToBook", px / (eq / shares))
        # ใช้ EPS ปรับลด (diluted) ที่งบรายงานไว้ก่อน เพราะเป็นตัวเดียวกับที่ Yahoo ใช้คิด P/E
        # ถ้าไม่มีบรรทัดนี้ค่อยหารเอง (กำไรสุทธิ ÷ จำนวนหุ้น) ซึ่งจะไม่นับหุ้นปรับลด
        eps_row = _row(inc, ["Diluted EPS", "Basic EPS"])
        eps_ttm = _ttm(eps_row) or _div(ni_t, shares)
        if eps_ttm and eps_ttm > 0:
            out.setdefault("trailingPE", px / eps_ttm)

    return {k: v for k, v in out.items() if v is not None}


def get_fundamentals(ticker, force=False):
    """คืนข้อมูลพื้นฐานรายตัว + คำอธิบายภาษาไทย + ชุดข้อมูลกราฟรายปี"""
    ticker = ticker.strip().upper()
    now = time.time()
    c = _cache.get(ticker)
    if c and not force and now - c[0] < _TTL:
        return c[1]

    tk = yf.Ticker(ticker)
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    partial = False
    if not info.get("marketCap") and not info.get("regularMarketPrice"):
        # tk.info ว่าง — บนเซิร์ฟเวอร์เกิดตลอดเพราะ Yahoo ปิด quoteSummary ให้ IP ศูนย์ข้อมูล
        # ประกอบเองจากฟีดราคา + งบการเงิน ซึ่งยังดึงได้ แทนที่จะตอบ 404 ทิ้งทั้งหน้า
        info = _info_fallback(ticker)
        partial = True
    if not info.get("marketCap") and not info.get("regularMarketPrice"):
        return {"ok": False, "error": f"ไม่มีข้อมูลพื้นฐานของ {ticker} (อาจเป็น ETF หรือ Yahoo ไม่ให้ข้อมูล)"}

    price = _num(info.get("regularMarketPrice")) or _num(info.get("previousClose"))
    payload = {
        "ok": True,
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName") or ticker,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "price": price,
        "metrics": _metrics(info, price),
        "annual": _annual(tk),
        "quote_type": info.get("quoteType"),
        # บอกตรงๆ ว่าชุดนี้ประกอบเอง ไม่ได้มาจากแหล่งเต็ม (sector/industry จะว่าง)
        "partial": partial,
    }
    _cache[ticker] = (now, payload)
    if len(_cache) > 60:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:30]:
            _cache.pop(k, None)
    return payload
