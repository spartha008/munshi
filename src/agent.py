"""The agent loop: Observe -> Normalise -> Reason -> Propose -> (owner OK) -> Act -> Learn.

Every inbound WhatsApp message from an onboarded shop goes through handle(). Replies are
built from the ledger and the rules; nothing is ordered unless the owner's own message says OK.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime

from . import channels, clock, forecast, ledger, trace
from .db import iso, row, rows
from .tools import ordering
from .tools.bill import BillUnreadable, parse_bill
from .tools.names import learn_alias, new_item_from_bill, normalise_item, split_note
from .tools.owner import (ask_owner, close_question, open_question, promote_next, queued_count,
                          schedule_reminder, yes_no)
from .tools.stock import recent_bill_lines_expiring, score_expiry, update_stock


class Reply(str):
    """A text reply, optionally with up to three WhatsApp reply buttons: [(id, title), ...]."""
    buttons: list[tuple[str, str]] | None

    def __new__(cls, text: str, buttons: list[tuple[str, str]] | None = None):
        obj = super().__new__(cls, text)
        obj.buttons = buttons
        return obj


YES_NO = [("yes", "Yes"), ("no", "No")]
ORDER_BUTTONS = [("ok", "OK, send ✅"), ("edit", "Edit ✏️")]

OK_WORDS = {"ok", "okay", "ok send", "send", "send it", "approve", "approved", "theek hai", "thik hai", "sari",
            "done", "👍", "ok.", "ok!", "yes send", "haan bhejo", "bhejo", "kalisi"}
UPI_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)[^.\n]*?(?:credited|received)|"
                    r"(?:credited|received)[^.\n]*?(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

HELP = ("Send me:\n"
        "• a photo of each distributor bill\n"
        "• a voice note or text of what ran out (\"Maggi khatam, Parle-G do bache\")\n"
        "• forwarded UPI payment SMS\n"
        "Type:\n"
        "• \"order\" for this week's order, \"expiry\" for stock at risk, \"stock\" for what's low\n"
        "• \"scheme namkeen 12+1\" to check a salesman's offer\n"
        "• \"udhaar Ramesh 500\" / \"Ramesh paid 300\" for customer credit\n"
        "• \"cash 3800\", \"budget 5000\", \"dues 400\", \"settings\"\n"
        "Nothing is ordered without your OK.")


@dataclass
class Inbound:
    kind: str                       # text | voice | image | upi
    text: str = ""
    image: bytes | None = None
    media_type: str | None = None
    media_path: str | None = None
    wa_message_id: str | None = None


def log_message(conn, shop_id, direction, kind, body, media_path=None, wa_message_id=None) -> int:
    cur = conn.execute("INSERT INTO message (shop_id, direction, kind, body, media_path, wa_message_id, created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (shop_id, direction, kind, body, media_path, wa_message_id, iso(clock.now())))
    return cur.lastrowid


def handle(conn, shop_id: str, msg: Inbound) -> list[Reply]:
    body = msg.text if msg.kind != "image" else (msg.text or "[bill photo]")
    mid = log_message(conn, shop_id, "in", msg.kind, body, msg.media_path, msg.wa_message_id)
    token = trace.bind(conn, shop_id, mid)
    if msg.kind == "voice":
        trace.log("speech_to_text", {"source": "voice note"}, {"transcript": msg.text})
    try:
        replies = [r if isinstance(r, Reply) else Reply(r) for r in _route(conn, shop_id, msg)]
    except Exception as e:  # noqa: BLE001 - the owner gets a plain reply, the trace gets the detail
        trace.log("agent.error", {"text": msg.text}, {"error": f"{type(e).__name__}: {e}"})
        replies = [Reply("Sorry, something went wrong on my side. Please try again.")]
    finally:
        trace.unbind(token)
    for r in replies:
        log_message(conn, shop_id, "out", "text", str(r))
    return replies


def run_job(conn, shop_id: str, label: str, fn) -> list[Reply]:
    """Scheduled work (no inbound message): traced and logged like a turn."""
    mid = log_message(conn, shop_id, "in", "system", label)
    token = trace.bind(conn, shop_id, mid)
    try:
        replies = [r if isinstance(r, Reply) else Reply(r) for r in fn()]
    finally:
        trace.unbind(token)
    for r in replies:
        log_message(conn, shop_id, "out", "text", str(r))
    return replies


def morning(conn, shop_id: str) -> list[Reply]:
    """Salesman-day morning: propose the order."""
    return run_job(conn, shop_id, "⏰ Salesman day, morning", lambda: on_order(conn, shop_id))


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def _route(conn, shop_id: str, msg: Inbound) -> list:
    if msg.kind == "image":
        return on_bill(conn, shop_id, msg)
    text = (msg.text or "").strip()
    if not text:
        return [HELP]
    if msg.kind == "upi" or UPI_RE.search(text):
        return on_upi(conn, shop_id, text)

    low = text.lower().strip()
    q = open_question(conn, shop_id)
    order = ordering.open_order(conn, shop_id)
    yn = yes_no(text)

    if order and low in OK_WORDS:
        return on_approve(conn, shop_id, order, text)
    if q and yn:
        return on_answer(conn, shop_id, q, yn)
    if order and yn == "yes":
        return on_approve(conn, shop_id, order, text)
    if order and (yn == "no" or low == "edit"):
        return ["Okay, nothing is sent. Tell me the change, like \"maggi 36\", \"remove lays\" or "
                "\"add surf 3\". Or say \"order\" for a fresh list."]

    m = re.match(r"^salesman\s+(?:day\s+)?(\w+)$", low)
    if m and m.group(1)[:3] in {d[:3] for d in DAYS}:
        return on_salesman_day(conn, shop_id, m.group(1))
    if re.match(r"^(order|aaj ka order|nale order|salesman|order ready|new order)\b", low):
        return on_order(conn, shop_id)
    m = re.match(r"^scheme\s+(.+?)\s+(\d+)\s*\+\s*(\d+)$", low)
    if m:
        return on_scheme(conn, shop_id, m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(budget|cash|dues)\s*(?:is|=)?\s*₹?\s*([\d,]+)$", low)
    if m:
        return on_money(conn, shop_id, m.group(1), float(m.group(2).replace(",", "")))
    m = (re.match(r"^(?:udhaar|udhar|credit|saala|khata)\s+([a-z][\w .]*?)\s+₹?\s*([\d,]+)$", low)
         or re.match(r"^([a-z][\w .]*?)\s+(?:udhaar|udhar|credit)\s+₹?\s*([\d,]+)$", low))
    if m:
        return on_udhaar(conn, shop_id, m.group(1), float(m.group(2).replace(",", "")))
    m = re.match(r"^([a-z][\w .]*?)\s+(?:paid|ne|diya|ne diya|kotta|kottaru|returned)\s*₹?\s*([\d,]+)"
                 r"(?:\s*(?:diya|kotta|kottaru|paid))?$", low)
    if m and row(conn, "SELECT 1 FROM udhaar WHERE shop_id=? AND lower(customer)=? AND status='open'",
                 shop_id, m.group(1).strip()):
        return on_paid(conn, shop_id, m.group(1), float(m.group(2).replace(",", "")))
    if low in {"udhaar", "udhar", "khata", "credit"}:
        return on_udhaar_list(conn, shop_id)
    m = re.match(r"^distributor\s+(.+?)(?:\s+(\+?[\d ]{10,15}))?$", text, re.I)
    if m:
        return on_distributor(conn, shop_id, m.group(1), m.group(2))
    m = re.match(r"^(?:language|bhasha|bhashe)\s+(\w+)$", low)
    if m:
        return on_language(conn, shop_id, m.group(1))
    if low in {"settings", "setting", "profile"}:
        return on_settings(conn, shop_id)
    if low in {"expiry", "expiry risk", "risk", "returns"}:
        return on_expiry(conn, shop_id)
    if low in {"stock", "low stock", "what's low", "kam"}:
        return on_stock(conn, shop_id)
    if low.split()[0] in {"festival", "christmas", "sankranti", "deepavali", "diwali", "ugadi"}:
        return on_festival(conn, shop_id)
    if low == "pool":
        return on_pool(conn, shop_id)
    if low in {"help", "?", "hi", "hello", "namaskara", "namaste", "menu", "start"}:
        return [HELP]
    m = re.match(r"^(.+?)\s+(?:is|=|matlab)\s+(.+)$", text, re.I)
    if m and _is_pending_bill_name(conn, shop_id, m.group(1)):
        return on_teach(conn, shop_id, m.group(1), m.group(2))
    if order:
        edits = _parse_edits(conn, shop_id, text)
        if edits is not None:
            return on_edit(conn, shop_id, order, edits)
    return on_shelf_check(conn, shop_id, text, msg.kind)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _r(x: float) -> str:
    return f"₹{x:,.0f}"


def _d(s: str) -> str:
    return datetime.fromisoformat(s[:10]).strftime("%d %b").lstrip("0")


def _cover_text(cover: float) -> str:
    if math.isinf(cover):
        return "no recent sales"
    if cover <= 0.05:
        return "out of stock"
    if cover < 1:
        hours = max(1, round(cover * 12))  # a shop day is ~12 hours
        return f"about {hours} hour{'s' if hours != 1 else ''} of stock"
    return f"about {cover:.0f} day{'s' if round(cover) != 1 else ''} of stock"


def _short(name: str) -> str:
    return re.sub(r"\s+\d+\s*(g|kg|ml|l|L)$", "", name).replace("2-Minute Noodles ", "")


def _question_reply(q: dict | None, prefix: str = "") -> list[Reply]:
    return [Reply(prefix + q["text"], YES_NO)] if q else []


# --------------------------------------------------------------------------- #
# Observe: bill photo
# --------------------------------------------------------------------------- #
def on_bill(conn, shop_id: str, msg: Inbound) -> list:
    try:
        data = parse_bill(msg.image or b"", msg.media_type or "image/jpeg")
    except BillUnreadable as e:
        return [str(e)]

    now = clock.now()
    received = iso(now)
    if data.get("bill_date"):
        try:
            d = datetime.fromisoformat(data["bill_date"]).replace(hour=11)
            if d <= now and (now - d).days <= 120:
                received = iso(d)
        except ValueError:
            pass

    cur = conn.execute("""INSERT INTO bill (shop_id, distributor, bill_date, bill_total, lines_total, reconciled,
                          extractor, image_path, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (shop_id, data.get("distributor"), data.get("bill_date"), data.get("bill_total"),
                        data.get("lines_total"), int(bool(data.get("reconciled"))), data.get("extractor"),
                        msg.media_path, iso(now)))
    bill_id = cur.lastrowid

    events, pending, new_items = [], [], []
    for it in data.get("items", []):
        if not it.get("name") or not it.get("qty"):
            continue
        r = normalise_item(conn, shop_id, it["name"], item_only=True)
        sku = r["sku_id"]
        if not sku and not r.get("guess"):
            sku = new_item_from_bill(conn, shop_id, it, data.get("bill_date"))
            new_items.append(it["name"])
        elif not sku:
            pending.append((it, r))
        free = 0
        m = re.match(r"^\s*(\d+)\s*\+\s*(\d+)\s*$", it.get("scheme") or "")
        if m and int(m.group(1)):
            free = (int(it["qty"]) // int(m.group(1))) * int(m.group(2))
        events.append({"type": "purchase", "bill_id": bill_id, "sku_id": sku, "printed_name": it["name"],
                       "qty": it["qty"], "free_qty": free, "unit_price": it.get("unit_price"),
                       "batch": it.get("batch"), "expiry": it.get("expiry"), "scheme_terms": it.get("scheme"),
                       "received_on": received})
        if sku and it.get("mrp"):
            conn.execute("UPDATE item SET mrp=? WHERE sku_id=? AND shop_id=?", (it["mrp"], sku, shop_id))
    update_stock(conn, shop_id, events)
    _remember_distributor(conn, shop_id, data.get("distributor"))

    near = recent_bill_lines_expiring(conn, bill_id, within_days=20)
    n = len(events)
    total = data.get("bill_total") or data.get("lines_total") or 0
    head = f"Got it: {n} items from {data.get('distributor') or 'the distributor'}"
    if data.get("bill_date"):
        head += f", bill of {_d(data['bill_date'])}"
    head += f", {_r(total)}."
    if near:
        parts = [f"{_short(p['canonical_name'] or p['printed_name'])} ×{p['qty']:.0f}" for p in near]
        head += f" {len(near)} expire within 20 days — " + ", ".join(parts) + "."
    if new_items:
        head += f" {len(new_items)} new item{'s' if len(new_items) > 1 else ''} added to your list."
    replies: list = [head]

    risks = [r for r in score_expiry(conn, shop_id) if r["days_to_expiry"] <= 30]
    if risks:
        lines = [f"• {_short(r['name'])}: {r['at_risk_units']:.0f} won't sell before {_d(r['expiry'])} "
                 f"({_r(r['rupees_at_risk'])}) → {r['text']}" for r in risks]
        replies.append(f"{_r(sum(r['rupees_at_risk'] for r in risks))} may expire on the shelf:\n" + "\n".join(lines))

    asked = None
    if not data.get("reconciled") and data.get("bill_total"):
        asked = ask_owner(conn, shop_id, f"The lines add up to {_r(data['lines_total'])} but the bill says "
                                         f"{_r(data['bill_total'])}. Is {_r(data['bill_total'])} right?",
                          "bill_total", {"bill_id": bill_id}) or asked
    for it, r in pending:
        ids = [p["id"] for p in rows(conn, "SELECT id FROM purchase WHERE bill_id=? AND printed_name=? "
                                           "AND status='pending'", bill_id, it["name"])]
        asked = ask_owner(conn, shop_id, r["question"], "bill_alias",
                          {"printed_name": it["name"], "guess": r.get("guess"), "purchase_ids": ids,
                           "line": it, "bill_date": data.get("bill_date")}) or asked
    replies += _question_reply(asked, "One question: ")
    waiting = queued_count(conn, shop_id)
    if waiting:
        replies.append(f"({waiting} more question{'s' if waiting > 1 else ''} saved for later — "
                       "I ask at most three a day.)")
    return replies


def _remember_distributor(conn, shop_id: str, name: str | None) -> None:
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    if name and not shop["distributor_id"]:
        cur = conn.execute("INSERT INTO distributor (shop_id, name) VALUES (?,?)", (shop_id, name))
        conn.execute("UPDATE shop SET distributor_id=? WHERE shop_id=?", (cur.lastrowid, shop_id))


# --------------------------------------------------------------------------- #
# Observe: voice / text shelf check
# --------------------------------------------------------------------------- #
def on_shelf_check(conn, shop_id: str, text: str, kind: str) -> list:
    events, done, unknown = [], [], []
    asked = None
    for phrase in split_note(text):
        r = normalise_item(conn, shop_id, phrase)
        if r["sku_id"] and r["action"] in {"sold_out", "count", "running_low"}:
            events.append({"type": "shelf_check", "sku_id": r["sku_id"], "status": r["action"],
                           "count": 0 if r["action"] == "sold_out" else r["qty"], "source": kind, "raw": phrase})
            done.append(r)
        elif not r["sku_id"] and r.get("guess") and r["action"] != "unknown":
            asked = ask_owner(conn, shop_id, r["question"], "shelf_alias",
                              {"phrase": r["phrase"], "guess": r.get("guess"), "action": r["action"],
                               "qty": r["qty"]}) or asked
        elif not r["sku_id"] and r["action"] != "unknown" and r["phrase"]:
            unknown.append(r["phrase"])
    replies: list = []
    if events:
        update_stock(conn, shop_id, events)
        lines = []
        for r in done:
            s = ledger.item_state(conn, shop_id, ledger.item_by_sku(conn, r["sku_id"]))
            if r["action"] == "sold_out":
                lines.append(f"• {s.name}: sold out." + (f" Sells ~{s.velocity:.1f}/day, so it goes first on "
                                                         "the next order." if s.velocity > 0 else ""))
            elif r["action"] == "running_low":
                lines.append(f"• {s.name}: running low, {_cover_text(s.cover_days)}.")
            else:
                lines.append(f"• {s.name}: {r['qty']:.0f} left, {_cover_text(s.cover_days)}"
                             + (f" at ~{s.velocity:.1f}/day." if s.velocity > 0 else "."))
        replies.append("Noted:\n" + "\n".join(lines))
    if unknown:
        replies.append(f"I don't know '{unknown[0]}' yet. Say it with the brand and size, "
                       "e.g. \"Good Day 100g khatam\" — or send the bill it came on.")
    replies += _question_reply(asked)
    if not replies:
        replies = ["I didn't catch an item there. Try \"Maggi khatam, Parle-G do bache\", or say \"help\"."]
    return replies


# --------------------------------------------------------------------------- #
# Learn: answers to yes/no questions
# --------------------------------------------------------------------------- #
def _is_pending_bill_name(conn, shop_id: str, name: str) -> bool:
    return bool(row(conn, "SELECT 1 FROM purchase WHERE shop_id=? AND status='pending' AND lower(printed_name)=?",
                    shop_id, name.strip().lower()))


def _confirm_bill_alias(conn, shop_id: str, printed: str, sku_id: str) -> str:
    learn_alias(conn, shop_id, printed, sku_id, "bill")
    conn.execute("UPDATE purchase SET sku_id=?, status='ok' WHERE shop_id=? AND status='pending' "
                 "AND lower(printed_name)=?", (sku_id, shop_id, printed.lower()))
    update_stock(conn, shop_id, [])
    item = ledger.item_by_sku(conn, sku_id)
    return f"Saved: '{printed}' = {item['canonical_name']}. Added to stock, and I won't ask again."


def on_answer(conn, shop_id: str, q: dict, yn: str) -> list:
    p = q["payload"]
    close_question(conn, q["id"], yn)
    replies: list = []
    if q["kind"] == "bill_alias":
        if yn == "yes" and p.get("guess"):
            replies.append(_confirm_bill_alias(conn, shop_id, p["printed_name"], p["guess"]))
        else:
            sku = new_item_from_bill(conn, shop_id, p.get("line") or {"name": p["printed_name"]}, p.get("bill_date"))
            conn.execute("UPDATE purchase SET sku_id=?, status='ok' WHERE shop_id=? AND status='pending' "
                         "AND lower(printed_name)=?", (sku, shop_id, p["printed_name"].lower()))
            update_stock(conn, shop_id, [])
            replies.append(f"Okay, I've added '{p['printed_name']}' as a new item.")
    elif q["kind"] == "shelf_alias":
        if yn == "yes" and p.get("guess"):
            learn_alias(conn, shop_id, p["phrase"], p["guess"], "voice")
            word = ("khatam" if p["action"] == "sold_out" else "kam" if p["action"] == "running_low"
                    else f"{p['qty']:.0f} left")
            replies += on_shelf_check(conn, shop_id, f"{p['phrase']} {word}", "voice")
        else:
            replies.append("Okay, ignored. Say it again with the brand name and I'll learn it.")
    elif q["kind"] == "pool_offer":
        other = row(conn, "SELECT * FROM shop WHERE shop_id=?", p["from_shop"])
        me = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
        sc = row(conn, "SELECT s.*, i.canonical_name AS name FROM scheme s JOIN item i ON i.sku_id=s.sku_id "
                       "WHERE scheme_id=?", p["scheme_id"])
        if yn == "yes" and other and sc:
            replies.append(f"Great. {other['name']} ({other['locality']}) will split {sc['name']} "
                           f"{sc['buy_n']}+{sc['free_m']} with you. Their WhatsApp: +{other['owner_phone']}.")
            try:
                from . import channels
                channels.get_transport().send_text(other["owner_phone"],
                    f"{me['name']} ({me['locality']}) agreed to split {sc['name']} {sc['buy_n']}+{sc['free_m']}. "
                    f"Their WhatsApp: +{me['owner_phone']}.")
            except Exception as e:  # noqa: BLE001 - best effort; the trace shows it
                trace.log("pool_notify.failed", {"to": "requesting shop"}, {"error": str(e)})
        else:
            replies.append("Okay, I'll let them know it's a no.")
    elif q["kind"] == "bill_total":
        if yn == "yes":
            conn.execute("UPDATE bill SET reconciled=1 WHERE bill_id=?", (p["bill_id"],))
            replies.append("Thanks, total confirmed.")
        else:
            replies.append("Okay. Please send a clearer photo of the bill and I'll read it again.")
    replies += _question_reply(promote_next(conn, shop_id), "Next question: ")
    return replies


def on_teach(conn, shop_id: str, printed: str, item_text: str) -> list:
    r = normalise_item(conn, shop_id, item_text, item_only=True)
    sku = r["sku_id"] or r.get("guess")
    if not sku:
        return [f"I couldn't find '{item_text}'. Try the brand and pack size."]
    for q in rows(conn, "SELECT id FROM question WHERE shop_id=? AND status IN ('open','queued') "
                        "AND kind='bill_alias' AND payload LIKE ?", shop_id, f'%{printed.strip()}%'):
        close_question(conn, q["id"], f"taught: {sku}")
    return [_confirm_bill_alias(conn, shop_id, printed.strip(), sku)] + _question_reply(promote_next(conn, shop_id))


# --------------------------------------------------------------------------- #
# Reason + Propose: the order
# --------------------------------------------------------------------------- #
def _format_order(conn, shop_id: str, d: dict) -> list:
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    b = d["budget"]
    if not d["lines"]:
        msg = "Nothing needs ordering this week — every item has enough stock."
        if not ledger.stocked_items(conn, shop_id):
            msg = ("I don't know your stock yet. Send me photos of your recent distributor bills and a voice note "
                   "of what's on the shelf, and I'll have an order ready.")
        return [msg]
    slow = [s for s in d["skipped"] if s["why"] == "slow"]
    day = shop["salesman_day"] or "This week's"
    head = f"{day} order ready: {_r(d['total'])} of your {_r(b['owner_cap'])} budget."
    if b["cap"] < b["owner_cap"]:
        head = (f"{day} order ready: {_r(d['total'])}. You can spend {_r(b['cap'])} this week "
                f"({_r(b['cash_on_hand'])} cash + {_r(b['expected_udhaar'])} likely udhaar back − "
                f"{_r(b['commitments'])} dues), under your {_r(b['owner_cap'])} cap.")
    if d.get("festivals"):
        head += f" Includes extra stock for {' and '.join(d['festivals'])}."
    if slow:
        head += f" {len(slow)} slow mover{'s' if len(slow) > 1 else ''} skipped."
    if d["refused"]:
        r0 = d["refused"][0]
        head += (f" {len(d['refused'])} scheme refused — {_short(r0['name'])} {r0['buy_n']}+{r0['free_m']} "
                 f"would lock {_r(r0['cash_locked'])} for {r0['cover_days']} days.")
    head += " Reply OK or edit."

    lines = []
    for i, l in enumerate(d["lines"], 1):
        free = f" + {l['free_qty']} free" if l["free_qty"] else ""
        lines.append(f"{i}. {l['name']} ×{l['qty']}{free} — {_r(l['cost'])}\n    {l['reason']}")
    extra = []
    if slow:
        extra.append("Skipped (slow): " + "; ".join(f"{s['name']} ({s['detail']})" for s in slow))
    for s in d["skipped"]:
        if s["why"] == "budget":
            extra.append(f"Left out (cash): {s['name']} — {s['detail']}")
    for r in d["refused"]:
        extra.append(f"Scheme refused: {r['name']} {r['buy_n']}+{r['free_m']} — {r['reason']}")
    for p in d["pools"]:
        extra.append(f"Scheme to share: {p['name']} {p['buy_n']}+{p['free_m']} — {p['reason']}. You take "
                     f"{p['you_take']}, a nearby Munshi shop takes {p['they_take']}. Reply \"pool\" to ask them.")
    extra.append("To change it, type like \"maggi 36\" or \"remove lays\".")
    return ["\n".join(lines) + "\n\n" + "\n".join(extra), Reply(head, ORDER_BUTTONS)]


def on_order(conn, shop_id: str) -> list:
    return _format_order(conn, shop_id, ordering.draft_order(conn, shop_id))


def _parse_edits(conn, shop_id: str, text: str) -> list[dict] | None:
    edits = []
    for seg in split_note(text):
        s = seg.strip().lower()
        m = (re.match(r"^(?:remove|cancel|delete|hatao|no)\s+(.+)$", s)
             or re.match(r"^(.+?)\s+(?:nahi|nahin|beda|remove|cancel|hatao|mat|venda)$", s))
        add = s.startswith("add ")
        if m:
            qty, phrase = 0, m.group(1)
        else:
            m = re.match(r"^(?:add\s+)?(.+?)\s+(?:x\s*)?(\d+)$", s) or re.match(r"^(?:add\s+)?(\d+)\s+(.+)$", s)
            if not m:
                return None
            a, b = m.groups()
            qty, phrase = (int(b), a) if b.isdigit() else (int(a), b)
        r = normalise_item(conn, shop_id, phrase, item_only=True)
        if not r["sku_id"]:
            return None
        edits.append({"sku_id": r["sku_id"], "qty": qty, "add": add})
    return edits or None


def on_edit(conn, shop_id: str, order: dict, edits: list[dict]) -> list:
    res = ordering.edit_order(conn, order["order_id"], edits)
    if not res["ok"]:
        return [f"That would make the order {_r(res['total'])}, over your {_r(res['cap'])} limit. Remove something "
                f"or raise the cap (\"budget {int(math.ceil(res['total'] / 100) * 100)}\")."]
    lines = ordering.order_lines(conn, order["order_id"])
    body = "\n".join(f"{i}. {l['name']} ×{l['qty']}" + (f" + {l['free_qty']} free" if l["free_qty"] else "")
                     for i, l in enumerate(lines, 1))
    return [Reply(f"Updated: {_r(res['total'])} of {_r(res['cap'])}.\n{body}", ORDER_BUTTONS)]


# --------------------------------------------------------------------------- #
# Owner approves -> Act
# --------------------------------------------------------------------------- #
def on_approve(conn, shop_id: str, order: dict, owner_text: str) -> list:
    ordering.approve_order(conn, order["order_id"], owner_text)
    sent = ordering.send_order(conn, order["order_id"])
    n = len(ordering.order_lines(conn, order["order_id"]))
    if sent["channel"] == "whatsapp":
        replies: list = [f"Sent to {sent['sent_to']} on WhatsApp: {n} items, {_r(sent['total'])}."]
    else:
        if not sent["has_phone"]:
            why = "I don't have their WhatsApp number yet (type \"distributor <name> <number>\")"
        elif sent["error_code"] in channels.OUTSIDE_WINDOW_ERRORS:
            why = "WhatsApp only lets me message them after they've messaged this number in the last 24 hours"
        else:
            why = "WhatsApp didn't deliver it just now"
        replies = [f"Order approved: {n} items, {_r(sent['total'])}. I couldn't send it to {sent['sent_to']} myself "
                   f"({why}), so here it is to forward:", sent["body"]]
    reminders = []
    for r in score_expiry(conn, shop_id):
        if r["action"] == "return" and r["return_by"]:
            reason = f"return {r['at_risk_units']:.0f} {_short(r['name'])} to {sent['sent_to']}"
            schedule_reminder(conn, shop_id, r["sku_id"], r["return_by"], reason, r["rupees_at_risk"])
            when = "today" if r["return_by"] == clock.today().isoformat() else f"before {_d(r['return_by'])}"
            reminders.append(f"{reason} {when}, when the return window closes")
    if reminders:
        replies.append("Reminder: " + "; ".join(reminders) + ". Hand them back with this delivery.")
    return replies


# --------------------------------------------------------------------------- #
# Money, settings, other intents
# --------------------------------------------------------------------------- #
def on_upi(conn, shop_id: str, text: str) -> list:
    m = UPI_RE.search(text)
    if not m:
        return ["I couldn't read an amount in that SMS."]
    amount = float((m.group(1) or m.group(2)).replace(",", ""))
    conn.execute("INSERT INTO upi_sale (shop_id, amount, received_at, raw) VALUES (?,?,?,?)",
                 (shop_id, amount, iso(clock.now()), None))  # amounts only, never sender names
    trace.log("read_upi_sms", {"sms": "<redacted>"}, {"amount": amount})
    tot = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM upi_sale WHERE shop_id=? "
                       "AND substr(received_at,1,10)=?", (shop_id, clock.today().isoformat())).fetchone()
    return [f"✓ {_r(amount)} UPI payment noted. Today so far: {_r(tot[1])} from {tot[0]} payment"
            f"{'s' if tot[0] != 1 else ''}. (Only amounts are kept, never names.)"]


