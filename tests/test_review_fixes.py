"""Fixes from the code review: approval gate, undelivered questions, real prices, k>=3 cold start,
payment-SMS privacy, duplicate photos, festivals by name, unpunctuated voice notes, locality keys."""
import threading
from datetime import datetime, timedelta

from src import agent, clock, db, scheduler
from src.db import iso, locality_key
from src.tools import ordering
from src.tools.names import split_note
from src.tools.owner import ask_owner
from src.whatsapp import WAMessage
from src.worker import Worker

from .fixtures import SHOP


def say(conn, text, kind="text"):
    return agent.handle(conn, SHOP, agent.Inbound(kind, text=text))


def test_stale_order_is_not_approvable(conn):
    say(conn, "order")
    clock.set_now(clock.now() + timedelta(hours=49))
    replies = say(conn, "ok")
    assert not any(r.startswith("Sent to") or r.startswith("Order approved") for r in replies)
    assert conn.execute("SELECT status, summary FROM orders").fetchone()[1] == "expired"


def test_bare_yes_approves_only_right_after_the_proposal(conn, transport):
    say(conn, "order")
    say(conn, "settings")                      # something else was said since the proposal
    assert transport.sent == [] and not any("approved" in r.lower() for r in say(conn, "yes"))
    say(conn, "order")
    assert any("Sent to" in r or "Order approved" in r for r in say(conn, "yes"))


def test_done_and_sari_no_longer_send_an_order(conn, transport):
    say(conn, "order")
    say(conn, "done"); say(conn, "sari")
    assert transport.sent == []
    assert "Cancelled" in say(conn, "cancel order")[0]
    assert ordering.open_order(conn, SHOP) is None


def test_undelivered_scheduled_question_goes_back_to_the_queue(conn):
    ask_owner(conn, SHOP, "Is 'A' Good Day Cashew 100g?", "bill_alias", {"printed_name": "A", "guess": "GDC100"})
    conn.execute("UPDATE question SET status='queued', asked_on=NULL")
    conn.execute("UPDATE shop SET last_inbound_at=? WHERE shop_id=?", (iso(clock.now() - timedelta(days=2)), SHOP))
    w = Worker(conn, threading.Lock(), send=lambda to, r: "id", mark_read=lambda m: None)
    clock.set_now(datetime(2026, 11, 26, 10, 5))
    scheduler.tick(w)
    assert conn.execute("SELECT status, asked_on FROM question").fetchone()[0] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM job_run WHERE job='questions' AND shop_id=?", (SHOP,)).fetchone()[0] == 0
    assert not any("yes" in r.lower() and "Saved" in r for r in say(conn, "yes"))  # nothing to answer blindly


def test_order_lines_use_the_price_this_shop_paid(conn):
    conn.execute("INSERT INTO purchase (shop_id, sku_id, printed_name, qty, unit_price, received_on) VALUES (?,?,?,?,?,?)",
                 (SHOP, "MAG70", "MAGGI", 1, 13.20, iso(clock.now() - timedelta(days=1))))
    from src import ledger
    assert ledger.item_state(conn, SHOP, ledger.item_by_sku(conn, "MAG70")).unit_cost == 13.20
    d = ordering.draft_order(conn, SHOP)
    priced = {l["sku_id"]: l["cost"] / l["qty"] for l in d["lines"]}
    if "MAG70" in priced:
        assert abs(priced["MAG70"] - 13.20) < 0.01   # not the catalogue's 12.50
    else:  # the thinner margin pushed it below the cash line; the skip reason is priced the same way
        detail = next(s["detail"] for s in d["skipped"] if s["sku_id"] == "MAG70")
        assert "₹634" in detail                        # 48 packs x 13.20


def test_cold_start_needs_three_neighbours(conn):
    conn.execute("DELETE FROM purchase WHERE sku_id='SURF1'"); conn.execute("DELETE FROM shelf_check WHERE sku_id='SURF1'")
    conn.execute("INSERT INTO purchase (shop_id, sku_id, printed_name, qty, unit_price, received_on) VALUES (?,?,?,?,?,?)",
                 (SHOP, "SURF1", "SURF", 3, 120, iso(clock.now())))
    from src import ledger
    s = ledger.item_state(conn, SHOP, ledger.item_by_sku(conn, "SURF1"))
    assert s.source == "purchases"     # two neighbours stock it: not enough for a locality estimate
    conn.execute("INSERT INTO shop (shop_id, name, locality, locality_key, last_inbound_at) VALUES ('919000000004','Shop 4','Kuvempunagar',?,?)",
                 (locality_key("Kuvempunagar"), iso(clock.now())))
    conn.execute("INSERT INTO sales_estimate VALUES ('919000000004','SURF1',0.3,0.25,0.35,'high',NULL,NULL,0,?)", (iso(clock.now()),))
    s = ledger.item_state(conn, SHOP, ledger.item_by_sku(conn, "SURF1"))
    assert s.source == "cohort"


def test_payment_sms_is_logged_without_the_payer(conn):
    say(conn, "Rs.250.00 credited to your a/c XX1234 by UPI from RAVI KUMAR ref 6123")
    body = conn.execute("SELECT body FROM message WHERE shop_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (SHOP,)).fetchone()[0]
    assert "RAVI" not in body and "250" in body


def test_same_photo_twice_is_read_once(conn):
    first = agent.handle(conn, SHOP, agent.Inbound("image", image=b"same-bytes", media_type="image/jpeg"))
    assert first[0].startswith("Got it")
    again = agent.handle(conn, SHOP, agent.Inbound("image", image=b"same-bytes", media_type="image/jpeg"))
    assert "already read this bill" in again[0]
    assert conn.execute("SELECT COUNT(*) FROM bill").fetchone()[0] == 1


def test_return_reminders_exist_before_any_order(conn):
    agent.handle(conn, SHOP, agent.Inbound("image", image=b"x", media_type="image/jpeg"))
    assert conn.execute("SELECT COUNT(*) FROM reminder").fetchone()[0] >= 1


def test_festival_by_name_and_new_year(conn):
    assert say(conn, "sankranti")[0].startswith("Sankranti is in")
    assert say(conn, "new year")[0].startswith("New Year is in")
    assert "Christmas" in say(conn, "festival")[0]


def test_unpunctuated_transcript_splits_on_status_words():
    assert split_note("maggi khatam parle g do bache surf kam") == ["maggi khatam", "parle g do bache", "surf kam"]
    assert split_note("Maggi khatam, Parle G do bache.") == ["Maggi khatam", "Parle G do bache"]


def test_locality_key_matches_spelling_variants():
    assert locality_key("Kuvempunagar, Mysuru") == locality_key("kuvempunagar") == "kuvempunagar"
    assert locality_key("N.R. Mohalla, Mysore") == "nrmohalla"
    assert locality_key(None) is None
