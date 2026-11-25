"""The agent end to end against a shop with three weeks of history (Claude's bill read stubbed)."""
import pytest

from src import agent
from src.tools import ordering
from src.tools.names import normalise_item

from .fixtures import DIST_PHONE, SHOP


def say(conn, kind, text="", **kw):
    return agent.handle(conn, SHOP, agent.Inbound(kind, text=text, **kw))


def bill(conn):
    return say(conn, "image", image=b"jpeg", media_type="image/jpeg")


def order_head(replies):
    return next(r for r in replies if "order ready" in r)


def test_bill_photo_reads_items_flags_expiry_and_asks_once(conn):
    replies = bill(conn)
    assert replies[0].startswith("Got it: 14 items from Sharma Distributors")
    assert "3 expire within 20 days" in replies[0]
    assert "may expire on the shelf" in replies[1]
    q = next(r for r in replies if "GD CSHW 100G" in r)
    assert "Good Day Cashew" in q and q.buttons == agent.YES_NO
    assert conn.execute("SELECT COUNT(*) FROM purchase WHERE status='pending'").fetchone()[0] == 1


def test_yes_learns_alias_and_never_asks_again(conn):
    bill(conn)
    assert "Saved" in say(conn, "text", "yes")[0]
    assert conn.execute("SELECT COUNT(*) FROM purchase WHERE status='pending'").fetchone()[0] == 0
    r = normalise_item(conn, SHOP, "GD CSHW 100G", item_only=True)
    assert r["sku_id"] == "GDC100" and r["via"] == "alias"


def test_no_adds_the_line_as_a_new_item(conn):
    bill(conn)
    assert "new item" in say(conn, "text", "no")[0]
    item = conn.execute("SELECT * FROM item WHERE shop_id=?", (SHOP,)).fetchone()
    assert item["canonical_name"] == "Gd Cshw 100G" and item["unit_cost"] == 26


def test_unknown_bill_line_becomes_a_shop_item_with_shelf_life_from_expiry(conn, fake_llm):
    fake_llm["bill"]["items"].append({"name": "XYZ PAPAD 200G", "qty": 10, "unit": "pc", "unit_price": 30, "mrp": 40,
                                      "line_total": 300, "batch": None, "expiry": "2027-02-28", "scheme": None,
                                      "confidence": 0.9})
    fake_llm["bill"]["bill_total"] += 300
    replies = bill(conn)
    assert "1 new item added" in replies[0]
    it = conn.execute("SELECT * FROM item WHERE shop_id=? AND keywords='xyz papad 200g'", (SHOP,)).fetchone()
    assert it["mrp"] == 40 and it["shelf_life_days"] > 90


def test_voice_shelf_check(conn):
    bill(conn)
    replies = say(conn, "voice", "Maggi khatam, Parle-G do bache")
    assert "Maggi 2-Minute Noodles 70g: sold out" in replies[0]
    assert "Parle-G ₹5 pack: 2 left" in replies[0]


def test_order_is_capped_refuses_namkeen_scheme_and_skips_slow_movers(conn):
    bill(conn)
    say(conn, "text", "yes")
    say(conn, "voice", "Maggi khatam, Parle-G do bache")
    replies = agent.morning(conn, SHOP)
    head = order_head(replies)
    assert head.startswith("Thursday order ready")
    assert "2 slow movers skipped" in head
    assert "1 scheme refused" in head and "Haldiram" in head
    assert head.buttons == agent.ORDER_BUTTONS
    o = ordering.open_order(conn, SHOP)
    assert o["budget_used"] <= o["budget_cap"] <= 5000
    skus = {l["sku_id"] for l in ordering.order_lines(conn, o["order_id"])}
    assert {"MAG70", "PG5"} <= skus
    assert not skus & {"HAL200", "HOR500", "KJ200"}


def test_new_shop_without_data_gets_no_invented_order(conn):
    conn.execute("INSERT INTO shop (shop_id, name, cash_budget) VALUES ('919111', 'New', 3000)")
    replies = agent.handle(conn, "919111", agent.Inbound("text", text="order"))
    assert "I don't know your stock yet" in replies[0]
    assert ordering.order_lines(conn, ordering.open_order(conn, "919111")["order_id"]) == []


def test_edit_over_cap_is_refused(conn):
    say(conn, "text", "order")
    before = ordering.open_order(conn, SHOP)["budget_used"]
    assert "over your" in say(conn, "text", "maggi 2000")[0]
    assert ordering.open_order(conn, SHOP)["budget_used"] == before


def test_nothing_is_sent_without_ok_then_ok_messages_the_distributor(conn, transport):
    say(conn, "text", "order")
    o = ordering.open_order(conn, SHOP)
    assert transport.sent == []
    with pytest.raises(ordering.ApprovalRequired):
        ordering.send_order(conn, o["order_id"])
    replies = say(conn, "text", "ok")  # the OK button's id
    assert replies[0].startswith("Sent to Sharma Distributors on WhatsApp")
    assert transport.sent[0][0] == DIST_PHONE and "Order from Sri Lakshmi Stores" in transport.sent[0][1]
    assert conn.execute("SELECT status FROM orders WHERE order_id=?", (o["order_id"],)).fetchone()[0] == "sent"


