"""First conversation with a new shop, entirely on WhatsApp. Six short answers, then Munshi is ready."""
from __future__ import annotations

import re

from . import clock, trace
from .agent import DAYS, HELP, Reply, log_message
from .db import iso, row

LANG_BUTTONS = [("lang_en", "English"), ("lang_kn", "ಕನ್ನಡ"), ("lang_hi", "हिंदी")]
# Button ids, full names and 1/2/3 only. Bare codes are left out on purpose: "hi" is a greeting,
# and a first-time owner typing it must not land in Hindi onboarding.
LANGS = {"lang_en": "en", "english": "en", "1": "en",
         "lang_kn": "kn", "kannada": "kn", "ಕನ್ನಡ": "kn", "2": "kn",
         "lang_hi": "hi", "hindi": "hi", "हिंदी": "hi", "3": "hi"}


def clean_phone(s: str | None) -> str | None:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 10:
        digits = "91" + digits
    return digits if 11 <= len(digits) <= 15 else None


def start(conn, wa_id: str, profile_name: str | None, channel: str = "meta") -> list[Reply]:
    conn.execute("""INSERT INTO shop (shop_id, owner_name, owner_phone, onboarding_step, channel, created_at,
                    last_inbound_at) VALUES (?,?,?,?,?,?,?)""",
                 (wa_id, profile_name, wa_id, "language", channel, iso(clock.now()), iso(clock.now())))
    return [Reply("Namaskara! ನಮಸ್ಕಾರ! नमस्ते!\nI'm Munshi, your shop's clerk on WhatsApp. I read your distributor "
                  "bills, keep track of stock and expiry, and get the salesman's order ready within your cash. "
                  "I never order anything without your OK.\n\nWhich language should I use?", LANG_BUTTONS)]


def _ask(step: str) -> Reply:
    return Reply({
        "name": "What is your shop's name?",
        "locality": "Which area is the shop in? (e.g. Kuvempunagar, Mysuru)",
        "distributor": "Who is your main distributor? Send the name and their WhatsApp number, "
                       "e.g. \"Sharma Distributors 9876543210\". Or type \"skip\".",
        "salesman_day": "Which day does the salesman come for the order? (e.g. Thursday)",
        "budget": "At most, how much cash can you spend on one weekly order? (e.g. 5000)",
    }[step])


def handle(conn, shop: dict, text: str) -> list[Reply]:
    sid, step, t = shop["shop_id"], shop["onboarding_step"], (text or "").strip()
    mid = log_message(conn, sid, "in", "text", t)
    token = trace.bind(conn, sid, mid)
    try:
        replies = _step(conn, sid, step, t)
        trace.log("onboarding", {"step": step}, {"next": row(conn, "SELECT onboarding_step FROM shop WHERE shop_id=?",
                                                              sid)["onboarding_step"]})
    finally:
        trace.unbind(token)
    for r in replies:
        log_message(conn, sid, "out", "text", str(r))
    return replies


def _next(conn, sid: str, step: str | None) -> None:
    conn.execute("UPDATE shop SET onboarding_step=? WHERE shop_id=?", (step, sid))


def _step(conn, sid: str, step: str, t: str) -> list[Reply]:
    low = t.lower()
    if step == "language":
        code = LANGS.get(low)
        if not code:
            return [Reply("Please pick a language:", LANG_BUTTONS)]
        conn.execute("UPDATE shop SET language=? WHERE shop_id=?", (code, sid))
        _next(conn, sid, "name")
        return [_ask("name")]
    if step == "name":
        if len(t) < 2 or len(t) > 80:
            return [_ask("name")]
        conn.execute("UPDATE shop SET name=? WHERE shop_id=?", (t, sid))
        _next(conn, sid, "locality")
        return [_ask("locality")]
    if step == "locality":
        if len(t) < 2 or len(t) > 120:
            return [_ask("locality")]
        conn.execute("UPDATE shop SET locality=? WHERE shop_id=?", (t, sid))
        _next(conn, sid, "distributor")
        return [_ask("distributor")]
    if step == "distributor":
        if low not in {"skip", "no", "later"}:
            m = re.match(r"^(.*?)[\s,:-]*(\+?[\d][\d \-]{8,16})?$", t)
            name = (m.group(1) if m else t).strip() or t
            phone = clean_phone(m.group(2)) if m and m.group(2) else None
            cur = conn.execute("INSERT INTO distributor (shop_id, name, phone) VALUES (?,?,?)", (sid, name, phone))
            conn.execute("UPDATE shop SET distributor_id=? WHERE shop_id=?", (cur.lastrowid, sid))
        _next(conn, sid, "salesman_day")
        return [_ask("salesman_day")]
    if step == "salesman_day":
        day = next((d for d in DAYS if low[:3] == d[:3]), None)
        if not day:
            return [_ask("salesman_day")]
        conn.execute("UPDATE shop SET salesman_day=? WHERE shop_id=?", (day.title(), sid))
        _next(conn, sid, "budget")
        return [_ask("budget")]
    if step == "budget":
        m = re.search(r"\d[\d,]*", t)
        amount = float(m.group(0).replace(",", "")) if m else 0
        if amount < 100:
            return [_ask("budget")]
        conn.execute("UPDATE shop SET cash_budget=? WHERE shop_id=?", (amount, sid))
        _next(conn, sid, None)
        s = row(conn, "SELECT * FROM shop WHERE shop_id=?", sid)
        return [Reply(f"All set, {s['name']}! Order cap ₹{amount:,.0f}, salesman on {s['salesman_day']}.\n\n"
                      "To start: send me photos of your last few distributor bills, then a short voice note "
                      "walking the shelf (\"Maggi khatam, Parle-G do bache…\"). Within a week I'll have your "
                      "order ready before the salesman comes."),
                Reply(HELP)]
    _next(conn, sid, None)
    return [Reply(HELP)]