def on_money(conn, shop_id: str, which: str, amount: float) -> list:
    col = {"budget": "cash_budget", "cash": "cash_on_hand", "dues": "commitments"}[which]
    conn.execute(f"UPDATE shop SET {col}=? WHERE shop_id=?", (amount, shop_id))
    trace.log(f"set_{col}", {"owner_said": amount}, {col: amount})
    msg = {"budget": f"Done: your order cap is now {_r(amount)}.",
           "cash": f"Noted: {_r(amount)} cash in the galla.",
           "dues": f"Noted: {_r(amount)} of bills to pay this week."}[which]
    order = ordering.open_order(conn, shop_id)
    if not order:
        return [msg]
    if order["status"] == "edited":
        # Keep the owner's edits; only re-check them against the new limit.
        cap = ordering.budget_for(conn, shop_id).cap
        conn.execute("UPDATE orders SET budget_cap=? WHERE order_id=?", (cap, order["order_id"]))
        if order["budget_used"] > cap + 0.01:
            return [msg + f" Your edited order is {_r(order['budget_used'])}, over this week's limit of {_r(cap)}. "
                          "Remove something, or say \"order\" for a fresh list."]
        return [Reply(msg + f" Your edited order ({_r(order['budget_used'])}) still fits within {_r(cap)}.",
                      ORDER_BUTTONS)]
    return [msg + " Here's the order again:"] + on_order(conn, shop_id)


