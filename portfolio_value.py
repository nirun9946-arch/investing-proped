#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ตีมูลค่าพอร์ตจากสแนปช็อตจำนวนหุ้น + ราคาปิดล่าสุดจริง

ใช้เมื่ออ่านยอดสดจาก Webull ไม่ได้ (เว็บโดนบล็อกภูมิภาค / แอปเปิดไม่ได้ใน scheduled run)
อ่านจำนวนหุ้นและต้นทุนเฉลี่ยจาก  ~/.claude/agents/portfolio.md  ซึ่งเป็นสแนปช็อตจริง
จากสกรีนช็อตของเจ้าของ แล้วคูณด้วยราคาปิดล่าสุดที่ดึงสด

ผลลัพธ์ "ไม่ใช่ยอดจากโบรกเกอร์" — ตั้งอยู่บนสมมติฐานว่าไม่มีการเทรดหลังวันที่ในสแนปช็อต
สคริปต์จะเทียบวันเทรดล่าสุดใน trade-history.md ให้เอง และเตือนถ้าสแนปช็อตเก่ากว่านั้น

ใช้:  python portfolio_value.py            # ราคาปิดล่าสุด
      python portfolio_value.py --json     # ออกเป็น JSON
"""
import argparse
import json
import os
import re
import sys

import investing_pro as core

HOME = os.path.expanduser("~")
PORTFOLIO_MD = os.path.join(HOME, ".claude", "agents", "portfolio.md")
TRADES_MD = os.path.join(HOME, ".claude", "agents", "trade-history.md")

_NUM = r"[-+]?\$?\s*([\d,]+\.?\d*)"


def _f(s):
    """แปลงข้อความตัวเลขที่มี $ , ** ปนอยู่ให้เป็น float — คืน None ถ้าแปลงไม่ได้"""
    if s is None:
        return None
    s = str(s).replace("*", "").replace("$", "").replace(",", "").strip()
    if not s or s in {"-", "—", "~0"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_portfolio(path=PORTFOLIO_MD):
    """ดึงจำนวนหุ้น/ต้นทุนเฉลี่ย/เงินสด/วันที่สแนปช็อต ออกจาก portfolio.md

    รูปแบบตาราง: | Ticker | Name | Qty | Avg Cost | Last | Mkt Value | P&L | P&L% | Weight |
    อ่านแค่ 4 คอลัมน์แรกที่จำเป็น — คอลัมน์ราคาในไฟล์เป็นของเก่า ไม่ใช้
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"ไม่พบสแนปช็อตพอร์ตที่ {path}")
    text = open(path, encoding="utf-8").read()

    snap = None
    m = re.search(r"\*\*Snapshot captured:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    if m:
        snap = m.group(1)
    priced = None
    m = re.search(r"\*\*Prices as of:\*\*\s*(.+)", text)
    if m:
        priced = m.group(1).strip()

    cash = None
    m = re.search(r"Total Cash \(USD\)\s*\|\s*" + _NUM, text)
    if m:
        cash = _f(m.group(1))

    acct = None
    m = re.search(r"Total Account Value\*?\*?\s*\|\s*\*?\*?" + _NUM, text)
    if m:
        acct = _f(m.group(1))

    positions = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        tk = cells[0].replace("*", "").strip()
        # ต้องเป็นสัญลักษณ์หุ้นจริง ไม่ใช่หัวตารางหรือเส้นคั่น
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", tk):
            continue
        qty, avg = _f(cells[2]), _f(cells[3])
        if qty is None or avg is None or qty <= 0:
            continue
        positions.append({"ticker": tk, "qty": qty, "avg_cost": avg, "name": cells[1]})

    if not positions:
        raise ValueError(f"อ่านรายการหุ้นจาก {path} ไม่ได้เลย — รูปแบบตารางอาจเปลี่ยนไป")
    return {"snapshot_date": snap, "priced_as_of": priced, "cash": cash or 0.0,
            "account_value_at_snapshot": acct, "positions": positions}


def last_trade_date(path=TRADES_MD):
    """วันเทรดล่าสุดที่บันทึกไว้ (รูปแบบ | DD/MM | ...) — ใช้เตือนว่าสแนปช็อตอาจล้าสมัย"""
    if not os.path.exists(path):
        return None
    best = None
    for line in open(path, encoding="utf-8").read().splitlines():
        m = re.match(r"\|\s*(\d{1,2})/(\d{1,2})\s*\|", line.strip())
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            key = (mo, d)
            if best is None or key > best:
                best = key
    return f"{best[1]:02d}/{best[0]:02d}" if best else None


def value_portfolio(snap=None):
    snap = snap or parse_portfolio()
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "config.json"), encoding="utf-8"))
    except Exception:
        pass

    rows, errors = [], []
    for p in snap["positions"]:
        try:
            r = core.analyze(p["ticker"], cfg)
            price, chg, asof = r["price"], r["change_pct"], r["asof"]
            prev = r.get("sess_prev") or (price / (1 + chg / 100) if chg else price)
        except Exception as e:
            errors.append(f"{p['ticker']}: {str(e)[:80]}")
            continue
        mv, cost = p["qty"] * price, p["qty"] * p["avg_cost"]
        rows.append({**p, "price": price, "asof": asof, "change_pct": chg,
                     "mkt_value": mv, "cost": cost, "pl": mv - cost,
                     "pl_pct": (price / p["avg_cost"] - 1) * 100,
                     "day_pl": p["qty"] * (price - prev)})

    mv = sum(r["mkt_value"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    day = sum(r["day_pl"] for r in rows)
    cash = snap["cash"]
    acct = mv + cash
    prev_acct = acct - day
    for r in rows:
        r["weight"] = r["mkt_value"] / mv * 100 if mv else 0.0

    return {"rows": sorted(rows, key=lambda x: -x["mkt_value"]), "errors": errors,
            "market_value": mv, "cash": cash, "account_value": acct,
            "cost_basis": cost, "open_pl": mv - cost,
            "open_pl_pct": (mv / cost - 1) * 100 if cost else 0.0,
            "day_pl": day, "day_pl_pct": (acct / prev_acct - 1) * 100 if prev_acct else 0.0,
            "cash_pct": cash / acct * 100 if acct else 0.0,
            "asof": rows[0]["asof"] if rows else None,
            "snapshot_date": snap["snapshot_date"], "priced_as_of": snap["priced_as_of"],
            "account_value_at_snapshot": snap["account_value_at_snapshot"],
            "last_trade": last_trade_date()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="ออกผลเป็น JSON")
    a = ap.parse_args()
    out = value_portfolio()
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print("=" * 96)
    print("มูลค่าพอร์ต — คำนวณจากสแนปช็อต ไม่ใช่ยอดจากโบรกเกอร์")
    print(f"  จำนวนหุ้น/ต้นทุน จากสแนปช็อต : {out['snapshot_date']}  (ราคาในไฟล์: {out['priced_as_of']})")
    print(f"  ราคาที่ใช้คำนวณ ณ            : {out['asof']}")
    print(f"  เทรดล่าสุดที่บันทึกไว้        : {out['last_trade']}"
          "   ← ถ้ามีเทรดหลังจากนี้ ตัวเลขทั้งหมดผิด")
    print("=" * 96)
    print("%-6s %10s %10s %10s %12s %12s %8s %11s %7s"
          % ("TK", "Qty", "AvgCost", "Price", "MktValue", "OpenP&L", "P&L%", "วันล่าสุด$", "น้ำหนัก%"))
    for r in out["rows"]:
        print("%-6s %10.5f %10.2f %10.2f %12.2f %+12.2f %+7.2f%% %+11.2f %7.2f"
              % (r["ticker"], r["qty"], r["avg_cost"], r["price"], r["mkt_value"],
                 r["pl"], r["pl_pct"], r["day_pl"], r["weight"]))
    print("-" * 96)
    print("มูลค่าหุ้น            $%s" % format(out["market_value"], ",.2f"))
    print("เงินสด                $%s   (%.2f%% ของบัญชี)" % (format(out["cash"], ",.2f"), out["cash_pct"]))
    print("มูลค่าบัญชีรวม        $%s" % format(out["account_value"], ",.2f"))
    print("ต้นทุนรวม             $%s" % format(out["cost_basis"], ",.2f"))
    print("กำไร/ขาดทุนสะสม      $%s  (%+.2f%%)"
          % (format(out["open_pl"], "+,.2f"), out["open_pl_pct"]))
    print("กำไร/ขาดทุนวันล่าสุด  $%s  (%+.2f%% ของบัญชี)"
          % (format(out["day_pl"], "+,.2f"), out["day_pl_pct"]))
    if out["account_value_at_snapshot"]:
        d = out["account_value"] - out["account_value_at_snapshot"]
        print("เทียบวันสแนปช็อต      $%s  (%+.2f%%)"
              % (format(d, "+,.2f"), d / out["account_value_at_snapshot"] * 100))
    if out["errors"]:
        print("\n⚠️ ดึงราคาไม่ได้ (ไม่ถูกนับในยอดรวม):")
        for e in out["errors"]:
            print("   -", e)
    print("\n⚠️ ตัวเลขนี้เป็นการคำนวณ ไม่ใช่ยอดจริงจาก Webull — ต้องระบุให้ชัดทุกครั้งที่รายงาน")


if __name__ == "__main__":
    sys.exit(main())
