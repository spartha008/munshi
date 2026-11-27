"""draft_order, evaluate_scheme, pool_with_neighbour, send_order.

The approval gate lives here: send_order refuses any order the owner has not approved.
There is no auto-ordering path.
"""
from __future__ import annotations

import math

from datetime import date

from datetime import timedelta

from .. import channels, clock, forecast, ledger, rules
from ..db import iso, parse_dt, row, rows
from ..trace import log, traced


class ApprovalRequired(Exception):
    pass


def udhaar_likelihood(given_on: str | None, override: float | None = None) -> float:
    """Chance a credit comes back this week. Fresh credit usually does; old credit rarely."""
    if override is not None:
        return override
    if not given_on:
        return 0.3
    age = (clock.today() - date.fromisoformat(given_on[:10])).days
    if age <= 7:
        return 0.6
    if age <= 30:
        return 0.4
    if age <= 60:
        return 0.15
    return 0.05


def budget_for(conn, shop_id: str) -> rules.Budget:
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    udhaar = [(u["amount"], udhaar_likelihood(u["given_on"], u["likelihood"])) for u in
              rows(conn, "SELECT amount, given_on, likelihood FROM udhaar WHERE shop_id=? AND status='open'",
                   shop_id)]
    # If the owner never told us the cash in hand, plan against their cap alone.
    cash = shop["cash_on_hand"] if shop["cash_on_hand"] is not None else shop["cash_budget"] + shop["commitments"]
    return rules.Budget(owner_cap=shop["cash_budget"], cash_on_hand=cash,
                        expected_udhaar=rules.expected_udhaar(udhaar), commitments=shop["commitments"])


@traced("evaluate_scheme")
def evaluate_scheme(conn, shop_id: str, scheme: dict, state: ledger.ItemState) -> rules.SchemeVerdict:
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    nb = ledger.neighbour_velocity(conn, shop_id, scheme["sku_id"])
    past = conn.execute("SELECT AVG(sell_through) FROM scheme WHERE shop_id=? AND sku_id=? AND sell_through IS NOT NULL",
                        (shop_id, scheme["sku_id"])).fetchone()[0]
    v = rules.evaluate_scheme(unit_price=state.unit_cost, velocity=state.velocity, buy_n=scheme["buy_n"],
                              free_m=scheme["free_m"], shelf_life_days=state.shelf_life_days,
                              target_cover=shop["target_cover_days"], neighbour_velocity=max(nb, default=0),
                              past_sell_through=past)
    conn.execute("UPDATE scheme SET verdict=?, reason=? WHERE scheme_id=?",
                 (v.verdict, v.reason, scheme["scheme_id"]))
    return v


@traced("pool_with_neighbour")
def pool_with_neighbour(conn, shop_id: str, sku_id: str, buy_n: int, free_m: int) -> dict | None:
    """Propose splitting a scheme lot with one nearby Munshi shop. Shops stay anonymous until both agree."""
    me = row(conn, "SELECT velocity_per_day FROM sales_estimate WHERE shop_id=? AND sku_id=?", shop_id, sku_id)
    nb = ledger.neighbour_velocity(conn, shop_id, sku_id)
    if not me or not nb:
        return None
    v_me, v_nb = me["velocity_per_day"], max(nb)
    lot = buy_n + free_m
    mine = max(1, round(lot * v_me / (v_me + v_nb)))
    return {"neighbour": "a Munshi shop nearby (name shared only if both agree)", "you_take": mine,
            "they_take": lot - mine, "cover_days": round(lot / (v_me + v_nb), 1)}


SELL_THROUGH_WINDOW_DAYS = 14