def on_udhaar(conn, shop_id: str, customer: str, amount: float) -> list:
    name = customer.strip().title()
    conn.execute("INSERT INTO udhaar (shop_id, customer, amount, given_on) VALUES (?,?,?,?)",
                 (shop_id, name, amount, clock.today().isoformat()))
    trace.log("udhaar_given", {"customer": name}, {"amount": amount})
    total = conn.execute("SELECT COALESCE(SUM(amount),0) FROM udhaar WHERE shop_id=? AND status='open' "
                         "AND customer=?", (shop_id, name)).fetchone()[0]
    return [f"Noted: {_r(amount)} udhaar to {name}. {name} now owes {_r(total)}."]


def on_paid(conn, shop_id: str, customer: str, amount: float) -> list:
    left = amount
    entries = rows(conn, "SELECT * FROM udhaar WHERE shop_id=? AND lower(customer)=? AND status='open' "
                         "ORDER BY given_on", shop_id, customer.strip().lower())
    for u in entries:
        if left <= 0:
            break
        pay = min(left, u["amount"])
        rest = u["amount"] - pay
        conn.execute("UPDATE udhaar SET amount=?, status=? WHERE id=?", (rest, "open" if rest > 0.5 else "paid", u["id"]))
        left -= pay
    name = entries[0]["customer"] if entries else customer.title()
    owes = conn.execute("SELECT COALESCE(SUM(amount),0) FROM udhaar WHERE shop_id=? AND status='open' "
                        "AND lower(customer)=?", (shop_id, customer.strip().lower())).fetchone()[0]
    trace.log("udhaar_paid", {"customer": name}, {"amount": amount, "still_owes": owes})
    return [f"Noted: {name} paid {_r(amount)}." + (f" Still owes {_r(owes)}." if owes > 0.5 else " All clear now.")]


