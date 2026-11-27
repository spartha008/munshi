"""Regressions from the first live day: ambiguous names ask instead of inventing items, forwarded
payment SMS are not shelf checks, numbers survive splitting, bills with discount/GST reconcile,
and the owner can merge a bill-made item into the real SKU."""
from src import agent, llm
from src.tools.bill import reconcile
from src.tools.names import split_note

from .fixtures import SHOP


def say(conn, text, kind="text"):
    return agent.handle(conn, SHOP, agent.Inbound(kind, text=text))


def test_split_keeps_amounts_and_decimals_whole():
    assert split_note("₹2,205.00 paid to STAR AGENCY, Maggi khatam. Parle G do bache") == \
        ["₹2,205.00 paid to STAR AGENCY", "Maggi khatam", "Parle G do bache"]


def test_reconcile_accepts_discount_and_tax_lines():
    bill = {"bill_total": 2205.0, "discount": 68.0, "tax": 105.0, "items": [{"line_total": 2170.0}]}
    assert reconcile(bill) == (True, 2170.0)
    assert reconcile({"bill_total": 2205.0, "discount": None, "tax": None, "items": [{"line_total": 2170.0}]})[0] is False


def test_outgoing_payment_sms_is_not_a_sale_or_a_shelf_check(conn):
    text = "UPI Payment Successful\n₹2,205.00 paid to STAR AGENCY.\nTransaction ID: DEMO123\nStatus: Verified"
    replies = say(conn, text)
    assert "not a sale" in replies[0] and "₹2,205" in replies[0] and "Star Agency" in replies[0]
    assert conn.execute("SELECT COUNT(*) FROM upi_sale").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trace WHERE tool='normalise_item'").fetchone()[0] == 0


def test_incoming_payment_sms_still_counts(conn):
    assert "UPI payment noted" in say(conn, "Rs.250.00 credited to your a/c by UPI ref 6123")[0]


def test_low_confidence_claude_match_asks_about_its_candidate(conn, monkeypatch):
    monkeypatch.setattr(llm, "normalise", lambda phrase, aliases, cat: {
        "sku_id": None, "sku_name": None, "qty": None, "action": "sold_out", "candidate_sku_id": "MAG70",
        "confidence": 0.5, "question": "Kya aap Maggi 70g ki baat kar rahe hain?"})
    conn.execute("DELETE FROM alias WHERE shop_id=? AND phrase='maggi'", (SHOP,))
    replies = say(conn, "Maggi khatam", "voice")
    q = next(r for r in replies if r.buttons == agent.YES_NO)
    assert "Maggi 2-Minute Noodles 70g" in q and "sold out" in q and "Kya" not in q
    assert say(conn, "yes")[0].startswith("Noted:")
    assert conn.execute("SELECT sku_id FROM alias WHERE shop_id=? AND phrase='maggi'", (SHOP,)).fetchone()[0] == "MAG70"


def test_bill_line_with_a_candidate_is_asked_not_invented(conn, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "normalise", lambda phrase, aliases, cat: {
        "sku_id": None, "sku_name": None, "qty": None, "action": "unknown", "candidate_sku_id": "MAG70",
        "confidence": 0.5, "question": "?"})
    fake_llm["bill"]["items"] = [{"name": "Maggi Noodles (Pack)", "qty": 10, "unit": None, "unit_price": 14.0,
                                  "mrp": None, "line_total": 140.0, "batch": None, "expiry": None, "scheme": None,
                                  "confidence": 0.9}]
    fake_llm["bill"]["bill_total"] = 140.0
    replies = agent.handle(conn, SHOP, agent.Inbound("image", image=b"x", media_type="image/jpeg"))
    assert any("Maggi Noodles (Pack)" in r and "Maggi 2-Minute Noodles 70g" in r for r in replies)
    assert conn.execute("SELECT COUNT(*) FROM item WHERE shop_id=?", (SHOP,)).fetchone()[0] == 0


def test_owner_can_merge_a_bill_made_item_into_the_real_sku(conn):
    from src.tools.names import new_item_from_bill
    learned = new_item_from_bill(conn, SHOP, {"name": "Maggi Noodles (Pack)", "unit_price": 14.0}, "2026-11-01")
    conn.execute("INSERT INTO purchase (shop_id, sku_id, printed_name, qty, unit_price, received_on) VALUES (?,?,?,?,?,?)",
                 (SHOP, learned, "Maggi Noodles (Pack)", 10, 14.0, "2026-11-01T11:00"))
    replies = say(conn, "Maggi Noodles (Pack) is Maggi 70g")
    assert "Saved" in replies[0] and "Maggi 2-Minute Noodles 70g" in replies[0]
    assert conn.execute("SELECT COUNT(*) FROM item WHERE sku_id=?", (learned,)).fetchone()[0] == 0
    assert conn.execute("SELECT sku_id FROM purchase WHERE printed_name='Maggi Noodles (Pack)'").fetchone()[0] == "MAG70"
