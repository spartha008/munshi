"""README promises beyond the core loop: festival forecast in orders, scheme sell-through learning,
confidence bands, the three KPIs, and the anonymised locality demand report."""
from datetime import datetime, timedelta

from src import agent, clock, db, insights, rules
from src.db import iso
from src.tools import ordering
from src.tools.names import normalise_item

from .fixtures import SHOP, build_shop


def say(conn, text):
    return agent.handle(conn, SHOP, agent.Inbound("text", text=text))


def test_past_scheme_that_did_not_sell_turns_take_into_skip():
    assert rules.evaluate_scheme(4.4, 9, 24, 2, 180).verdict == rules.TAKE
    v = rules.evaluate_scheme(4.4, 9, 24, 2, 180, past_sell_through=0.5)
    assert v.verdict == rules.SKIP and "50%" in v.reason


def test_sell_through_is_learned_two_weeks_after_a_scheme_lot(conn):
    received = clock.now() - timedelta(days=15)
    conn.execute("""INSERT INTO purchase (shop_id, sku_id, printed_name, qty, free_qty, unit_price, received_on)
                    VALUES (?,?,?,?,?,?,?)""", (SHOP, "BRU50", "BRU 50G", 24, 2, 105, iso(received)))
    # Shelf counts consistent with a slow seller: the 26 arrive and barely move.
    conn.execute("DELETE FROM shelf_check WHERE shop_id=? AND sku_id='BRU50'", (SHOP,))
    for days_ago, count in [(21, 3), (14, 28), (7, 27), (0, 26)]:
        conn.execute("INSERT INTO shelf_check (shop_id, sku_id, count, status, checked_on) VALUES (?,?,?,?,?)",
                     (SHOP, "BRU50", count, "count", iso(clock.now() - timedelta(days=days_ago, minutes=1))))
    conn.execute("INSERT INTO scheme (shop_id, sku_id, buy_n, free_m, offered_on) VALUES (?,?,?,?,?)",
                 (SHOP, "BRU50", 24, 2, iso(received - timedelta(days=1))))
    learned = ordering.learn_scheme_sell_through(conn, SHOP)
    st = next(l["sell_through"] for l in learned if l["sku_id"] == "BRU50")
    assert st < 0.7  # coffee sells ~1 a fortnight, so most of the 26 is still there
    assert "only" in say(conn, "scheme bru 24+2")[0] or "SKIP" in say(conn, "scheme bru 24+2")[0]


def test_order_lines_carry_their_confidence_band(conn):
    say(conn, "order")
    lines = ordering.order_lines(conn, ordering.open_order(conn, SHOP)["order_id"])
    assert all("–" in l["reason"] and "/day (" in l["reason"] for l in lines)


def test_festival_in_the_order_window_raises_the_order(fake_llm):
    before_xmas = datetime(2026, 12, 21, 9, 0)
    clock.set_now(before_xmas)
    c = db.connect(":memory:")
    build_shop(c, before_xmas)
    try:
        d = ordering.draft_order(c, SHOP)
        assert d["festivals"] == ["Christmas"]
        dm = next(l for l in d["lines"] if l["sku_id"] == "DM13")
        assert "Christmas on 25 Dec: +60%" in dm["reason"]
    finally:
        clock.set_now(None)


def test_kpis_count_expiry_caught_and_minutes_to_ok(conn):
    agent.handle(conn, SHOP, agent.Inbound("image", image=b"x", media_type="image/jpeg"))
    say(conn, "order")
    clock.set_now(clock.now() + timedelta(minutes=2))
    say(conn, "ok")
    k = insights.shop_kpis(conn, SHOP)
    assert k["expiring_stock_caught_rupees"] >= 400 and k["expiry_actions"] >= 1
    assert k["orders_approved"] == 1 and k["median_minutes_to_ok"] == 2.0


def test_demand_report_needs_three_shops_and_never_names_one(conn):
    say(conn, "order")  # recomputes this shop's SalesEstimate
    rows = insights.demand_report(conn)
    skus = {r["sku_id"] for r in rows}
    assert "SURF1" in skus          # this shop + two neighbours
    assert "PG5" not in skus        # only two shops -> withheld
    assert all("shop_id" not in r for r in rows)
    assert all(r["shops"] >= 3 for r in rows)


def test_ambiguous_name_guesses_the_everyday_pack_for_a_new_shop(conn):
    conn.execute("INSERT INTO shop (shop_id, name) VALUES ('919222', 'New')")
    r = normalise_item(conn, "919222", "Maggi khatam")
    assert r["sku_id"] is None and r["guess"] == "MAG70"