def on_udhaar_list(conn, shop_id: str) -> list:
    entries = rows(conn, "SELECT customer, SUM(amount) AS amt, MIN(given_on) AS since FROM udhaar WHERE shop_id=? "
                         "AND status='open' GROUP BY customer ORDER BY amt DESC", shop_id)
    if not entries:
        return ["No udhaar outstanding. Add one with \"udhaar Ramesh 500\"."]
    b = ordering.budget_for(conn, shop_id)
    lines = [f"• {e['customer']}: {_r(e['amt'])} (since {_d(e['since'])})" for e in entries]
    return [f"Udhaar outstanding: {_r(sum(e['amt'] for e in entries))}. About {_r(b.expected_udhaar)} is likely "
            f"back this week.\n" + "\n".join(lines)]


def on_salesman_day(conn, shop_id: str, day: str) -> list:
    full = next(d for d in DAYS if d.startswith(day[:3])).title()
    conn.execute("UPDATE shop SET salesman_day=? WHERE shop_id=?", (full, shop_id))
    return [f"Done: I'll have the order ready on {full} mornings."]


def on_distributor(conn, shop_id: str, name: str, phone: str | None) -> list:
    from .onboarding import clean_phone
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    ph = clean_phone(phone) if phone else None
    if shop["distributor_id"]:
        conn.execute("UPDATE distributor SET name=?, phone=COALESCE(?, phone) WHERE distributor_id=?",
                     (name.strip(), ph, shop["distributor_id"]))
    else:
        cur = conn.execute("INSERT INTO distributor (shop_id, name, phone) VALUES (?,?,?)", (shop_id, name.strip(), ph))
        conn.execute("UPDATE shop SET distributor_id=? WHERE shop_id=?", (cur.lastrowid, shop_id))
    return [f"Saved distributor: {name.strip()}" + (f", {ph}." if ph else ".")]


