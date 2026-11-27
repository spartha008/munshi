"""ask_owner(question) -> yes | no | text;  schedule_reminder(shop, item, date, reason)

One question is open at a time, so a "yes" is never ambiguous, and at most three are
asked a day (README guardrail). The rest wait in a queue for the next day.
"""
from __future__ import annotations

import json

from .. import clock
from ..db import dumps, iso, row
from ..trace import traced

MAX_QUESTIONS_PER_DAY = 3

YES = {"yes", "y", "haan", "ha", "han", "haa", "ji", "houdu", "howdu", "hu", "sari", "correct", "right",
       "sahi", "👍", "ok", "okay", "ಹೌದು", "हाँ", "हां"}
NO = {"no", "n", "nahi", "nahin", "na", "illa", "alla", "wrong", "galat", "beda", "ಇಲ್ಲ", "नहीं"}


def yes_no(text: str) -> str | None:
    t = text.strip().lower().strip(".!?")
    if t in YES:
        return "yes"
    if t in NO:
        return "no"
    return None


def questions_today(conn, shop_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM question WHERE shop_id=? AND substr(asked_on,1,10)=?",
                        (shop_id, clock.today().isoformat())).fetchone()[0]


def open_question(conn, shop_id: str) -> dict | None:
    q = row(conn, "SELECT * FROM question WHERE shop_id=? AND status='open' ORDER BY id LIMIT 1", shop_id)
    if q:
        q["payload"] = json.loads(q["payload"] or "{}")
    return q


def queued_count(conn, shop_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM question WHERE shop_id=? AND status='queued'", (shop_id,)).fetchone()[0]


def promote_next(conn, shop_id: str) -> dict | None:
    """Open the oldest queued question if none is open and today's three aren't used up."""
    if open_question(conn, shop_id) or questions_today(conn, shop_id) >= MAX_QUESTIONS_PER_DAY:
        return None
    q = row(conn, "SELECT * FROM question WHERE shop_id=? AND status='queued' ORDER BY id LIMIT 1", shop_id)
    if not q:
        return None
    conn.execute("UPDATE question SET status='open', asked_on=? WHERE id=?", (iso(clock.now()), q["id"]))
    return {"id": q["id"], "text": q["text"]}


@traced("ask_owner")
def ask_owner(conn, shop_id: str, text: str, kind: str, payload: dict) -> dict | None:
    """Queue a yes/no question. Returns the question to send now, if one can be asked now."""
    conn.execute("INSERT INTO question (shop_id, created_on, text, kind, payload) VALUES (?,?,?,?,?)",
                 (shop_id, iso(clock.now()), text, kind, dumps(payload)))
    return promote_next(conn, shop_id)


def close_question(conn, qid: int, answer: str) -> None:
    conn.execute("UPDATE question SET status='answered', answer=? WHERE id=?", (answer, qid))


@traced("schedule_reminder")
def schedule_reminder(conn, shop_id: str, sku_id: str | None, due_on: str, reason: str,
                      rupees: float | None = None) -> dict:
    # One reminder per item and date: a re-estimate of the same batch updates it rather than adding another.
    existing = row(conn, "SELECT * FROM reminder WHERE shop_id=? AND sku_id IS ? AND due_on=? AND status='scheduled'",
                   shop_id, sku_id, due_on)
    if existing:
        conn.execute("UPDATE reminder SET reason=?, rupees=? WHERE id=?", (reason, rupees, existing["id"]))
        return {**existing, "reason": reason, "rupees": rupees}
    cur = conn.execute("INSERT INTO reminder (shop_id, sku_id, due_on, reason, rupees) VALUES (?,?,?,?,?)",
                       (shop_id, sku_id, due_on, reason, rupees))
    return {"id": cur.lastrowid, "due_on": due_on, "reason": reason}
