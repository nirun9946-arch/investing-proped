# -*- coding: utf-8 -*-
"""
Investing Pro — AI Analyst Module
ส่งข้อมูลจริงทั้งหมดของหุ้น (เทคนิค + Volume Profile + เงินใหญ่ + ข่าว)
ให้ AI วิเคราะห์เป็นภาษาไทยแบบนักวิเคราะห์

รองรับ AI 2 เจ้า — ใส่คีย์ตัวไหนก็ใช้ตัวนั้น:
  • Google Gemini (ฟรี)  → env GEMINI_API_KEY  (สมัครฟรีที่ aistudio.google.com)
  • Anthropic Claude (จ่ายตามใช้) → env ANTHROPIC_API_KEY
หรือใส่ใน config.json: {"ai": {"gemini_key": "...", "anthropic_key": "..."}}
ถ้ามีทั้งคู่ ระบบเลือก Gemini ก่อน (ฟรี) เว้นแต่ตั้ง ai.provider = "claude"
"""

import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

CLAUDE_MODEL = "claude-opus-4-8"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_cache = {}   # ticker -> (ts, analysis_text)
AI_TTL = 3600  # วิเคราะห์ซ้ำตัวเดิมไม่เกินชั่วโมงละครั้ง — คุมค่าใช้จ่าย

SYSTEM_PROMPT = """คุณคือนักวิเคราะห์หุ้นอาวุโสของ Investing Pro เขียนบทวิเคราะห์ภาษาไทยสำหรับนักลงทุนรายย่อย (มีทั้งมือใหม่และมีประสบการณ์)

กติกาเหล็ก:
- วิเคราะห์จากข้อมูลที่ให้มาเท่านั้น ห้ามแต่งตัวเลขหรืออ้างข้อมูลที่ไม่มี ถ้าข้อมูลส่วนไหนขาด ให้บอกตรงๆ ว่าขาด
- ข้อมูลสถาบัน/กองทุนเป็นรายงาน 13F รายไตรมาส (ล่าช้าได้ ~45 วัน) — อย่าตีความเป็นการซื้อขายวันนี้
- ชี้ให้เห็นทั้งฝั่งบวกและฝั่งเสี่ยงเสมอ โดยเฉพาะจุดที่สัญญาณขัดแย้งกัน
- ความน่าจะเป็น 5 วันเป็นสถิติจากอดีต ไม่ใช่คำทำนาย — สื่อสารให้ถูก

รูปแบบคำตอบ (markdown):
**สรุปใน 2 ประโยค** — ภาพรวมที่คนไม่มีเวลาอ่านทั้งหมดควรรู้
**🟢 ฝั่งบวก** — 2-4 ข้อ อ้างตัวเลขจริง
**🔴 ฝั่งเสี่ยง** — 2-4 ข้อ อ้างตัวเลขจริง
**⚔️ จุดที่สัญญาณขัดกัน** — ถ้ามี อธิบายว่าทำไมถึงขัดและควรให้น้ำหนักข้างไหน
**🎯 กลยุทธ์ที่สมเหตุสมผล** — แยกตามประเภท: คนยังไม่มีหุ้น / คนถืออยู่ ระบุโซนราคาอ้างอิงจากข้อมูล (แนวรับ, POC, จุดตัดขาดทุน)
**👀 สิ่งที่ต้องจับตา** — เหตุการณ์/ระดับราคาที่จะเปลี่ยนมุมมอง

ปิดท้ายด้วยบรรทัดเดียว: "_บทวิเคราะห์นี้สร้างโดย AI จากข้อมูล ณ เวลาที่ระบุ เพื่อประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน_"
ความยาวรวมไม่เกิน ~450 คำ กระชับแต่ครบ"""


def _ai_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("ai") or {}
    except Exception:
        return {}


def _provider():
    """เลือกเจ้า AI จากคีย์ที่มี — คืน (provider, key) หรือ (None, None)"""
    cfg = _ai_config()
    gkey = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            or cfg.get("gemini_key"))
    ckey = (os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_key")
            or cfg.get("api_key"))  # api_key = ชื่อเดิม รองรับย้อนหลัง
    pref = (cfg.get("provider") or "").lower()
    if pref == "claude" and ckey:
        return "claude", ckey
    if pref == "gemini" and gkey:
        return "gemini", gkey
    if gkey:                       # ค่าเริ่มต้น: Gemini ก่อน (ฟรี)
        return "gemini", gkey
    if ckey:
        return "claude", ckey
    return None, None