def on_language(conn, shop_id: str, lang: str) -> list:
    code = {"kannada": "kn", "ಕನ್ನಡ": "kn", "kn": "kn", "hindi": "hi", "हिंदी": "hi", "hi": "hi",
            "english": "en", "en": "en"}.get(lang.lower())
    if not code:
        return ["I can reply in English, Kannada or Hindi. Type \"language kannada\", \"language hindi\" or "
                "\"language english\"."]
    conn.execute("UPDATE shop SET language=? WHERE shop_id=?", (code, shop_id))
    return [f"Done: I'll reply in {dict(kn='Kannada', hi='Hindi', en='English')[code]}."]


def on_settings(conn, shop_id: str) -> list:
    s = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    d = row(conn, "SELECT * FROM distributor WHERE distributor_id=?", s["distributor_id"]) if s["distributor_id"] else None
    b = ordering.budget_for(conn, shop_id)
    return [f"{s['name']} · {s['locality'] or 'area not set'}\n"
            f"• Salesman day: {s['salesman_day']}\n"
            f"• Distributor: {(d['name'] + (', ' + d['phone'] if d['phone'] else ', no number')) if d else 'not set'}\n"
            f"• Order cap: {_r(s['cash_budget'])} · cash: "
            f"{_r(s['cash_on_hand']) if s['cash_on_hand'] is not None else 'not told'} · dues: {_r(s['commitments'])}\n"
            f"• Likely udhaar back this week: {_r(b.expected_udhaar)}\n"
            f"• Language: {dict(kn='Kannada', hi='Hindi', en='English').get(s['language'], 'English')}\n"
            "Change with \"budget 5000\", \"cash 3800\", \"dues 400\", \"salesman friday\", "
            "\"distributor Sharma 9876543210\", \"language kannada\"."]


