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


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    return jsonify(read_config()["watchlist"])


@app.route("/api/watchlist", methods=["POST"])
def add_ticker():
    ticker = (request.json or {}).get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "กรุณาระบุชื่อหุ้น"}), 400
    cfg = read_config()
    if ticker in cfg["watchlist"]:
        return jsonify({"error": f"{ticker} มีอยู่แล้ว"}), 400
    # ตรวจว่าดึงข้อมูลได้จริงก่อนบันทึก
    try:
        core.fetch(ticker, period="3mo")
    except Exception:
        return jsonify({"error": f"ไม่พบข้อมูล {ticker} — ตรวจสอบชื่อ (หุ้นไทยใส่ .BK เช่น PTT.BK)"}), 404
    cfg["watchlist"].append(ticker)
    write_config(cfg)
    return jsonify({"ok": True, "watchlist": cfg["watchlist"]})


@app.route("/api/watchlist/<ticker>", methods=["DELETE"])
def remove_ticker(ticker):
    cfg = read_config()
    ticker = ticker.upper()
    if ticker in cfg["watchlist"]:
        cfg["watchlist"].remove(ticker)
        write_config(cfg)
    return jsonify({"ok": True, "watchlist": cfg["watchlist"]})


@app.route("/api/news")
def api_news():
    force = request.args.get("refresh") == "1"
    try:
        items = news_mod.get_news(read_config()["watchlist"], force=force)
        return jsonify({"updated": news_mod._cache["ts"], "items": items})
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
        items = news_mod.get_insider(read_config()["watchlist"], force=force)
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
