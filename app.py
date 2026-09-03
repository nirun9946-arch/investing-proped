# -*- coding: utf-8 -*-
"""
Investing Pro — Web Dashboard
รัน: python app.py  แล้วเปิด http://127.0.0.1:8750
"""

import json
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from flask import Flask, jsonify, request, send_from_directory

import hashlib
import re
import secrets
import threading
import time

import ai as ai_mod
import fundamentals as fund_mod
import investing_pro as core
import news as news_mod
import smartflow as flow_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))


# ----------------------------------------------------------------------
# กัน NaN/Infinity หลุดไปกับ JSON — Python เขียน NaN ได้แต่ JavaScript อ่านไม่ได้
# ถ้าหลุดไปแม้ตัวเดียว เบราว์เซอร์จะ parse ทั้งก้อนไม่ผ่าน = การ์ดขึ้น "โหลดไม่สำเร็จ" ยกแผง
# (curl/Python ทดสอบแล้วผ่าน เพราะยอมรับ NaN — บั๊กแบบนี้จึงหลุดง่าย จึงกันไว้ที่ชั้นนี้)
# ----------------------------------------------------------------------
def _json_safe(o):
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


try:
    from flask.json.provider import DefaultJSONProvider

    class _SafeJSON(DefaultJSONProvider):
        def dumps(self, obj, **kw):
            return super().dumps(_json_safe(obj), **kw)

    app.json = _SafeJSON(app)
except Exception:      # Flask รุ่นเก่าไม่มี provider API — ข้ามไป ยังมีการกันที่ต้นทางอยู่
    pass

# ----------------------------------------------------------------------
# ระบบบัญชีผู้ใช้: watchlist/พอร์ตส่วนตัว ป้องกันด้วยรหัสผ่าน (PBKDF2 hash)
# ----------------------------------------------------------------------
USERS_PATH = os.path.join(BASE_DIR, "users.json")
_users_lock = threading.Lock()
_tokens = {}  # token -> username (in-memory session)


def _load_users():
    if os.path.exists(USERS_PATH):
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_users(users):
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False)


def _hash_pw(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), 100_000).hex()


def _auth_user():
    token = request.headers.get("X-Auth", "")
    return _tokens.get(token)