def on_scheme(conn, shop_id: str, phrase: str, buy: int, free: int) -> list:
    r = normalise_item(conn, shop_id, phrase, item_only=True)
    if not r["sku_id"]:
        return [f"Which item is '{phrase}'? Say it with the brand, e.g. \"scheme haldiram bhujia 12+1\"."]
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    s = ledger.item_state(conn, shop_id, ledger.item_by_sku(conn, r["sku_id"]))
    if s.velocity <= 0:
        return [f"I haven't seen {s.name} sell at your shop yet, so I can't judge {buy}+{free}. "
                "Send a bill and a shelf count first — until then, skip schemes on items you don't know."]
    cur = conn.execute("""INSERT INTO scheme (shop_id, distributor_id, sku_id, buy_n, free_m, valid_till, offered_on)
                          VALUES (?,?,?,?,?,?,?)""", (shop_id, shop["distributor_id"], r["sku_id"], buy, free,
                                                      clock.today().isoformat(), iso(clock.now())))
    sc = row(conn, "SELECT * FROM scheme WHERE scheme_id=?", cur.lastrowid)
    v = ordering.evaluate_scheme(conn, shop_id, sc, s)
    head = f"{s.name} {buy}+{free}: "
    if v.verdict == "TAKE":
        return [head + f"TAKE. It {v.reason}."]
    if v.verdict == "POOL":
        p = ordering.pool_with_neighbour(conn, shop_id, r["sku_id"], buy, free) or {}
        return [head + f"SHARE IT. {v.reason.capitalize()}. You'd take {p.get('you_take')}, a nearby Munshi shop "
                       f"{p.get('they_take')}. Reply \"pool\" to ask them."]
    return [head + f"SKIP. It {v.reason}. You sell ~{s.velocity * 30:.0f} a month; buy normally instead."]