def test_if_whatsapp_refuses_the_owner_gets_a_forwardable_order(conn, transport):
    transport.fail_code = 131047
    say(conn, "text", "order")
    replies = say(conn, "text", "OK")
    assert "here it is to forward" in replies[0]
    assert replies[1].startswith("Order from Sri Lakshmi Stores")
    out = conn.execute("SELECT channel, status FROM outbox").fetchone()
    assert tuple(out) == ("forward", "handed_to_owner")


def test_ok_after_bill_schedules_return_reminders(conn):
    bill(conn)
    say(conn, "text", "order")
    replies = say(conn, "text", "ok")
    assert any("return 4 Real Mixed Fruit Juice" in r for r in replies)
    assert conn.execute("SELECT COUNT(*) FROM reminder").fetchone()[0] >= 1


def test_scheme_command_gives_the_three_verdicts(conn):
    assert "SKIP" in say(conn, "text", "scheme namkeen 12+1")[0]
    assert "SHARE IT" in say(conn, "text", "scheme surf 10+1")[0]
    assert "TAKE" in say(conn, "text", "scheme parle 24+2")[0]


def test_upi_sms_keeps_amount_only(conn):
    replies = say(conn, "text", "Rs.250.00 credited to your a/c XX1234 by UPI ref 6123 from RAVI KUMAR")
    assert "₹250" in replies[0]
    assert conn.execute("SELECT raw FROM upi_sale").fetchone()[0] is None


def test_one_question_at_a_time_and_three_a_day(conn):
    for p in ["bru coffe khatam", "colgte kam", "lifboy khatam", "horlick khatam", "kisan jm khatam"]:
        say(conn, "voice", p)
    q = conn.execute("SELECT status, COUNT(*) FROM question GROUP BY status").fetchall()
    counts = {r[0]: r[1] for r in q}
    assert counts.get("open", 0) == 1
    for _ in range(5):
        say(conn, "text", "yes")
    asked = conn.execute("SELECT COUNT(*) FROM question WHERE asked_on IS NOT NULL").fetchone()[0]
    assert asked <= 3


def test_udhaar_commands_feed_the_budget(conn):
    before = ordering.budget_for(conn, SHOP).expected_udhaar
    assert "Suresh now owes ₹700" in say(conn, "text", "udhaar Suresh 700")[0]
    assert ordering.budget_for(conn, SHOP).expected_udhaar > before
    assert "Still owes ₹200" in say(conn, "text", "Suresh paid 500")[0]
    assert "Udhaar outstanding" in say(conn, "text", "udhaar")[0]


def test_settings_commands(conn):
    assert "Friday" in say(conn, "text", "salesman friday")[0]
    assert "₹6,000" in say(conn, "text", "budget 6000")[0]
    assert "Saved distributor: Gupta Agencies, 919876543210" in say(conn, "text", "distributor Gupta Agencies 9876543210")[0]
    s = say(conn, "text", "settings")[0]
    assert "Friday" in s and "Gupta Agencies" in s


def test_unreadable_bill_gets_a_helpful_reply(conn, fake_llm):
    fake_llm["bill"] = {"distributor": None, "bill_date": None, "bill_total": None, "items": []}
    assert "couldn't find any items" in bill(conn)[0]


def test_add_adds_to_an_existing_line(conn):
    conn.execute("UPDATE shop SET cash_budget=20000, cash_on_hand=20000 WHERE shop_id=?", (SHOP,))  # room under the cap
    say(conn, "text", "order")
    lines = {l["sku_id"]: l["qty"] for l in ordering.order_lines(conn, ordering.open_order(conn, SHOP)["order_id"])}
    say(conn, "text", "add surf 3")
    after = {l["sku_id"]: l["qty"] for l in ordering.order_lines(conn, ordering.open_order(conn, SHOP)["order_id"])}
    assert after["SURF1"] == lines["SURF1"] + 3


def test_money_change_keeps_owner_edits(conn):
    say(conn, "text", "order")
    say(conn, "text", "maggi 36")
    r = say(conn, "text", "cash 3000")[0]
    assert "edited order" in r
    lines = {l["sku_id"]: l["qty"] for l in ordering.order_lines(conn, ordering.open_order(conn, SHOP)["order_id"])}
    assert lines["MAG70"] == 36


def test_out_of_stock_wording(conn):
    assert "Horlicks 500g: 0 left, out of stock" in say(conn, "text", "stock")[0]


def test_non_window_delivery_error_is_not_blamed_on_24h_rule(conn, transport):
    transport.fail_code = 131026
    say(conn, "text", "order")
    r = say(conn, "text", "ok")[0]
    assert "didn't deliver it just now" in r and "24 hours" not in r