@traced("learn_scheme_sell_through")
def learn_scheme_sell_through(conn, shop_id: str) -> list[dict]:
    """Learn: two weeks after a scheme lot arrives, record how much of it sold (scheme.sell_through)."""
    now = clock.now()
    learned = []
    for p in rows(conn, """SELECT * FROM purchase WHERE shop_id=? AND status='ok' AND free_qty>0 AND sku_id IS NOT NULL
                           AND received_on<=?""", shop_id, iso(now - timedelta(days=SELL_THROUGH_WINDOW_DAYS))):
        sc = row(conn, """SELECT * FROM scheme WHERE shop_id=? AND sku_id=? AND sell_through IS NULL
                          AND (offered_on IS NULL OR offered_on<=?) ORDER BY scheme_id DESC LIMIT 1""",
                 shop_id, p["sku_id"], p["received_on"])
        if not sc:
            continue
        # What was left of this lot exactly SELL_THROUGH_WINDOW_DAYS after it arrived.
        at = parse_dt(p["received_on"]) + timedelta(days=SELL_THROUGH_WINDOW_DAYS)
        state = ledger.item_state(conn, shop_id, ledger.item_by_sku(conn, p["sku_id"]), at)
        left = sum(b.units for b in state.batches if b.purchase_id == p["id"])
        lot = p["qty"] + p["free_qty"]
        st = max(0.0, min(1.0, 1 - left / lot)) if lot else None
        conn.execute("UPDATE scheme SET sell_through=? WHERE scheme_id=?", (st, sc["scheme_id"]))
        learned.append({"sku_id": p["sku_id"], "sell_through": st})
    return learned


def _note(s: ledger.ItemState) -> str:
    if s.last_check and s.last_check["status"] == "sold_out":
        return "ran out" + ("; demand may be higher" if s.censored else "")
    return ""


@traced("draft_order")
def draft_order(conn, shop_id: str) -> dict:
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    learn_scheme_sell_through(conn, shop_id)
    states = ledger.recompute(conn, shop_id)
    by_sku = {s.sku_id: s for s in states}
    cover = shop["target_cover_days"] or rules.TARGET_COVER_DAYS
    today = clock.today().isoformat()
    schemes = rows(conn, """SELECT * FROM scheme WHERE shop_id=? AND (valid_till IS NULL OR valid_till>=?)
                            ORDER BY scheme_id""", shop_id, today)
    verdicts = {}
    for sc in schemes:
        if sc["sku_id"] in by_sku:
            verdicts[sc["sku_id"]] = (sc, evaluate_scheme(conn, shop_id, sc, by_sku[sc["sku_id"]]))

    budget = budget_for(conn, shop_id)
    cands = []
    festivals = []
    for s in states:
        sc, v = verdicts.get(s.sku_id, (None, None))
        velocity, lo, hi, uplift_note = s.velocity, s.band.low, s.band.high, ""
        up = forecast.uplift_in_window(s.category, clock.today(), cover)
        if up and velocity > 0:
            # A festival inside this order's cover window: stock for the festival week, with its band.
            velocity = s.velocity * (1 + up["uplift_pct"] / 100)
            lo, hi = s.band.low * (1 + up["low_pct"] / 100), s.band.high * (1 + up["high_pct"] / 100)
            uplift_note = (f"{up['festival']} on {up['date']:%d %b}: +{up['uplift_pct']:.0f}% "
                           f"({up['low_pct']:.0f}–{up['high_pct']:.0f}%)")
            festivals.append(up["festival"])
        basis = {"cohort": "shops nearby", "purchases": "bills only"}.get(s.source, "")
        cands.append(rules.Candidate(s.sku_id, s.name, velocity, s.on_hand, s.unit_cost, s.mrp, s.case_qty,
                                     scheme=v, scheme_buy=sc["buy_n"] if sc else 0,
                                     scheme_free=sc["free_m"] if sc else 0, note=_note(s),
                                     band_low=lo, band_high=hi, basis=basis, uplift_note=uplift_note))
    d = rules.draft_order(cands, budget.cap, cover)

    pools = []
    for c, v in d.pool_schemes:
        p = pool_with_neighbour(conn, shop_id, c.sku_id, c.scheme_buy, c.scheme_free)
        if p:
            pools.append({"sku_id": c.sku_id, "name": c.name, "buy_n": c.scheme_buy, "free_m": c.scheme_free,
                          "reason": v.reason, **p})

    conn.execute("UPDATE orders SET status='cancelled' WHERE shop_id=? AND status IN ('proposed','edited')",
                 (shop_id,))
    cur = conn.execute("""INSERT INTO orders (shop_id, distributor_id, proposed_on, for_date, status, budget_cap,
                          budget_used) VALUES (?,?,?,?, 'proposed', ?, ?)""",
                       (shop_id, shop["distributor_id"], iso(clock.now()), shop["salesman_day"], budget.cap,
                        d.total))
    order_id = cur.lastrowid
    for ln in d.lines:
        conn.execute("""INSERT INTO order_line (order_id, sku_id, qty, free_qty, unit_cost, reason, scheme_verdict)
                        VALUES (?,?,?,?,?,?,?)""",
                     (order_id, ln.sku_id, ln.qty, ln.free_qty, ln.unit_cost, ln.reason, ln.scheme_verdict))
    return {
        "order_id": order_id,
        "budget": {"cap": budget.cap, "owner_cap": budget.owner_cap, "cash_on_hand": budget.cash_on_hand,
                   "expected_udhaar": round(budget.expected_udhaar), "commitments": budget.commitments,
                   "available": round(budget.available)},
        "total": round(d.total, 2),
        "festivals": sorted(set(festivals)),
        "lines": [{"sku_id": l.sku_id, "name": l.name, "qty": l.qty, "free_qty": l.free_qty,
                   "cost": round(l.cost, 2), "reason": l.reason, "scheme": l.scheme_verdict} for l in d.lines],
        "skipped": [{"sku_id": s.sku_id, "name": s.name, "why": s.why, "detail": s.detail} for s in d.skipped],
        "refused": [{"sku_id": c.sku_id, "name": c.name, "buy_n": c.scheme_buy, "free_m": c.scheme_free,
                     "cash_locked": v.cash_locked, "cover_days": None if math.isinf(v.cover_days) else
                     round(v.cover_days), "reason": v.reason} for c, v in d.refused_schemes],
        "pools": pools,
    }