def on_expiry(conn, shop_id: str) -> list:
    risks = score_expiry(conn, shop_id)
    if not risks:
        return ["Nothing looks likely to expire on the shelf right now."]
    lines = [f"• {r['name']}: {r['at_risk_units']:.0f} of {r['units']:.0f} expire {_d(r['expiry'])} "
             f"({_r(r['rupees_at_risk'])}) → {r['text']}" for r in risks]
    return [f"{_r(sum(r['rupees_at_risk'] for r in risks))} at risk:\n" + "\n".join(lines)]


def on_stock(conn, shop_id: str) -> list:
    states = [s for s in ledger.all_states(conn, shop_id) if s.velocity > 0]
    if not states:
        return ["I don't have enough to go on yet. Send your distributor bills and a shelf voice note."]
    low = sorted(states, key=lambda s: s.cover_days)[:8]
    lines = [f"• {s.name}: {s.on_hand:.0f} left, {_cover_text(s.cover_days)}" for s in low]
    return ["Lowest cover first:\n" + "\n".join(lines)]


def on_festival(conn, shop_id: str) -> list:
    f = forecast.festival_forecast(conn, shop_id)
    if not f:
        return ["No festival in my calendar yet."]
    if not f["lines"]:
        return [f"{f['festival']} is in {f['days_away']} days. Once I've seen a couple of weeks of your sales, "
                "I'll tell you how much extra to stock."]
    cats = ", ".join(f"{k} +{v['uplift_pct']:.0f}% ({v['low_pct']:.0f}–{v['high_pct']:.0f}%)"
                     for k, v in f["categories"].items())
    lines = [f"• {l['name']}: ~{l['festival_week']} that week (range {l['low']}–{l['high']}; normal "
             f"{l['normal_week']})" for l in f["lines"]]
    src = ("from Munshi shops near you" if f["source"] == "cohort" else
           "starting estimates — they sharpen as shops near you build history")
    return [f"{f['festival']} is in {f['days_away']} days. Typical rise ({src}): {cats}.\n" + "\n".join(lines)
            + "\nThe order for festival week will include this extra automatically."]