def ai_available():
    return _provider()[0] is not None


def _compact_payload(r, smart=None, insider=None, news_titles=None):
    """คัดเฉพาะข้อมูลที่มีนัยจากผล analyze — ประหยัด token และกันข้อมูลรก"""
    vp = r.get("vp") or {}
    pred = r.get("prediction")
    out = {
        "ticker": r.get("ticker"),
        "ราคา": r.get("price"),
        "เปลี่ยนแปลง_pct": r.get("change_pct"),
        "ราคาปิดก่อนหน้า": r.get("prev_close"),
        "สถานะตลาด": r.get("market_state"),
        "ราคา_premarket": r.get("pre_price"),
        "กรอบ52สัปดาห์": [r.get("w52l"), r.get("w52h")],
        "RSI14": r.get("rsi"),
        "EMA": {"20": r.get("ema20"), "50": r.get("ema50"), "200": r.get("ema200")},
        "แนวรับ": r.get("support"), "แนวต้าน": r.get("resistance"),
        "ATR": r.get("atr"),
        "จุดตัดขาดทุนแนะนำ": r.get("stop_suggest"),
        "เป้าหมายแนะนำ": r.get("target_suggest"),
        "วอลุ่มเทียบเฉลี่ย20วัน": r.get("vol_ratio"),
        "คะแนนเทคนิครวม": r.get("score"),
        "บทสรุประบบ": r.get("verdict"),
        "สัญญาณเทคนิค": [s.get("desc") for s in (r.get("signals") or [])],
        "PE": r.get("pe"), "ForwardPE": r.get("fwd_pe"), "PEG": r.get("peg"),
        "เป้านักวิเคราะห์": r.get("target"), "upside_pct": r.get("upside"),
        "ประเมินมูลค่า": r.get("value_label"),
        "VolumeProfile": {
            "POC": vp.get("poc"), "VAH": vp.get("vah"), "VAL": vp.get("val"),
            "สถิติแตะPOC": {"แตะ": vp.get("touches"), "เด้ง": vp.get("bounces"),
                            "ทะลุ": vp.get("breaks")},
        } if vp else None,
        "สถิติ5วัน": ({"โอกาสขึ้น_pct": round(pred["prob_up"] * 100),
                       "จากเหตุการณ์คล้ายกัน_ครั้ง": pred["n"]} if pred else None),
    }
    if smart:
        out["สัญญาณเงินใหญ่"] = [a.get("text") for a in (smart.get("alerts") or [])]
    if insider:
        out["ภาพในองค์กร"] = {
            "สรุป": insider.get("note"),
            "คนในถือหุ้น_pct": (insider.get("insiders") or 0) * 100,
            "สถาบันถือ_pct": (insider.get("institutions") or 0) * 100,
        }
    if news_titles:
        out["หัวข้อข่าวล่าสุด"] = news_titles[:6]
    return out


_NO_KEY_MSG = ("ยังไม่ได้ตั้งค่าคีย์ AI — ใช้ฟรีได้ด้วย Google Gemini: "
               "สมัครฟรีที่ aistudio.google.com/apikey แล้วตั้ง environment variable "
               "GEMINI_API_KEY (หรือใส่ใน config.json ช่อง ai.gemini_key) จากนั้นรีสตาร์ทโปรแกรม "
               "· ถ้าต้องการใช้ Claude แทน ให้ตั้ง ANTHROPIC_API_KEY")


