"""update_stock(shop, events) -> Ledger;  score_expiry(shop) -> ExpiryRisk[]"""
from __future__ import annotations

from datetime import timedelta

from .. import clock, ledger
from ..db import iso, row, rows
from ..trace import traced


@traced("update_stock")
def update_stock(conn, shop_id: str, events: list[dict]) -> dict:
    """events: {"type": "purchase", ...purchase columns} or {"type": "shelf_check", sku_id, count, status, raw}."""
    now = clock.now()
    n_buy = n_check = 0
    for e in events:
        if e["type"] == "purchase":
            conn.execute("""INSERT INTO purchase (shop_id, bill_id, sku_id, printed_name, qty, free_qty, unit_price,
                            batch, expiry, scheme_terms, received_on, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (shop_id, e.get("bill_id"), e.get("sku_id"), e.get("printed_name"), e["qty"],
                          e.get("free_qty", 0), e.get("unit_price"), e.get("batch"), e.get("expiry"),
                          e.get("scheme_terms"), e.get("received_on") or iso(now),
                          "ok" if e.get("sku_id") else "pending"))
            n_buy += 1
        elif e["type"] == "shelf_check":
            conn.execute("""INSERT INTO shelf_check (shop_id, sku_id, count, status, source, raw, checked_on)
                            VALUES (?,?,?,?,?,?,?)""",
                         (shop_id, e["sku_id"], e.get("count"), e.get("status", "count"), e.get("source", "voice"),
                          e.get("raw"), e.get("checked_on") or iso(now)))
            n_check += 1
    states = ledger.recompute(conn, shop_id, now)
    return {"purchases": n_buy, "shelf_checks": n_check, "items": len(states)}


def _action(conn, shop: dict, s: ledger.ItemState, b: ledger.Batch, fast_movers: list[str]) -> dict:
    dist = row(conn, "SELECT * FROM distributor WHERE distributor_id=?", shop["distributor_id"]) or {}
    return_by = b.expiry - timedelta(days=dist.get("return_days_before_expiry", 15))
    n = max(1, round(b.at_risk_units))
    today = clock.today()
    if today <= return_by:
        return {"action": "return", "return_by": return_by.isoformat(),
                "text": f"return {n} to {dist.get('name', 'the distributor')} by {return_by:%d %b}"
                        + (" (today)" if return_by == today else "")}
    if (b.days_to_expiry or 0) <= 7:
        return {"action": "counter", "return_by": None,
                "text": f"put {n} on the counter and sell at a small discount this week"}
    buddy = next((f for f in fast_movers if f != s.name), None)
    return {"action": "bundle", "return_by": None,
            "text": f"bundle {n} with {buddy}" if buddy else f"bundle {n} with a fast mover"}


@traced("score_expiry")
def score_expiry(conn, shop_id: str, horizon_days: int = 60) -> list[dict]:
    """Batches that will expire before they sell, with rupees at risk and an action."""
    shop = row(conn, "SELECT * FROM shop WHERE shop_id=?", shop_id)
    states = ledger.all_states(conn, shop_id)
    fast = [s.name for s in sorted(states, key=lambda s: s.velocity, reverse=True)[:3]]
    out = []
    for s in states:
        for b in s.batches:
            if b.expiry is None or b.at_risk_units < 0.5 or (b.days_to_expiry or 0) > horizon_days:
                continue
            out.append({"sku_id": s.sku_id, "name": s.name, "units": round(b.units, 1),
                        "at_risk_units": round(b.at_risk_units, 1), "expiry": b.expiry.isoformat(),
                        "days_to_expiry": b.days_to_expiry, "rupees_at_risk": round(b.rupees_at_risk),
                        "velocity": round(s.velocity, 2), **_action(conn, shop, s, b, fast)})
    out.sort(key=lambda r: (-r["rupees_at_risk"], r["days_to_expiry"]))
    return out


def recent_bill_lines_expiring(conn, bill_id: int, within_days: int = 20) -> list[dict]:
    today = clock.today()
    out = []
    for p in rows(conn, """SELECT p.*, i.canonical_name, i.pack_size FROM purchase p
                           LEFT JOIN item i ON i.sku_id = p.sku_id WHERE p.bill_id=?""", bill_id):
        exp = ledger.parse_expiry(p["expiry"])
        if exp and (exp - today).days <= within_days:
            out.append({**p, "days": (exp - today).days})
    return out