def on_pool(conn, shop_id: str) -> list:
    pools = rows(conn, "SELECT s.*, i.canonical_name AS name FROM scheme s JOIN item i ON i.sku_id=s.sku_id "
                       "WHERE s.shop_id=? AND s.verdict='POOL' ORDER BY s.scheme_id DESC LIMIT 1", shop_id)
    if not pools:
        return ["There's no scheme to share right now."]
    p = pools[0]
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    nb = row(conn, """SELECT s.shop_id FROM sales_estimate se JOIN shop s ON s.shop_id=se.shop_id
                      WHERE se.sku_id=? AND s.shop_id!=? AND s.locality=? AND s.onboarding_step IS NULL
                      ORDER BY se.velocity_per_day DESC LIMIT 1""", p["sku_id"], shop_id, shop["locality"])
    if not nb:
        return ["No other Munshi shop near you stocks it yet, so there's no one to split with. Skip this scheme."]
    q = ask_owner(conn, nb["shop_id"], f"A Munshi shop near you wants to split {p['name']} {p['buy_n']}+{p['free_m']} "
                                       "from the distributor. Interested? (Your names are shared only if you both say yes.)",
                  "pool_offer", {"from_shop": shop_id, "scheme_id": p["scheme_id"]})
    trace.log("pool_request", {"sku_id": p["sku_id"]}, {"queued_for_neighbour": True, "asked_now": bool(q)})
    return [f"Asked a nearby Munshi shop about splitting {p['name']} {p['buy_n']}+{p['free_m']}. "
            "I'll tell you when they reply; your shop's name is shared only if they agree."]
