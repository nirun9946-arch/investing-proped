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

import investing_pro as core
import news as news_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))

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
    return send_from_directory(app.static_folder, "index.html")


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


@app.route("/api/quotes")
def api_quotes():
    try:
        return jsonify({"quotes": core.live_quotes(_tickers_param())})
    except Exception as e:
        return jsonify({"error": str(e), "quotes": []}), 500


# แถบตลาดโลก — สัญลักษณ์ที่ตรวจแล้วว่า Yahoo มีข้อมูลจริง
# (XAUUSD=X และ DX=F ใช้ไม่ได้ ถูกตัดออก)
MARKET_SYMBOLS = [
    ("GC=F", "ทองคำ", "🥇", "USD/ounce"),
    ("CL=F", "น้ำมัน WTI", "🛢", "USD/barrel"),
    ("BTC-USD", "บิตคอยน์", "₿", "USD"),
    ("THB=X", "ดอลลาร์/บาท", "💵", "บาทต่อ 1 ดอลลาร์"),
]


@app.route("/api/market")
def api_market():
    """ราคาเรียลไทม์ตลาดโลก: ทองคำ / น้ำมัน / บิตคอยน์ / ดัชนีดอลลาร์"""
    try:
        quotes = {q["ticker"]: q for q in core.live_quotes([s[0] for s in MARKET_SYMBOLS])}
        out = []
        for sym, name, icon, unit in MARKET_SYMBOLS:
            q = quotes.get(sym)
            if not q:
                continue
            out.append({"symbol": sym, "name": name, "icon": icon, "unit": unit,
                        "price": q["price"], "chg": q.get("chg"), "ts": q.get("ts")})
        return jsonify({"items": out})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


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