OPEN_ORDER_HOURS = 48   # a proposal the owner hasn't answered in two days is stale, not approvable


def open_order(conn, shop_id: str) -> dict | None:
    cutoff = iso(clock.now() - timedelta(hours=OPEN_ORDER_HOURS))
    conn.execute("UPDATE orders SET status='cancelled', summary='expired' WHERE shop_id=? "
                 "AND status IN ('proposed','edited') AND proposed_on < ?", (shop_id, cutoff))
    return row(conn, """SELECT * FROM orders WHERE shop_id=? AND status IN ('proposed','edited')
                        ORDER BY order_id DESC LIMIT 1""", shop_id)


def order_lines(conn, order_id: int) -> list[dict]:
    return rows(conn, """SELECT ol.*, i.canonical_name AS name FROM order_line ol JOIN item i ON i.sku_id=ol.sku_id
                         WHERE order_id=? ORDER BY ol.id""", order_id)


def order_total(conn, order_id: int) -> float:
    return conn.execute("SELECT COALESCE(SUM(qty*unit_cost),0) FROM order_line WHERE order_id=?",
                        (order_id,)).fetchone()[0]


@traced("edit_order")
def edit_order(conn, order_id: int, edits: list[dict]) -> dict:
    """edits: [{"sku_id", "qty"}]; qty 0 removes the line. Refused if it breaks the cash cap."""
    o = row(conn, "SELECT * FROM orders WHERE order_id=?", order_id)
    current = {l["sku_id"]: l for l in order_lines(conn, order_id)}
    for e in edits:  # "add surf 3" adds to what's there; "surf 3" sets it
        if e.get("add") and e["sku_id"] in current:
            e["qty"] = current[e["sku_id"]]["qty"] + e["qty"]
    new_total = order_total(conn, order_id)
    for e in edits:
        item = ledger.item_by_sku(conn, e["sku_id"])
        old = current.get(e["sku_id"])
        new_total += e["qty"] * item["unit_cost"] - (old["qty"] * old["unit_cost"] if old else 0)
    if new_total > o["budget_cap"] + 0.01:
        return {"ok": False, "total": round(new_total), "cap": round(o["budget_cap"])}
    for e in edits:
        item = ledger.item_by_sku(conn, e["sku_id"])
        old = current.get(e["sku_id"])
        if e["qty"] <= 0:
            conn.execute("DELETE FROM order_line WHERE order_id=? AND sku_id=?", (order_id, e["sku_id"]))
        elif old:
            conn.execute("UPDATE order_line SET qty=?, free_qty=0, reason=?, scheme_verdict=NULL WHERE id=?",
                         (e["qty"], "edited by owner", old["id"]))
        else:
            conn.execute("""INSERT INTO order_line (order_id, sku_id, qty, free_qty, unit_cost, reason)
                            VALUES (?,?,?,0,?, 'added by owner')""", (order_id, e["sku_id"], e["qty"],
                                                                     item["unit_cost"]))
    total = order_total(conn, order_id)
    conn.execute("UPDATE orders SET status='edited', budget_used=? WHERE order_id=?", (total, order_id))
    return {"ok": True, "total": round(total), "cap": round(o["budget_cap"])}


