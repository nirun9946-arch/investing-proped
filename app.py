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

import investing_pro as core
import news as news_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))


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
    try:
        import time
        items = news_mod.get_news(_tickers_param(), force=force)
        return jsonify({"updated": time.time(), "items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/article")
def api_article():
    url = request.args.get("url", "")
    return jsonify(news_mod.fetch_article_th(url))


@app.route("/api/insider")
def api_insider():
    force = request.args.get("refresh") == "1"
    try:
        items = news_mod.get_insider(_tickers_param(), force=force)
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