def _clean_port(p, default_name="พอร์ต 1"):
    """กรองข้อมูลพอร์ตเดียว (watchlist + มูลค่าพอร์ต + สัดส่วน)"""
    p = p or {}
    out = {"name": str(p.get("name") or default_name)[:30],
           "watchlist": [], "port_total": 0, "positions": {}}
    for t in (p.get("watchlist") or [])[:30]:
        s = str(t).strip().upper()[:15]
        if s and re.match(r"^[A-Z0-9.\-]+$", s):
            out["watchlist"].append(s)
    try:
        out["port_total"] = max(0.0, float(p.get("port_total") or 0))
    except (TypeError, ValueError):
        pass
    for k, v in list((p.get("positions") or {}).items())[:60]:
        try:
            out["positions"][str(k).strip().upper()[:15]] = {
                "hold": max(0.0, float(v.get("hold", 0) or 0)),
                "target": max(0.0, min(100.0, float(v.get("target", 10) or 10))),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _clean_user_data(d):
    """รองรับหลายพอร์ต {ports:[...], active:n} และโครงสร้างเก่าแบบพอร์ตเดียว"""
    d = d or {}
    if d.get("ports"):
        ports = [_clean_port(p, f"พอร์ต {i+1}") for i, p in enumerate(d["ports"][:5])]
        try:
            active = max(0, min(len(ports) - 1, int(d.get("active") or 0)))
        except (TypeError, ValueError):
            active = 0
        return {"ports": ports, "active": active}
    # โครงสร้างเก่า: พอร์ตเดียว → ห่อเป็น ports
    return {"ports": [_clean_port(d)], "active": 0}


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    body = request.json or {}
    user = str(body.get("user", "")).strip().lower()
    pw = str(body.get("pass", ""))
    if not re.match(r"^[a-z0-9_]{3,20}$", user):
        return jsonify({"error": "ชื่อผู้ใช้ต้องเป็น a-z, 0-9, _ ยาว 3-20 ตัว"}), 400
    if len(pw) < 4:
        return jsonify({"error": "รหัสผ่านอย่างน้อย 4 ตัวอักษร"}), 400
    with _users_lock:
        users = _load_users()
        if user in users:
            return jsonify({"error": f"ชื่อ '{user}' ถูกใช้แล้ว — ลองชื่ออื่น หรือเข้าสู่ระบบ"}), 409
        salt = secrets.token_hex(16)
        users[user] = {"salt": salt, "hash": _hash_pw(pw, salt),
                       "data": {"ports": [], "active": 0}}
        _save_users(users)
    token = secrets.token_hex(24)
    _tokens[token] = user
    return jsonify({"ok": True, "token": token, "user": user, "data": users[user]["data"]})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.json or {}
    user = str(body.get("user", "")).strip().lower()
    pw = str(body.get("pass", ""))
    users = _load_users()
    rec = users.get(user)
    if not rec or _hash_pw(pw, rec["salt"]) != rec["hash"]:
        return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401
    token = secrets.token_hex(24)
    _tokens[token] = user
    return jsonify({"ok": True, "token": token, "user": user, "data": rec.get("data", {})})


@app.route("/api/auth/me")
def auth_me():
    user = _auth_user()
    if not user:
        return jsonify({"error": "เซสชันหมดอายุ"}), 401
    rec = _load_users().get(user)
    if not rec:
        return jsonify({"error": "ไม่พบบัญชี"}), 401
    return jsonify({"ok": True, "user": user, "data": rec.get("data", {})})


@app.route("/api/auth/sync", methods=["POST"])
def auth_sync():
    user = _auth_user()
    if not user:
        return jsonify({"error": "เซสชันหมดอายุ"}), 401
    data = _clean_user_data((request.json or {}).get("data"))
    with _users_lock:
        users = _load_users()
        if user not in users:
            return jsonify({"error": "ไม่พบบัญชี"}), 401
        users[user]["data"] = data
        _save_users(users)
    return jsonify({"ok": True})


def read_config():
    return core.load_config()


def write_config(cfg):
    with open(core.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@app.route("/")
def index():
    """หน้าเว็บหลัก

    ตั้ง no-cache เพราะเว็บทั้งเว็บอยู่ในไฟล์นี้ไฟล์เดียว (ทั้ง HTML/CSS/JS)
    เบราว์เซอร์ที่เก็บไว้จะไม่เห็นของใหม่จนกว่าจะกดรีเฟรชแบบล้างแคช —
    เจอจริง 3 ก.ย. 69: อัปแก้แถบดัชนีขึ้นไปแล้ว แต่หน้าที่เปิดอยู่ยังเป็นของเก่า
    no-cache ไม่ได้แปลว่าโหลดใหม่ทุกครั้ง แต่คือ "ถามเซิร์ฟเวอร์ก่อนเสมอ"
    ถ้าไฟล์ไม่เปลี่ยน ETag จะตอบ 304 ไม่ต้องโหลดซ้ำอยู่ดี
    """
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def _tickers_param():
    """watchlist ของผู้ใช้แต่ละคน ส่งมาทาง ?tickers=A,B,C — ถ้าไม่ส่งใช้ค่าเริ่มต้นจาก config"""
    q = request.args.get("tickers", "").strip()
    if q:
        return [t.strip().upper() for t in q.split(",") if t.strip()][:15]
    return read_config()["watchlist"]


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    """รายการเริ่มต้นสำหรับผู้ใช้ใหม่ (แต่ละคนเก็บของตัวเองในเบราว์เซอร์)"""
    return jsonify(read_config()["watchlist"])


@app.route("/api/validate/<ticker>")
def validate_ticker(ticker):
    """ตรวจว่าชื่อหุ้นดึงข้อมูลได้จริง ก่อนให้ผู้ใช้เพิ่มเข้า watchlist ของตัวเอง"""
    ticker = ticker.strip().upper()
    try:
        core.fetch(ticker, period="3mo")
        return jsonify({"ok": True, "ticker": ticker})
    except Exception:
        return jsonify({"ok": False,
                        "error": f"ไม่พบข้อมูล {ticker} — ตรวจสอบชื่อ (หุ้นไทยใส่ .BK เช่น PTT.BK)"}), 404


@app.route("/api/news")
def api_news():
    force = request.args.get("refresh") == "1"
    import time
    try:
        items = news_mod.get_news(_tickers_param(), force=force)
        return jsonify({"updated": time.time(), "items": items})
    except Exception as e:
        # แผนสำรอง: ส่งข่าวชุดล่าสุดที่เคยสำเร็จ ดีกว่าหน้าว่าง
        stale = news_mod._last_news.get("items") or []
        if stale:
            return jsonify({"updated": time.time(), "items": stale, "stale": True})
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/article")
def api_article():
    url = request.args.get("url", "")
    return jsonify(news_mod.fetch_article_th(url))


@app.route("/api/insider")
def api_insider():
    force = request.args.get("refresh") == "1"
    try:
        tickers = _tickers_param()
        items = news_mod.get_insider(tickers, force=force)
        summary = news_mod.get_insider_overview(tickers, items)
        return jsonify({"items": items, "summary": summary})
    except Exception as e:
        return jsonify({"error": str(e), "items": [], "summary": []}), 500


@app.route("/api/flows")
def api_flows():
    """เงินสถาบัน/กองทุนเข้า-ออก (จากรายงาน 13F รายไตรมาส ไม่ใช่เรียลไทม์)"""
    force = request.args.get("refresh") == "1"
    try:
        return jsonify({"items": news_mod.get_flows(_tickers_param(), force=force)})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


# ===== ตัวอุ่นราคาเบื้องหลัง =====
# ปัญหาเดิม: คำขอราคาของผู้ใช้ต้องรอ Yahoo ตอบก่อนเสมอ → หน่วงเพิ่มทุกครั้งที่แคชหมดอายุ
# ตอนนี้มีเธรดคอยดึงราคาของตัวที่ "มีคนดูอยู่" ไว้ล่วงหน้า คำขอจริงจึงอ่านจากแคชได้ทันที
# (batch ยิงครั้งเดียวได้ทุกตัว จึงอุ่นทุก 2.5 วิได้โดยไม่กวน Yahoo มากกว่าเดิม)
_HOT = {}                  # ticker -> เวลาที่มีคนขอล่าสุด
_HOT_TTL = 120             # วิ — ไม่มีใครขอเกินนี้ ถือว่าเลิกดูแล้ว หยุดอุ่น
_WARM_EVERY = 2.0


def _mark_hot(tickers):
    now = time.time()
    for t in tickers:
        _HOT[t.upper()] = now


def _warm_loop():
    while True:
        try:
            now = time.time()
            hot = [t for t, ts in list(_HOT.items()) if now - ts < _HOT_TTL]
            for t, ts in list(_HOT.items()):
                if now - ts >= _HOT_TTL:
                    _HOT.pop(t, None)
            if hot:
                # ขอให้รีเฟรชถ้าเก่ากว่ารอบอุ่นนิดเดียว — ต้องสั้นกว่า QUOTE_TTL ของฝั่งคำขอ
                # แคชที่ผู้ใช้เห็นจึงใหม่กว่าเส้นหมดอายุเสมอ และไม่มีใครต้องรอ Yahoo
                core.live_quotes(hot, max_age=_WARM_EVERY * 0.8)
                time.sleep(_WARM_EVERY)
            else:
                time.sleep(5)      # ไม่มีคนดู → นอนยาว ไม่ยิง Yahoo เปล่าๆ
        except Exception:
            time.sleep(5)


threading.Thread(target=_warm_loop, daemon=True, name="quote-warmer").start()


@app.route("/api/quotes")
def api_quotes():
    try:
        ts = _tickers_param()
        _mark_hot(ts)
        return jsonify({"quotes": core.live_quotes(ts)})
    except Exception as e:
        return jsonify({"error": str(e), "quotes": []}), 500


# แถบตลาดโลก — สัญลักษณ์ที่ตรวจแล้วว่า Yahoo มีข้อมูลจริง
# (XAUUSD=X และ DX=F ใช้ไม่ได้ ถูกตัดออก)
#
# ช่องที่ 5 = สัญลักษณ์สำรอง ใช้เมื่อตัวหลักดึงไม่ได้
# ทำไมต้องมี: Yahoo ไม่ส่งดัชนีที่มีลิขสิทธิ์ (^GSPC, ^NDX) ให้ IP ของศูนย์ข้อมูล
# ตรวจจากเซิร์ฟเวอร์จริงบน Render 3 ก.ย. 69 — ^GSPC/^NDX ไม่มา แต่ ^DJI ^IXIC ^VIX
# ^TNX SPY QQQ มาปกติ ผลคือแถบดัชนีขึ้นแค่ 6 จาก 8 ช่อง (เพื่อนผู้ใช้เป็นคนทัก)
#   • ^SPX = ดัชนี S&P 500 ตัวเดียวกับ ^GSPC เป๊ะ (ทดสอบแล้วได้ 7666.6 +0.46% เท่ากัน)
#     แค่เป็นชื่อเรียกอีกแบบที่ Yahoo ยอมส่งให้ → สลับมาใช้ได้โดยเลขไม่เปลี่ยน
#   • Nasdaq 100 ไม่มีชื่อเรียกสำรองที่ใช้ได้ (NDX เฉยๆ เป็นคนละตัว, ^NDXT เป็นดัชนีย่อย)
#     จึงถอยไปใช้ QQQ ซึ่งเป็นกองทุนที่อ้างอิงดัชนีนี้ — % ตรงกันแทบสนิท (0.226 vs 0.227)
#     แต่ "ระดับราคา" คนละสเกล (709 vs 29,143) จึงต้องเปลี่ยนชื่อช่องให้ตรงตามของจริง
#
# ตรวจซ้ำวันเดียวกัน: ^SPX ที่เคยใช้ได้กลับหายไปด้วย และ ^DJI ^IXIC ^RUT ก็หาย
# สรุปคือ "ทุกอย่างที่ขึ้นต้นด้วย ^ ไม่แน่นอน" ยกเว้น ^TNX/^VIX ที่ยังมาตลอด
# ส่วนกองทุน ETF (SPY VOO IVV QQQ) มาครบทุกครั้ง → ปิดท้ายเชนด้วย ETF เสมอ
# จะได้ไม่มีช่องไหนหายอีก ไม่ว่า Yahoo จะตัดดัชนีตัวไหนเพิ่ม
MARKET_SYMBOLS = [
    ("GC=F", "ทองคำ", "🥇", "USD/ounce"),
    ("CL=F", "น้ำมัน WTI", "🛢", "USD/barrel"),
    ("BTC-USD", "บิตคอยน์", "₿", "USD"),
    ("THB=X", "ดอลลาร์/บาท", "💵", "บาทต่อ 1 ดอลลาร์"),
    # ดัชนีหลักและตัววัดความเสี่ยงของตลาด
    ("^GSPC", "S&P 500", "🇺🇸", "ดัชนีหุ้นใหญ่ 500 ตัวของสหรัฐ",
     [("^SPX", None, None),
      ("SPY", "S&P 500 · SPY", "ราคากองทุน SPY ที่อ้างอิงดัชนี S&P 500 "
                               "(% ขึ้นลงตรงกับดัชนี แต่ระดับราคาคนละสเกล)")]),
    ("^NDX", "Nasdaq 100", "💻", "ดัชนีหุ้นเทค 100 ตัว",
     [("QQQ", "Nasdaq 100 · QQQ", "ราคากองทุน QQQ ที่อ้างอิงดัชนี Nasdaq 100 "
                                  "(% ขึ้นลงตรงกับดัชนี แต่ระดับราคาคนละสเกล)")]),
    ("^TNX", "พันธบัตร 10 ปี", "🏦", "% ผลตอบแทน — ขึ้นมากมักกดดันหุ้นเติบโต"),
    ("^VIX", "VIX ความกลัว", "😨", "ยิ่งสูงยิ่งกลัว: <20 ปกติ · >30 ตลาดตื่นตระหนก"),
]


def _market_plan():
    """คลี่ MARKET_SYMBOLS เป็น (สัญลักษณ์หลัก, ชื่อ, ไอคอน, หน่วย, [ตัวสำรอง])"""
    for row in MARKET_SYMBOLS:
        sym, name, icon, unit = row[0], row[1], row[2], row[3]
        alts = row[4] if len(row) > 4 else []
        yield sym, name, icon, unit, alts


@app.route("/api/market")
def api_market():
    """ราคาเรียลไทม์ตลาดโลก: ทองคำ / น้ำมัน / บิตคอยน์ / ดัชนี / VIX"""
    try:
        plan = list(_market_plan())
        # ขอทั้งตัวหลักและตัวสำรองไปในคำขอเดียว จะได้ไม่ต้องยิงรอบสองเวลาตัวหลักหาย
        syms = []
        for sym, _n, _i, _u, alts in plan:
            syms.append(sym)
            syms.extend(a[0] for a in alts)
        _mark_hot(syms)
        quotes = {q["ticker"]: q for q in core.live_quotes(syms)}

        out = []
        for sym, name, icon, unit, alts in plan:
            q, used, via = quotes.get(sym), sym, None
            for alt_sym, alt_name, alt_note in alts:
                if q:
                    break
                q = quotes.get(alt_sym)
                if q:
                    used = alt_sym
                    if alt_name:
                        name = alt_name
                    if alt_note:
                        unit, via = alt_note, alt_note
            if not q:
                continue
            out.append({"symbol": used, "name": name, "icon": icon, "unit": unit,
                        "price": q["price"], "chg": q.get("chg"), "ts": q.get("ts"),
                        "delay": q.get("delay"),
                        # via มีค่าเมื่อช่องนี้ไม่ได้ใช้แหล่งหลัก — ฝั่งเว็บเอาไปบอกผู้ใช้
                        "via": via})
        return jsonify({"items": out})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


_MARKET_META = {}
for _sym, _name, _icon, _unit, _alts in _market_plan():
    _MARKET_META[_sym] = (_name, _icon, _unit)
    for _a_sym, _a_name, _a_note in _alts:
        _MARKET_META[_a_sym] = (_a_name or _name, _icon, _a_note or _unit)


@app.route("/api/market-ta/<path:symbol>")
def api_market_ta(symbol):
    """วิเคราะห์เทคนิคของสินทรัพย์ในแถบตลาด (ทอง/น้ำมัน/บิตคอยน์/ดอลลาร์) ด้วยเครื่องเดียวกับหุ้น"""
    symbol = symbol.strip()
    if symbol not in _MARKET_META:
        return jsonify({"ok": False, "error": "สัญลักษณ์ไม่รองรับ"}), 400
    try:
        r = core.analyze(symbol, read_config())
        name, icon, unit = _MARKET_META[symbol]
        keys = ["ticker", "price", "change_pct", "rsi", "ema20", "ema50", "ema200",
                "support", "resistance", "support_basis", "resistance_basis",
                "atr", "score", "confidence", "confidence_label",
                "verdict", "signals", "spark", "reversal", "prev_close",
                # asof/market_state ต้องส่งออกด้วย ผู้เรียกจะได้ตรวจได้ว่าข้อมูลเป็นของเซสชันไหน
                "asof", "market_state",
                "w52h", "w52l", "high_52w", "low_52w",
                "vol_ratio", "vol_ratio_basis", "vol_ratio_clock_matched",
                "vol_ratio_label", "vol_ratio_raw", "vol_session_pct", "vol_asof",
                "stop_suggest", "target_suggest"]
        out = {k: r.get(k) for k in keys}
        out.update({"ok": True, "name": name, "icon": icon, "unit": unit})
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": f"วิเคราะห์ไม่สำเร็จ: {str(e)[:120]}"}), 500


@app.route("/api/fundamentals/<ticker>")
def api_fundamentals(ticker):
    """ข้อมูลพื้นฐานรายตัว: มูลค่า/กระแสเงินสด/มาร์จิ้น/งบดุล/ปันผล + กราฟรายปี"""
    force = request.args.get("refresh") == "1"
    try:
        r = fund_mod.get_fundamentals(ticker, force=force)
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": f"ดึงข้อมูลพื้นฐานไม่สำเร็จ: {str(e)[:120]}"}), 500


# ----------------------------------------------------------------------
# จำกัดการเรียก AI — จำเป็นเมื่อเปิดเว็บสาธารณะ เพราะทุกคนใช้คีย์ของเจ้าของเว็บ
# ถ้าไม่จำกัด คนเดียวกดรัวก็ทำให้โควตาหมดทั้งวัน (หรือโดนคิดเงินถ้าใช้เจ้าที่จ่ายตามใช้)
# ตั้งค่าได้ผ่าน env: AI_LIMIT_PER_HOUR (ต่อ IP), AI_LIMIT_PER_DAY (รวมทั้งเว็บ)
# ----------------------------------------------------------------------
AI_PER_IP_HOUR = int(os.environ.get("AI_LIMIT_PER_HOUR", "12"))
AI_PER_DAY = int(os.environ.get("AI_LIMIT_PER_DAY", "600"))
_ai_hits = {}          # ip -> [timestamps]
_ai_day = {"date": None, "n": 0}
_ai_lock = threading.Lock()


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def _ai_quota_check():
    """คืน None ถ้าผ่าน, หรือ (ข้อความ, วินาทีที่ต้องรอ) ถ้าเกินโควตา"""
    import time as _t
    from datetime import date as _d
    now = _t.time()
    with _ai_lock:
        today = str(_d.today())
        if _ai_day["date"] != today:
            _ai_day["date"], _ai_day["n"] = today, 0
        if _ai_day["n"] >= AI_PER_DAY:
            return ("โควตา AI ของเว็บวันนี้เต็มแล้ว (ใช้ร่วมกันทุกคน) — "
                    "ลองใหม่พรุ่งนี้ หรือรันโปรแกรมในเครื่องตัวเองเพื่อใช้คีย์ของคุณเอง"), 0
        ip = _client_ip()
        hits = [t for t in _ai_hits.get(ip, []) if now - t < 3600]
        if len(hits) >= AI_PER_IP_HOUR:
            wait = int(3600 - (now - hits[0]))
            return (f"คุณกดวิเคราะห์ครบ {AI_PER_IP_HOUR} ครั้งในชั่วโมงนี้แล้ว — "
                    f"รออีก {max(1, wait // 60)} นาทีแล้วลองใหม่"), wait
        hits.append(now)
        _ai_hits[ip] = hits
        _ai_day["n"] += 1
        if len(_ai_hits) > 500:      # กันหน่วยความจำบวมเมื่อมีผู้ใช้เยอะ
            for k in [k for k, v in _ai_hits.items() if not v or now - v[-1] > 3600]:
                _ai_hits.pop(k, None)
    return None


@app.route("/api/flow/<ticker>")
def api_flow(ticker):
    """รอยเท้าเงินใหญ่: แรงซื้อขายฝั่งออปชัน (เงินจริง) + สแกนการสะสมในกรอบราคาแคบ"""
    ticker = ticker.strip().upper()
    force = request.args.get("refresh") == "1"
    out = {"ok": True, "ticker": ticker}
    try:
        out["options"] = flow_mod.options_flow(ticker, force=force)
    except Exception as e:
        out["options"] = {"ok": False, "error": f"อ่านออปชันไม่สำเร็จ: {str(e)[:100]}"}
    try:
        _, df = core.fetch(ticker)
        out["accumulation"] = flow_mod.accumulation_scan(df, float(df["Close"].iloc[-1]))
        out["rainbow"] = flow_mod.rainbow_gmma(df)
        out["candles"] = flow_mod.candles(df)
        out["chips"] = flow_mod.chip_distribution(df)
    except Exception:
        out["accumulation"] = None
    return jsonify(out)


@app.route("/api/earnings/<ticker>")
def api_earnings(ticker):
    """สรุปผลประกอบการไตรมาสล่าสุด (EPS เทียบที่ตลาดคาด + รายได้/กำไร YoY) ภาษาไทย"""
    force = request.args.get("refresh") == "1"
    try:
        r = fund_mod.get_earnings(ticker, force=force)
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e:
        return jsonify({"ok": False, "error": f"ดึงผลประกอบการไม่สำเร็จ: {str(e)[:120]}"}), 500


@app.route("/api/ai/<ticker>")
def api_ai(ticker):
    """AI วิเคราะห์หุ้นรายตัว — รวมข้อมูลทุกมิติที่ระบบมี"""
    ticker = ticker.strip().upper()
    force = request.args.get("refresh") == "1"
    if not ai_mod.ai_available():
        return jsonify(ai_mod.analyze_with_ai(ticker, {})), 503
    # ผลที่แคชไว้แล้ว (อายุไม่เกิน 1 ชม.) ไม่ต้องเสียโควตา — ปล่อยผ่านได้เลย
    if not force and not ai_mod.has_cached(ticker):
        over = _ai_quota_check()
        if over:
            return jsonify({"ok": False, "error": over[0], "rate_limited": True}), 429
    elif force:
        over = _ai_quota_check()
        if over:
            return jsonify({"ok": False, "error": over[0], "rate_limited": True}), 429
    try:
        r = core.analyze(ticker, read_config())
        smart = insider_sum = None
        news_items = []
        try:
            sigs = news_mod.smart_money_signals([ticker])
            smart = sigs[0] if sigs else None
            items = news_mod.get_insider([ticker])
            overview = news_mod.get_insider_overview([ticker], items)
            insider_sum = overview[0] if overview else None
            # ข่าวเฉพาะหุ้นตัวนี้ขึ้นก่อน แล้วเติมด้วยข่าวตลาดทั่วไป + ส่งเนื้อข่าวย่อ
            raw = news_mod.get_news([ticker], max_items=20)
            own = [n for n in raw if n.get("ticker") == ticker]
            gen = [n for n in raw if n.get("ticker") != ticker]
            news_items = [{
                "หัวข้อ": n.get("title_th") or n.get("title"),
                "สรุป": (n.get("summary_th") or n.get("summary") or "")[:200],
                "แหล่ง": n.get("source"),
                "เกี่ยวกับหุ้นนี้โดยตรง": n.get("ticker") == ticker,
            } for n in (own + gen)[:6]]
        except Exception:
            pass  # ข้อมูลเสริมล่มไม่ควรทำให้ AI วิเคราะห์ไม่ได้ — ใช้เท่าที่มี
        earn = None
        try:
            e = fund_mod.get_earnings(ticker)
            if e.get("ok"):
                earn = {"ประกาศเมื่อ": e["latest"]["date"], "สรุป": e["summary_th"],
                        "ผลเทียบที่คาด": e["latest"]["label"]}
        except Exception:
            pass
        result = ai_mod.analyze_with_ai(ticker, r, smart=smart, insider=insider_sum,
                                        news_items=news_items, earnings=earn, force=force)
        return jsonify(result), (200 if result.get("ok") else 502)
    except Exception as e:
        return jsonify({"ok": False, "error": f"เตรียมข้อมูลไม่สำเร็จ: {str(e)[:120]}"}), 500


@app.route("/api/signals")
def api_signals():
    """สัญญาณเงินใหญ่: สถาบัน/กองทุน/ผู้บริหาร ซื้อ-ขายอย่างมีนัย"""
    force = request.args.get("refresh") == "1"
    try:
        return jsonify({"items": news_mod.smart_money_signals(_tickers_param(), force=force)})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/calendar")
def api_calendar():
    force = request.args.get("refresh") == "1"
    try:
        items = news_mod.get_calendar(_tickers_param(), force=force)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/analyze/<ticker>")
def analyze_one(ticker):
    try:
        return jsonify(core.analyze(ticker.upper(), read_config()))
    except Exception as e:
        return jsonify({"ticker": ticker.upper(), "error": str(e)}), 500


def find_free_port(start=8750):
    import socket
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


if __name__ == "__main__":
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    if "--no-browser" not in sys.argv:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Investing Pro Dashboard: {url}  (กด Ctrl+C หรือปิดหน้าต่างนี้เพื่อหยุด)")
    try:
        app.run(host="127.0.0.1", port=port, debug=False)
    except Exception as e:
        print(f"\nเปิดเซิร์ฟเวอร์ไม่สำเร็จ: {e}")
        input("กด Enter เพื่อปิด...")