@traced("approve_order")
def approve_order(conn, order_id: int, owner_text: str) -> dict:
    """Called only when the owner's own message says OK."""
    conn.execute("UPDATE orders SET status='approved', approved_on=?, approval_text=? WHERE order_id=? "
                 "AND status IN ('proposed','edited')", (iso(clock.now()), owner_text, order_id))
    return row(conn, "SELECT order_id, status, approval_text FROM orders WHERE order_id=?", order_id)


def distributor_message(conn, order_id: int) -> str:
    o = row(conn, "SELECT * FROM orders WHERE order_id=?", order_id)
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", o["shop_id"])
    lines = order_lines(conn, order_id)
    where = f" ({shop['locality']})" if shop.get("locality") else ""
    body = [f"Order from {shop['name'] or 'our shop'}{where} for {o['for_date']}:"]
    for i, l in enumerate(lines, 1):
        free = f" + {l['free_qty']} free (scheme)" if l["free_qty"] else ""
        body.append(f"{i}. {l['name']} x {l['qty']}{free}")
    body.append(f"Expected total: ₹{order_total(conn, order_id):,.0f}. Please confirm by reply.")
    return "\n".join(body)


@traced("send_order")
def send_order(conn, order_id: int) -> dict:
    """Deliver an approved order to the distributor on WhatsApp. If WhatsApp won't allow it (the distributor
    has never messaged this number, or no number is saved), the owner gets a ready-to-forward copy."""
    o = row(conn, "SELECT * FROM orders WHERE order_id=?", order_id)
    if not o or o["status"] != "approved" or not o["approval_text"]:
        raise ApprovalRequired("Orders are sent only after the owner replies OK.")
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", o["shop_id"])
    dist = row(conn, "SELECT * FROM distributor WHERE distributor_id=?", o["distributor_id"]) or {}
    name = dist.get("name") or "your distributor"
    body = distributor_message(conn, order_id)
    channel, status, error, error_code, provider_id = "forward", "handed_to_owner", None, None, None
    if dist.get("phone"):
        try:
            provider_id = channels.get_transport().send_order_to_distributor(dist["phone"], shop["name"] or "", body,
                                                                             channel=shop.get("channel"))
            channel, status = "whatsapp", "accepted"   # 'delivered' only once the provider reports it
        except channels.DeliveryError as e:
            error, error_code = str(e), e.code
            log("send_order.fallback", {"to": name}, {"error": error})
    else:
        error = "no distributor number saved"
    conn.execute("INSERT INTO outbox (shop_id, to_name, to_phone, body, channel, status, error, provider_msg_id, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?)", (o["shop_id"], name, dist.get("phone"), body, channel, status,
                                                           error, provider_id, iso(clock.now())))
    conn.execute("UPDATE orders SET status='sent', sent_on=? WHERE order_id=?", (iso(clock.now()), order_id))
    return {"order_id": order_id, "sent_to": name, "channel": channel, "body": body, "error": error,
            "error_code": error_code, "has_phone": bool(dist.get("phone")),
            "total": round(order_total(conn, order_id))}
