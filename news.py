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

_cache = {"ts": 0, "items": []}
CACHE_SECONDS = 300  # เก็บผล 5 นาที


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def translate_th(text):
    """แปลอังกฤษ→ไทย ผ่าน Google Translate (ไม่ต้องใช้ API key)"""
    if not text:
        return ""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "th", "dt": "t", "q": text[:1500]},
            timeout=12,
        )
        segs = r.json()[0] or []
        return "".join(s[0] for s in segs if s and s[0]).strip()
    except Exception:
        return ""


def fetch_feed(name, url):
    try:
        d = feedparser.parse(url)
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


def fetch_yahoo_watchlist(tickers):
    """ข่าวรายตัวของหุ้นใน watchlist จาก Yahoo Finance"""
    import yfinance as yf
    out = []
    for t in tickers[:12]:
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
            continue
    return out


def get_news(tickers, max_items=30, force=False):
    now = time.time()
    if not force and _cache["items"] and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["items"]

    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_feed, n, u) for n, u in FEEDS]
        futs.append(ex.submit(fetch_yahoo_watchlist, tickers))
        for f in futs:
            items.extend(f.result())

    # เรียงใหม่สุดก่อน + ตัดข่าวซ้ำ (หัวข้อเหมือนกัน)
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        key = it["title"].lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    uniq = uniq[:max_items]

    # แปลไทยแบบขนาน (เร็ว) — แปลไม่ได้ก็แสดงต้นฉบับ
    def tr(it):
        it["title_th"] = translate_th(it["title"]) or it["title"]
        it["summary_th"] = translate_th(it["summary"]) if it["summary"] else ""
        return it

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        uniq = list(ex.map(tr, uniq))

    _cache["ts"] = now
    _cache["items"] = uniq
    return uniq


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
_insider_cache = {"ts": 0, "items": []}

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
    """ดึงรายการซื้อขายของผู้บริหารจาก OpenInsider (ข้อมูล SEC Form 4)"""
    from bs4 import BeautifulSoup
    now = time.time()
    if not force and _insider_cache["items"] and now - _insider_cache["ts"] < 1800:
        return _insider_cache["items"]

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

    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for rows in ex.map(one, tickers):
            items.extend(rows)
    items.sort(key=lambda x: x["date"], reverse=True)
    items = items[:60]
    _insider_cache["ts"] = now
    _insider_cache["items"] = items
    return items