def _call_claude(key, system, user_msg):
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=120.0)
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=2500,
            thinking={"type": "adaptive"},
            system=system, messages=[{"role": "user", "content": user_msg}],
        )
        if resp.stop_reason == "refusal":
            return {"ok": False, "error": "AI ปฏิเสธการวิเคราะห์คำขอนี้ — ลองใหม่อีกครั้ง"}
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            return {"ok": False, "error": "AI ไม่ได้ส่งคำวิเคราะห์กลับมา — ลองใหม่อีกครั้ง"}
        return {"ok": True, "analysis": text, "model": CLAUDE_MODEL}
    except anthropic.AuthenticationError:
        return {"ok": False, "no_key": True,
                "error": "Claude API key ไม่ถูกต้องหรือถูกยกเลิก — ตรวจสอบที่ console.anthropic.com"}
    except anthropic.RateLimitError:
        return {"ok": False, "error": "เรียก AI ถี่เกินไป — รอสักครู่แล้วลองใหม่"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "error": f"บริการ AI ขัดข้อง (HTTP {e.status_code}) — ลองใหม่ภายหลัง"}
    except anthropic.APIConnectionError:
        return {"ok": False, "error": "เชื่อมต่อบริการ AI ไม่ได้ — ตรวจสอบอินเทอร์เน็ต"}
    except Exception as e:
        return {"ok": False, "error": f"วิเคราะห์ไม่สำเร็จ: {str(e)[:120]}"}


def _call_gemini(key, system, user_msg):
    """เรียก Gemini ผ่าน REST (ไม่ต้องลง SDK เพิ่ม) — ใช้ระบบใช้ฟรีของ Google"""
    import requests
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"maxOutputTokens": 2500, "temperature": 0.6},
    }
    try:
        resp = requests.post(url, params={"key": key}, json=body, timeout=120)
        if resp.status_code in (401, 403):
            return {"ok": False, "no_key": True,
                    "error": "Gemini API key ไม่ถูกต้องหรือถูกปิดสิทธิ์ — ตรวจสอบที่ aistudio.google.com/apikey"}
        if resp.status_code == 429:
            return {"ok": False, "error": "โควตาฟรีของ Gemini เต็มชั่วคราว — รอสักครู่แล้วลองใหม่"}
        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")[:80]
            except Exception:
                pass
            return {"ok": False, "error": f"Gemini ขัดข้อง (HTTP {resp.status_code}) {detail}"}
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            return {"ok": False, "error": f"Gemini ไม่ส่งคำตอบ{' (ถูกกรอง: '+block+')' if block else ''} — ลองใหม่"}
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            return {"ok": False, "error": "Gemini ส่งคำตอบว่าง — ลองใหม่อีกครั้ง"}
        return {"ok": True, "analysis": text, "model": GEMINI_MODEL + " (ฟรี)"}
    except requests.Timeout:
        return {"ok": False, "error": "Gemini ตอบช้าเกินไป — ลองใหม่อีกครั้ง"}
    except requests.ConnectionError:
        return {"ok": False, "error": "เชื่อมต่อ Gemini ไม่ได้ — ตรวจสอบอินเทอร์เน็ต"}
    except Exception as e:
        return {"ok": False, "error": f"วิเคราะห์ไม่สำเร็จ: {str(e)[:120]}"}


def analyze_with_ai(ticker, r, smart=None, insider=None, news_titles=None, force=False):
    """เรียก AI วิเคราะห์ (Gemini ฟรี หรือ Claude) — คืน dict {ok, analysis|error, cached, model}"""
    provider, key = _provider()
    if not provider:
        return {"ok": False, "no_key": True, "error": _NO_KEY_MSG}

    now = time.time()
    cached = _cache.get(ticker)
    if cached and not force and now - cached[0] < AI_TTL:
        return {"ok": True, "analysis": cached[1][0], "cached": True, "model": cached[1][1]}

    from datetime import datetime, timezone, timedelta
    th_time = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M น.")
    payload = _compact_payload(r, smart=smart, insider=insider, news_titles=news_titles)
    user_msg = (f"วิเคราะห์หุ้น {ticker} จากข้อมูล ณ {th_time} (เวลาไทย) ต่อไปนี้:\n\n"
                + json.dumps(payload, ensure_ascii=False, default=str))

    result = (_call_gemini if provider == "gemini" else _call_claude)(key, SYSTEM_PROMPT, user_msg)
    if result.get("ok"):
        _cache[ticker] = (now, (result["analysis"], result["model"]))
        if len(_cache) > 60:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:30]:
                _cache.pop(k, None)
        result["cached"] = False
    return result
