# -*- coding: utf-8 -*-
"""
Investing Pro — News Module
ดึงข่าวการเงินจากแหล่งที่เชื่อถือได้ (CNBC, MarketWatch, Yahoo Finance)
พร้อมแปลหัวข้อ/สรุปเป็นภาษาไทยอัตโนมัติ
"""

import concurrent.futures
import html
import re
import time
from collections import Counter
from datetime import datetime, timezone

import feedparser
import requests

# RSS ทางการของ CNBC และ MarketWatch (Dow Jones)
FEEDS = [
    ("CNBC Top News", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("CNBC Technology", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910"),
    ("CNBC Investing", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

_feed_cache = {"ts": 0, "items": []}   # ข่าว RSS (ใช้ร่วมกันทุกคน)
_yahoo_cache = {}                       # ข่าวรายหุ้น: ticker -> (ts, rows)
_tr_cache = {}                          # คำแปล: en -> th
CACHE_SECONDS = 300  # เก็บผล 5 นาที


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def translate_th(text):
    """แปลอังกฤษ→ไทย ผ่าน Google Translate (ไม่ต้องใช้ API key)"""
    if not text:
        return ""
    if text in _tr_cache:
        return _tr_cache[text]
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "th", "dt": "t", "q": text[:1500]},
            timeout=8,
        )
        segs = r.json()[0] or []
        result = "".join(s[0] for s in segs if s and s[0]).strip()
        if len(_tr_cache) > 800:
            _tr_cache.clear()
        _tr_cache[text] = result
        return result
    except Exception:
        return ""


def fetch_feed(name, url):
    try:
        # ดึงเองผ่าน requests พร้อม timeout — กัน feed ช้าแล้วลากทั้งหน้าให้ค้าง
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        d = feedparser.parse(resp.content)
        out = []
        for e in d.entries[:12]:
            ts = 0
            if getattr(e, "published_parsed", None):
                ts = time.mktime(e.published_parsed)
            title = clean(e.get("title"))
            if not title:
                continue
            out.append({
                "source": name,
                "title": title,
                "summary": clean(e.get("summary", ""))[:280],
                "link": e.get("link"),
                "ts": ts,
                "ticker": None,
            })
        return out
    except Exception:
        return []


def fetch_yahoo_one(t):
    """ข่าวรายตัวของหุ้นหนึ่งตัวจาก Yahoo Finance"""
    import yfinance as yf
    out = []
    try:
        for n in (yf.Ticker(t).news or [])[:3]:
                c = n.get("content", n) or n
                title = clean(c.get("title"))
                link = ((c.get("canonicalUrl") or {}).get("url")
                        or (c.get("clickThroughUrl") or {}).get("url")
                        or n.get("link"))
                if not title or not link:
                    continue
                prov = ((c.get("provider") or {}).get("displayName")
                        or n.get("publisher") or "Yahoo Finance")
                ts = 0
                if c.get("pubDate"):
                    try:
                        ts = datetime.fromisoformat(
                            c["pubDate"].replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
                elif n.get("providerPublishTime"):
                    ts = float(n["providerPublishTime"])
                out.append({
                    "source": prov,
                    "title": title,
                    "summary": clean(c.get("summary") or c.get("description") or "")[:280],
                    "link": link,
                    "ts": ts,
                    "ticker": t,
                })
    except Exception:
        pass
    return out


_last_news = {"items": []}  # ข่าวชุดล่าสุดที่สำเร็จ — แผนสำรองเมื่อรอบใหม่ล้มเหลว

# แหล่งที่มี paywall/บล็อกการดึงเนื้อหา — ตัดออก ให้เหลือเฉพาะข่าวที่กดอ่าน/แปลได้จริง
BLOCKED_SOURCES = {
    "motley fool", "the motley fool", "the wall street journal", "wsj",
    "barrons.com", "barron's", "investor's business daily",
    "bloomberg", "seeking alpha", "morningstar",
}


def get_news(tickers, max_items=30, force=False):
    """รวมข่าว RSS (แคชร่วม) + ข่าวรายหุ้น — ทุกขั้นตอนมีเส้นตาย ไม่มีทางค้างทั้งหน้า"""
    now = time.time()
    tickers = [t.upper() for t in tickers][:15]
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=10)
    try:
        # 1) ข่าว RSS ส่วนกลาง — รอไม่เกิน 15 วิ แหล่งไหนช้าโดนข้าม
        if force or not _feed_cache["items"] or now - _feed_cache["ts"] >= CACHE_SECONDS:
            futs = [ex.submit(fetch_feed, n, u) for n, u in FEEDS]
            done, _ = concurrent.futures.wait(futs, timeout=15)
            feed_items = []
            for f in done:
                try:
                    feed_items.extend(f.result())
                except Exception:
                    pass
            if feed_items:
                _feed_cache["ts"], _feed_cache["items"] = now, feed_items

        # 2) ข่าวรายหุ้น — เส้นตาย 15 วิ ตัวที่ค้างถูกทิ้งไว้ ไม่ลากทั้งหน้า
        need = [t for t in tickers
                if force or t not in _yahoo_cache or now - _yahoo_cache[t][0] >= CACHE_SECONDS]
        if need:
            futs = {ex.submit(fetch_yahoo_one, t): t for t in need}
            done, _ = concurrent.futures.wait(futs, timeout=15)
            for f in done:
                try:
                    _yahoo_cache[futs[f]] = (now, f.result())
                except Exception:
                    pass
        if len(_yahoo_cache) > 200:
            for k in sorted(_yahoo_cache, key=lambda k: _yahoo_cache[k][0])[:100]:
                _yahoo_cache.pop(k, None)

        items = list(_feed_cache["items"])
        for t in tickers:
            if t in _yahoo_cache:
                items.extend(_yahoo_cache[t][1])

        # เรียงใหม่สุดก่อน + ตัดข่าวซ้ำ + ตัดแหล่งที่อ่านไม่ได้ (paywall)
        seen, uniq = set(), []
        for it in sorted(items, key=lambda x: -x["ts"]):
            if it["source"].strip().lower() in BLOCKED_SOURCES:
                continue
            key = it["title"].lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        uniq = uniq[:max_items]

        # 3) แปลไทยเฉพาะข่าวที่ยังไม่มีคำแปล — งบเวลา 25 วิ
        #    คำแปลถูกเก็บติดกับข่าวในแคช: แปลสำเร็จครั้งเดียว ไม่แปลซ้ำอีก
        def tr(it):
            return (translate_th(it["title"]),
                    translate_th(it["summary"]) if it["summary"] else "")

        todo = [it for it in uniq if not it.get("title_th")]
        if todo:
            futs = {ex.submit(tr, it): it for it in todo}
            done, _ = concurrent.futures.wait(futs, timeout=25)
            for f in done:
                it = futs[f]
                try:
                    t_th, s_th = f.result()
                    if t_th:
                        it["title_th"] = t_th
                        it["summary_th"] = s_th or it["summary"]
                except Exception:
                    pass

        # ส่งออก: ข่าวที่ยังแปลไม่ได้ให้แสดงต้นฉบับ (ไม่บันทึกลงแคช จะได้ลองแปลใหม่รอบหน้า)
        result = [{**it,
                   "title_th": it.get("title_th") or it["title"],
                   "summary_th": it.get("summary_th") or it["summary"]}
                  for it in uniq]
        if result:
            _last_news["items"] = result
        return result if result else list(_last_news["items"])
    finally:
        ex.shutdown(wait=False)


# ----------------------------------------------------------------------
# แปลทั้งข่าว: ดึงเนื้อหาเต็มจากลิงก์ แล้วแปลไทยทีละย่อหน้า
# ----------------------------------------------------------------------
_article_cache = {}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch_article_th(url):
    if not url or not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "ลิงก์ไม่ถูกต้อง"}
    if url in _article_cache:
        return _article_cache[url]
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for bad in soup(["script", "style", "nav", "footer", "aside", "form"]):
            bad.decompose()
        # หา <p> ในส่วน article ก่อน ถ้าไม่มีค่อยกวาดทั้งหน้า
        scope = soup.find("article") or soup
        paras = [clean(p.get_text()) for p in scope.find_all("p")]
        paras = [p for p in paras if len(p) >= 60][:22]
        if len(paras) < 3:
            # ชั้นสำรอง: หลายเว็บฝังเนื้อเต็มไว้ใน JSON-LD (articleBody)
            import json as _json
            for sc in soup.find_all("script", type="application/ld+json"):
                try:
                    d = _json.loads(sc.string or "")
                except Exception:
                    continue
                for doc in (d if isinstance(d, list) else [d]):
                    body = doc.get("articleBody") if isinstance(doc, dict) else None
                    if body and len(body) > 200:
                        text = clean(body)
                        # แบ่งเป็นย่อหน้าละ ~2 ประโยค
                        sents = re.split(r"(?<=[.!?])\s+", text)
                        paras = [" ".join(sents[i:i + 2]) for i in range(0, min(len(sents), 44), 2)]
                        break
                if len(paras) >= 3:
                    break
        if not paras:
            result = {"ok": False,
                      "error": "ดึงเนื้อหาไม่ได้ (เว็บอาจล็อกไว้/มี paywall) — ใช้ปุ่ม Google Translate แทน"}
            _article_cache[url] = result
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            th = list(ex.map(translate_th, paras))
        out_paras = [t if t else p for t, p in zip(th, paras)]
        result = {"ok": True, "paragraphs": out_paras}
        _article_cache[url] = result
        return result
    except Exception as e:
        return {"ok": False, "error": f"โหลดหน้าเว็บไม่สำเร็จ: {e}"}


# ----------------------------------------------------------------------
# ข่าววงใน: ผู้บริหารซื้อ/ขายหุ้น (SEC Form 4 ผ่าน OpenInsider)
# ----------------------------------------------------------------------
_insider_cache = {}  # ticker -> (ts, rows)

_POSITION_TH = {
    "chief executive officer": "ซีอีโอ", "ceo": "ซีอีโอ",
    "chief financial officer": "ซีเอฟโอ (การเงิน)", "cfo": "ซีเอฟโอ (การเงิน)",
    "chief operating officer": "ซีโอโอ (ปฏิบัติการ)", "coo": "ซีโอโอ (ปฏิบัติการ)",
    "chief technology officer": "ซีทีโอ (เทคโนโลยี)", "cto": "ซีทีโอ (เทคโนโลยี)",
    "chief accounting officer": "ผู้บริหารสูงสุดฝ่ายบัญชี", "cao": "ผู้บริหารสูงสุดฝ่ายบัญชี",
    "chief people officer": "ผู้บริหารสูงสุดฝ่ายบุคคล",
    "10%": "ผู้ถือหุ้นใหญ่ (>10%)", "chairman": "ประธานกรรมการ", "cob": "ประธานกรรมการ",
    "president": "ประธานบริษัท", "pres": "ประธานบริษัท",
    "director": "กรรมการบริษัท", "dir": "กรรมการบริษัท",
    "general counsel": "ที่ปรึกษากฎหมาย", "gc": "ที่ปรึกษากฎหมาย",
    "evp": "รองประธานบริหารอาวุโส", "svp": "รองประธานอาวุโส", "vp": "รองประธาน",
    "officer": "ผู้บริหาร",
}


def _position_th(pos):
    p = (pos or "").lower()
    matched = []
    for k, v in _POSITION_TH.items():
        if k in p and v not in matched:
            matched.append(v)
    return " / ".join(matched[:2]) if matched else (pos or "-")


def _classify_txn(trade_type):
    """แปลงรหัส Trade Type ของ SEC Form 4 เช่น 'S - Sale', 'P - Purchase'"""
    t = (trade_type or "").strip().lower()
    if t.startswith("p"):
        return "ซื้อ", "buy"
    if t.startswith(("s", "d")):
        return "ขาย", "sell"
    if t.startswith("f"):
        return "ขายจ่ายภาษี", "sell"
    if t.startswith("g"):
        return "โอนให้/บริจาค", "other"
    if t.startswith(("m", "x", "c")):
        return "ใช้สิทธิ์ออปชัน", "other"
    return "อื่นๆ", "other"


def _num(s):
    """'-40,000' / '-$19,562' → 40000.0 (ค่าสัมบูรณ์)"""
    try:
        return abs(float(re.sub(r"[^0-9.\-]", "", s or "")))
    except Exception:
        return None


def get_insider(tickers, force=False):
    """ดึงรายการซื้อขายของผู้บริหารจาก OpenInsider (ข้อมูล SEC Form 4) — แคชรายหุ้น 30 นาที"""
    from bs4 import BeautifulSoup
    now = time.time()
    tickers = [t.upper() for t in tickers][:15]

    def one(t):
        rows = []
        try:
            base = t.split(".")[0]  # หุ้นไทย .BK ไม่มีใน SEC — ใช้เฉพาะหุ้นสหรัฐ
            r = requests.get(f"http://openinsider.com/search?q={base}",
                             headers={"User-Agent": UA}, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.find("table", class_="tinytable")
            if not table:
                return rows
            for tr in table.find_all("tr")[1:11]:
                c = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(c) < 12 or c[3].upper() != base.upper():
                    continue
                action_th, action = _classify_txn(c[6])
                rows.append({
                    "ticker": t,
                    "insider": c[4].title(),
                    "position": _position_th(c[5]),
                    "action": action, "action_th": action_th,
                    "shares": _num(c[8]),
                    "price": _num(c[7]),
                    "value": _num(c[11]),
                    "date": c[2],
                })
        except Exception:
            pass
        return rows

    need = [t for t in tickers
            if force or t not in _insider_cache or now - _insider_cache[t][0] >= 1800]
    if need:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for t, rows in zip(need, ex.map(one, need)):
                _insider_cache[t] = (now, rows)
    if len(_insider_cache) > 200:
        for k in sorted(_insider_cache, key=lambda k: _insider_cache[k][0])[:100]:
            _insider_cache.pop(k, None)

    items = []
    for t in tickers:
        if t in _insider_cache:
            items.extend(_insider_cache[t][1])
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:60]


# ----------------------------------------------------------------------
# อินไซต์ในองค์กร: สัดส่วนถือหุ้นของผู้บริหาร/สถาบัน + ยอดซื้อขายสุทธิของผู้บริหาร
# ----------------------------------------------------------------------
_own_cache = {}  # ticker -> (ts, {insiders, institutions})


def _ownership(t):
    import yfinance as yf
    try:
        info = yf.Ticker(t).info or {}
        return {"insiders": info.get("heldPercentInsiders"),
                "institutions": info.get("heldPercentInstitutions")}
    except Exception:
        return {"insiders": None, "institutions": None}


def get_insider_overview(tickers, items):
    """สรุปภาพในองค์กรต่อบริษัท: % ถือหุ้นโดยคนใน/สถาบัน + ซื้อ-ขายสุทธิของผู้บริหารช่วงล่าสุด"""
    now = time.time()
    tickers = [t.upper() for t in tickers][:15]
    need = [t for t in tickers if t not in _own_cache or now - _own_cache[t][0] >= 43200]
    if need:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        try:
            futs = {ex.submit(_ownership, t): t for t in need}
            done, _ = concurrent.futures.wait(futs, timeout=20)
            for f in done:
                try:
                    _own_cache[futs[f]] = (now, f.result())
                except Exception:
                    pass
        finally:
            ex.shutdown(wait=False)

    out = []
    for t in tickers:
        rows = [i for i in items if i["ticker"] == t]
        own = _own_cache.get(t, (0, {}))[1]
        if not rows and own.get("insiders") is None:
            continue
        buys = sum(i.get("value") or 0 for i in rows if i["action"] == "buy")
        sells = sum(i.get("value") or 0 for i in rows if i["action"] == "sell")
        buy_n = sum(1 for i in rows if i["action"] == "buy")
        sell_n = sum(1 for i in rows if i["action"] == "sell")
        net = buys - sells
        if buy_n and not sell_n:
            note, tone = "ผู้บริหารซื้ออย่างเดียว — สัญญาณเชื่อมั่นแรง", "buy"
        elif net > 0:
            note, tone = "ซื้อสุทธิ — เชิงบวก คนในกำลังสะสม", "buy"
        elif sell_n and not buy_n:
            note, tone = "ช่วงนี้มีแต่รายการขาย — ควรติดตามใกล้ชิด", "sell"
        elif net < 0:
            note, tone = "ขายสุทธิ — ไม่ได้แย่เสมอไป แต่ควรดูประกอบ", "sell"
        else:
            note, tone = "ยังไม่มีรายการซื้อขายของผู้บริหารช่วงล่าสุด", "none"
        out.append({"ticker": t, "buys": buys, "sells": sells,
                    "buy_n": buy_n, "sell_n": sell_n, "net": net,
                    "insiders": own.get("insiders"),
                    "institutions": own.get("institutions"),
                    "note": note, "tone": tone})
    return out


# ----------------------------------------------------------------------
# เงินสถาบัน/กองทุน: ใครเพิ่มพอร์ต ใครลดพอร์ต (จากรายงาน 13F รายไตรมาส)
# ----------------------------------------------------------------------
_flow_cache = {}  # ticker -> (ts, payload) — ข้อมูลรายไตรมาส แคชได้ยาว (12 ชม.)
_FLOW_TTL = 43200


def _fnum(v):
    """float ที่กัน None/NaN/ค่าว่าง (NaN != NaN — ไม่ต้อง import pandas)"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _holder_rows(df, kind):
    """แปลงตาราง holders ของ yfinance เป็น list ธรรมดา (คอลัมน์: Date Reported, Holder,
    pctHeld, Shares, Value, pctChange — pctChange บวก = เพิ่มพอร์ต, ลบ = ลดพอร์ต)"""
    rows = []
    if df is None or getattr(df, "empty", True):
        return rows
    for _, r in df.iterrows():
        try:
            name = str(r.get("Holder") or "").strip()
            if not name:
                continue
            chg = _fnum(r.get("pctChange"))
            held = _fnum(r.get("pctHeld"))
            val = _fnum(r.get("Value"))
            d = r.get("Date Reported")
            rows.append({"holder": name, "kind": kind, "pct_held": held,
                         "pct_change": chg, "value": val,
                         "date": str(d)[:10] if d is not None else None})
        except Exception:
            continue
    return rows


def _flows_one(t):
    import yfinance as yf
    tk = yf.Ticker(t)
    rows = []
    for attr, kind in (("institutional_holders", "inst"), ("mutualfund_holders", "fund")):
        try:
            rows += _holder_rows(getattr(tk, attr, None), kind)
        except Exception:
            pass
    inst_count = inst_pct = None
    try:
        mh = tk.major_holders
        if mh is not None and not mh.empty and "Value" in mh.columns:
            g = lambda k: (_fnum(mh.loc[k, "Value"]) if k in mh.index else None)
            inst_count, inst_pct = g("institutionsCount"), g("institutionsPercentHeld")
    except Exception:
        pass
    return {"rows": rows, "inst_count": inst_count, "inst_pct": inst_pct}


def get_flows(tickers, force=False):
    """สรุปว่าสถาบัน/กองทุนกำลังเพิ่มหรือลดพอร์ตในแต่ละหุ้น

    หมายเหตุสำคัญ: ข้อมูลนี้มาจากรายงาน 13F ที่ยื่นเป็น "รายไตรมาส" (ดูฟิลด์ as_of)
    ไม่ใช่การซื้อขายแบบเรียลไทม์ของวันนี้
    """
    now = time.time()
    tickers = [t.upper() for t in tickers][:15]
    need = [t for t in tickers
            if force or t not in _flow_cache or now - _flow_cache[t][0] >= _FLOW_TTL]
    if need:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        try:
            futs = {ex.submit(_flows_one, t): t for t in need}
            done, _ = concurrent.futures.wait(futs, timeout=25)  # เส้นตาย กันหน้าค้าง
            for f in done:
                try:
                    _flow_cache[futs[f]] = (now, f.result())
                except Exception:
                    pass
        finally:
            ex.shutdown(wait=False)

    out = []
    for t in tickers:
        c = _flow_cache.get(t)
        if not c:
            continue
        data = c[1]
        rows = data["rows"]
        if not rows and data.get("inst_pct") is None:
            continue
        up = [r for r in rows if (r["pct_change"] or 0) > 0]
        down = [r for r in rows if (r["pct_change"] or 0) < 0]
        # ถ่วงน้ำหนักด้วยขนาดพอร์ต: รายใหญ่ขยับ 1% สำคัญกว่ารายเล็กขยับ 50%
        weighted = sum((r["pct_change"] or 0) * (r["pct_held"] or 0) for r in rows)
        if not rows:
            note, tone = "ไม่มีรายละเอียดผู้ถือหุ้นสถาบันสำหรับหุ้นตัวนี้", "none"
        elif weighted > 0 and len(up) > len(down):
            note, tone = "เงินสถาบันไหลเข้า — ส่วนใหญ่เพิ่มพอร์ต", "buy"
        elif weighted < 0 and len(down) > len(up):
            note, tone = "เงินสถาบันไหลออก — ส่วนใหญ่ลดพอร์ต", "sell"
        elif weighted > 0:
            note, tone = "ไหลเข้าเล็กน้อย — รายใหญ่เพิ่มพอร์ตมากกว่าที่ลด", "buy"
        elif weighted < 0:
            note, tone = "ไหลออกเล็กน้อย — รายใหญ่ลดพอร์ตมากกว่าที่เพิ่ม", "sell"
        else:
            note, tone = "ทรงตัว — สถาบันไม่ได้ขยับพอร์ตอย่างมีนัย", "none"
        top = sorted(rows, key=lambda r: r["pct_held"] or 0, reverse=True)[:5]
        # แต่ละรายยื่นรายงานคนละรอบ (13F รายไตรมาส แต่ ETF บางกองยื่นรายเดือน)
        # → ใช้ "วันที่ที่พบมากที่สุด" เป็นรอบอ้างอิงของการ์ด ไม่ใช่ max()
        #   เพราะ max() จะไปหยิบรอบของ ETF ไม่กี่กองมากำกับทั้งใบ ทำให้ดูสดเกินจริง
        dates = [r["date"] for r in rows if r["date"]]
        as_of = Counter(dates).most_common(1)[0][0] if dates else None
        out.append({"ticker": t, "up_n": len(up), "down_n": len(down),
                    "inst_count": data.get("inst_count"),
                    "inst_pct": data.get("inst_pct"),
                    "top": top, "as_of": as_of,
                    "note": note, "tone": tone})
    return out


# ----------------------------------------------------------------------
# สัญญาณเงินใหญ่ (Smart Money): รวมสัญญาณสถาบัน/กองทุน (13F) + ผู้บริหาร (Form 4)
# ----------------------------------------------------------------------
def smart_money_signals(tickers, force=False):
    """ตรวจจับ 'สัญญาณ' ที่มีนัยจริงเท่านั้น — ไม่ใช่รายงานความเคลื่อนไหวทุกรายการ

    ความสดของข้อมูลต่างกันมาก จึงกำกับไว้ทุกสัญญาณ:
      - สถาบัน/กองทุน = รายงาน 13F "รายไตรมาส" (ล่าช้าได้ถึง ~45 วัน)
      - ผู้บริหาร (Form 4) = ยื่นภายใน 2 วันทำการ → สดกว่ามาก
    """
    tickers = [t.upper() for t in tickers][:15]
    flows = {f["ticker"]: f for f in get_flows(tickers, force=force)}
    items = get_insider(tickers, force=force)
    ins = {s["ticker"]: s for s in get_insider_overview(tickers, items)}

    BIG_SINGLE = 20_000_000   # ขายก้อนเดียวเกิน $20M = ผิดปกติพอที่จะรู้
    BIG_TOTAL = 50_000_000    # ขายรวมเกิน $50M ในชุดล่าสุด

    out = []
    for t in tickers:
        alerts = []

        # --- สถาบัน/กองทุน (13F รายไตรมาส) ---
        f = flows.get(t)
        if f and (f.get("up_n") or f.get("down_n")):
            up, dn, as_of = f["up_n"], f["down_n"], f.get("as_of")
            if up >= 6 and up >= dn * 2:
                alerts.append({"kind": "inst", "dir": "buy", "level": "strong",
                               "text": f"สถาบันแห่เข้า — เพิ่มพอร์ต {up} ราย เทียบลดแค่ {dn} ราย",
                               "as_of": as_of})
            elif dn >= 6 and dn >= up * 2:
                alerts.append({"kind": "inst", "dir": "sell", "level": "strong",
                               "text": f"สถาบันแห่ออก — ลดพอร์ต {dn} ราย เทียบเพิ่มแค่ {up} ราย",
                               "as_of": as_of})
            # รายใหญ่ 5 อันดับแรกที่ขยับพอร์ตแรง (>=10%) — รายใหญ่ขยับ = มีนัย
            for m in (f.get("top") or []):
                pc = m.get("pct_change")
                if pc is None or abs(pc) < 0.10:
                    continue
                # ตัด artifact: ค่า ±1.0 เป๊ะ โผล่ซ้ำทุกหุ้นกับผู้ถือรายเดียวกัน (เช่น Vanguard
                # แสดง 1.0 ทั้ง NVDA/MU/PLTR/RKLB พร้อมกัน) = การรายงาน/เปิดสถานะใหม่
                # ไม่ใช่การซื้อเพิ่มเท่าตัวจริง — ถ้าปล่อยไว้จะเตือนหลอกทุกหุ้น
                if abs(abs(pc) - 1.0) < 1e-9:
                    continue
                is_fund = m.get("kind") == "fund"
                d = "buy" if pc > 0 else "sell"
                verb = "เพิ่มพอร์ต" if d == "buy" else "ลดพอร์ต"
                who = "กองทุน" if is_fund else "สถาบัน"
                name = (m.get("holder") or "")[:32]
                alerts.append({"kind": "fund" if is_fund else "inst", "dir": d, "level": "normal",
                               "text": f"{who}รายใหญ่ {name} {verb} {abs(pc)*100:.0f}% (ถือ {(m.get('pct_held') or 0)*100:.1f}%)",
                               "as_of": m.get("date") or as_of})
                if len([a for a in alerts if a["level"] == "normal"]) >= 2:
                    break

        # --- ผู้บริหาร (Form 4 — สดกว่า) ---
        s = ins.get(t)
        if s:
            if s["buy_n"] >= 2:
                alerts.append({"kind": "insider", "dir": "buy", "level": "strong",
                               "text": f"ผู้บริหารซื้อพร้อมกัน {s['buy_n']} ราย — สัญญาณเชื่อมั่นที่แรงที่สุดของกลุ่มคนใน",
                               "as_of": None})
            elif s["buy_n"] == 1:
                alerts.append({"kind": "insider", "dir": "buy", "level": "normal",
                               "text": "ผู้บริหารซื้อหุ้นตัวเอง — คนในไม่ค่อยซื้อถ้าไม่เห็นอะไร",
                               "as_of": None})
            # การขายของผู้บริหารส่วนใหญ่เป็นกิจวัตร (ภาษี/กระจายความเสี่ยง/แผน 10b5-1)
            # นับ "จำนวนรายการ" จึงไม่ใช่สัญญาณ (ดึงมาแค่ 10 แถว หุ้นใหญ่ก็เต็มทุกตัวอยู่แล้ว)
            # → เตือนเฉพาะเมื่อ "ขนาด" ผิดปกติจริง และไม่นับรายการขายจ่ายภาษีซึ่งเป็นระบบอัตโนมัติ
            rows = [i for i in items
                    if i["ticker"] == t and i["action"] == "sell"
                    and i["action_th"] != "ขายจ่ายภาษี" and (i.get("value") or 0) > 0]
            if rows:
                top_sale = max(rows, key=lambda i: i["value"])
                total = sum(i["value"] for i in rows)
                if top_sale["value"] >= BIG_SINGLE or total >= BIG_TOTAL:
                    m = top_sale["value"] / 1e6
                    alerts.append({
                        "kind": "insider", "dir": "sell", "level": "normal",
                        "text": (f"ผู้บริหารขายก้อนใหญ่ {top_sale['insider']} "
                                 f"({top_sale['position']}) {m:,.0f} ล้านดอลลาร์ "
                                 f"เมื่อ {top_sale['date']} · รวมชุดล่าสุด {total/1e6:,.0f} ล้าน "
                                 f"— ดูประกอบ ไม่ใช่สัญญาณขายเสมอไป"),
                        "as_of": None})

        if alerts:
            b = sum(1 for a in alerts if a["dir"] == "buy")
            sl = sum(1 for a in alerts if a["dir"] == "sell")
            out.append({"ticker": t, "alerts": alerts,
                        "tone": "buy" if b > sl else "sell" if sl > b else "mixed",
                        "strong": any(a["level"] == "strong" for a in alerts)})
    return out


# ----------------------------------------------------------------------
# ปฏิทินตลาด: กำหนดประชุม Fed (FOMC) + วันประกาศงบไตรมาสของหุ้นใน watchlist
# ----------------------------------------------------------------------
# กำหนดการทางการจาก federalreserve.gov/monetarypolicy/fomccalendars.htm
# (start, end, มี Dot Plot/คาดการณ์เศรษฐกิจ)
FOMC_MEETINGS = [
    ("2026-01-27", "2026-01-28", False),
    ("2026-03-17", "2026-03-18", True),
    ("2026-04-28", "2026-04-29", False),
    ("2026-06-16", "2026-06-17", True),
    ("2026-07-28", "2026-07-29", False),
    ("2026-09-15", "2026-09-16", True),
    ("2026-10-27", "2026-10-28", False),
    ("2026-12-08", "2026-12-09", True),
    ("2027-01-26", "2027-01-27", False),
    ("2027-03-16", "2027-03-17", True),
    ("2027-04-27", "2027-04-28", False),
    ("2027-06-08", "2027-06-09", True),
]

# กำหนดประกาศเงินเฟ้อ CPI ปี 2026 (ตาราง BLS ทางการ, 8:30 เช้าเวลาสหรัฐ)
CPI_RELEASES_2026 = [
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]


def _nfp_dates(months_ahead=8):
    """ตัวเลขจ้างงาน (Nonfarm Payrolls): ศุกร์แรกของเดือน (เลื่อนถ้าตรงปีใหม่)"""
    from datetime import date, timedelta
    today = date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months_ahead):
        d = date(y, m, 1)
        while d.weekday() != 4:
            d += timedelta(days=1)
        if m == 1 and d.day == 1:  # ตรงวันปีใหม่ → เลื่อนสัปดาห์ถัดไป
            d += timedelta(days=7)
        if d >= today:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


_earn_cache = {}  # ticker -> (ts, "YYYY-MM-DD" or None)


def _next_earnings_date(t):
    """วันประกาศงบถัดไปจาก Yahoo Finance (เป็นกำหนดคาดการณ์)"""
    import yfinance as yf
    from datetime import date, datetime as dtm
    today = date.today()
    try:
        cal = yf.Ticker(t).calendar or {}
        eds = cal.get("Earnings Date") or []
        if not isinstance(eds, (list, tuple)):
            eds = [eds]
        future = []
        for d in eds:
            if isinstance(d, dtm):
                d = d.date()
            if isinstance(d, date) and d >= today:
                future.append(d)
        return str(min(future)) if future else None
    except Exception:
        return None


def _thai_time(date_str, hour_et, minute_et=0):
    """แปลงเวลานิวยอร์ก → เวลาไทย (pytz จัดการ DST ให้เอง — หน้าหนาว/หน้าร้อนต่างกัน 1 ชม.)
    คืน (เวลา 'HH:MM', ข้ามไปวันถัดไปไหม) หรือ (None, False) ถ้าแปลงไม่ได้"""
    try:
        import pytz
        from datetime import datetime as dtm
        ny = pytz.timezone("America/New_York")
        d = dtm.strptime(date_str, "%Y-%m-%d")
        t = ny.localize(d.replace(hour=hour_et, minute=minute_et)) \
              .astimezone(pytz.timezone("Asia/Bangkok"))
        return t.strftime("%H:%M"), t.date() > d.date()
    except Exception:
        return None, False


def get_calendar(tickers, force=False):
    from datetime import date, datetime as dtm
    today = date.today()
    tickers = [t.upper() for t in tickers][:15]
    events = []

    # 1) ประชุม Fed
    for start, end, proj in FOMC_MEETINGS:
        end_d = dtm.strptime(end, "%Y-%m-%d").date()
        if end_d < today:
            continue
        start_d = dtm.strptime(start, "%Y-%m-%d").date()
        title = "ประชุม Fed (FOMC) — แถลงผลดอกเบี้ยวันที่สอง"
        if proj:
            title += " พร้อม Dot Plot คาดการณ์เศรษฐกิจ"
        # Fed แถลงผล 14:00 น. NY ของวันที่สอง → เช้ามืดวันถัดไปตามเวลาไทย
        tt, nextday = _thai_time(end, 14, 0)
        time_th = (f"แถลงผล {tt} น. ไทย" + (" (เช้ามืดคืนวันประชุม)" if nextday else "")) if tt else None
        events.append({"date": start, "date_end": end, "type": "fed",
                       "ticker": None, "title": title, "time_th": time_th,
                       "days": (start_d - today).days})

    # 2) ดัชนีเศรษฐกิจสหรัฐที่กระทบตลาด
    # ดัชนีเศรษฐกิจประกาศ 8:30 น. NY → คำนวณเวลาไทยจริงของแต่ละวัน (DST ทำให้ต่างกันได้ 1 ชม.)
    for d in _nfp_dates():
        tt, _ = _thai_time(str(d), 8, 30)
        events.append({"date": str(d), "date_end": None, "type": "econ", "subtype": "nfp",
                       "ticker": None,
                       "title": "ตัวเลขจ้างงานสหรัฐ (Nonfarm Payrolls)",
                       "time_th": f"ประกาศ {tt} น. ไทย" if tt else None,
                       "days": (d - today).days})
    for ds in CPI_RELEASES_2026:
        d = dtm.strptime(ds, "%Y-%m-%d").date()
        if d >= today:
            tt, _ = _thai_time(ds, 8, 30)
            events.append({"date": ds, "date_end": None, "type": "econ", "subtype": "cpi",
                           "ticker": None,
                           "title": "เงินเฟ้อสหรัฐ CPI",
                           "time_th": f"ประกาศ {tt} น. ไทย" if tt else None,
                           "days": (d - today).days})

    # 3) งบไตรมาสของหุ้นใน watchlist (แคชรายตัว 12 ชม.)
    now = time.time()
    need = [t for t in tickers
            if force or t not in _earn_cache or now - _earn_cache[t][0] >= 43200]
    if need:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for t, d in zip(need, ex.map(_next_earnings_date, need)):
                _earn_cache[t] = (now, d)
    for t in tickers:
        d = _earn_cache.get(t, (0, None))[1]
        if d:
            d_date = dtm.strptime(d, "%Y-%m-%d").date()
            # งบไตรมาสไม่มีเวลาแน่นอนจาก Yahoo — บอกกรอบเวลาไทยของสองช่วงมาตรฐาน
            bmo, _ = _thai_time(d, 8, 0)    # ก่อนตลาดเปิด (BMO ~8:00 NY)
            amc, _ = _thai_time(d, 16, 30)  # หลังตลาดปิด (AMC ~16:30 NY) → เช้ามืดไทยวันถัดไป
            time_th = (f"ก่อนเปิดตลาด ~{bmo} น. หรือหลังปิด ~{amc} น. ไทย (เช้ามืดวันถัดไป)"
                       if bmo and amc else None)
            events.append({"date": d, "date_end": None, "type": "earnings",
                           "ticker": t,
                           "title": f"ประกาศงบไตรมาส {t} (กำหนดคาดการณ์จาก Yahoo)",
                           "time_th": time_th,
                           "days": (d_date - today).days})

    events.sort(key=lambda e: e["date"])
    return events
