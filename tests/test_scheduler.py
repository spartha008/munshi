"""Scheduled work: salesman-morning order, reminders, queued questions; WhatsApp 24-hour window."""
import threading
from datetime import datetime, timedelta

from src import clock, scheduler
from src.db import iso
from src.tools.owner import ask_owner
from src.worker import Worker

from .fixtures import SHOP

THURSDAY_7AM = datetime(2026, 11, 26, 7, 5)


def make_worker(conn):
    sent = []
    w = Worker(conn, threading.Lock(), send=lambda to, r: sent.append((to, str(r))) or "wamid",
               mark_read=lambda mid: None)
    return w, sent


def test_morning_order_is_proposed_once_on_salesman_day(conn):
    w, sent = make_worker(conn)
    clock.set_now(THURSDAY_7AM)
    ran = scheduler.tick(w)
    assert (SHOP, "morning_order") in ran
    assert any("Thursday order ready" in s for _, s in sent)
    n = len(sent)
    scheduler.tick(w)
    assert len(sent) == n  # never twice in a day


def test_not_on_other_days_or_before_7(conn):
    w, sent = make_worker(conn)
    clock.set_now(datetime(2026, 11, 26, 6, 30))
    assert (SHOP, "morning_order") not in scheduler.tick(w)
    clock.set_now(datetime(2026, 11, 27, 9, 0))  # Friday
    assert (SHOP, "morning_order") not in scheduler.tick(w)


def test_outside_24h_window_nothing_is_sent_without_a_template(conn, monkeypatch):
    monkeypatch.delenv("WHATSAPP_NUDGE_TEMPLATE", raising=False)
    conn.execute("UPDATE shop SET last_inbound_at=? WHERE shop_id=?", (iso(THURSDAY_7AM - timedelta(days=2)), SHOP))
    w, sent = make_worker(conn)
    clock.set_now(THURSDAY_7AM)
    scheduler.tick(w)
    assert sent == []
    # the undelivered proposal is cancelled, so a later "ok" cannot send an order the owner never saw
    o = conn.execute("SELECT status, summary FROM orders ORDER BY order_id DESC LIMIT 1").fetchone()
    assert tuple(o) == ("cancelled", "not delivered")


def test_due_reminders_are_sent_and_marked(conn):
    conn.execute("INSERT INTO reminder (shop_id, sku_id, due_on, reason) VALUES (?,?,?,?)",
                 (SHOP, "RJ1L", "2026-11-26", "return 4 Real Mixed Fruit Juice to Sharma Distributors"))
    w, sent = make_worker(conn)
    clock.set_now(datetime(2026, 11, 26, 8, 15))
    scheduler.tick(w)
    assert any("return 4 Real Mixed Fruit Juice" in s for _, s in sent)
    assert conn.execute("SELECT status FROM reminder").fetchone()[0] == "sent"


def test_queued_question_is_asked_next_day(conn):
    clock.set_now(datetime(2026, 11, 25, 10, 0))
    ask_owner(conn, SHOP, "Is 'A' Good Day Cashew 100g?", "bill_alias", {"printed_name": "A", "guess": "GDC100"})
    ask_owner(conn, SHOP, "Is 'B' Bru Instant Coffee 50g?", "bill_alias", {"printed_name": "B", "guess": "BRU50"})
    conn.execute("UPDATE question SET status='answered' WHERE status='open'")
    conn.execute("UPDATE shop SET last_inbound_at='2026-11-26T09:00' WHERE shop_id=?", (SHOP,))
    w, sent = make_worker(conn)
    clock.set_now(datetime(2026, 11, 26, 10, 5))
    scheduler.tick(w)
    assert any("'B' Bru Instant Coffee" in s for _, s in sent)
