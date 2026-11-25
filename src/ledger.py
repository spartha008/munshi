"""Turns raw events (bill lines, shelf checks) into the per-item picture the rules need."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import clock, rules
from .db import iso, parse_dt, row, rows

WINDOW_DAYS = 28


@dataclass
class Batch:
    purchase_id: int
    received_on: datetime
    expiry: date | None
    units: float            # still on the shelf
    unit_cost: float
    days_to_expiry: float | None = None
    at_risk_units: float = 0.0

    @property
    def rupees_at_risk(self) -> float:
        return self.at_risk_units * self.unit_cost


@dataclass
class ItemState:
    sku_id: str
    name: str
    category: str
    shelf_life_days: int
    unit_cost: float
    mrp: float
    case_qty: int
    velocity: float
    band: rules.Band
    observed_days: float
    censored: bool
    on_hand: float
    last_check: dict | None
    batches: list[Batch] = field(default_factory=list)
    source: str = "own"     # own | cohort | purchases | none

    @property
    def cover_days(self) -> float:
        return rules.days_of_cover(self.on_hand, self.velocity)

    @property
    def rupees_at_risk(self) -> float:
        return sum(b.rupees_at_risk for b in self.batches)


def parse_expiry(s: str | None) -> date | None:
    """'YYYY-MM-DD' or 'YYYY-MM' (printed MM/YY: treat as the last day of that month)."""
    if not s:
        return None
    try:
        if len(s) == 7:
            y, m = int(s[:4]), int(s[5:7])
            nxt = date(y + (m == 12), m % 12 + 1, 1)
            return nxt - timedelta(days=1)
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _days(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400


def neighbour_velocity(conn, shop_id: str, sku_id: str) -> list[float]:
    """Velocities of this SKU at other shops in the same locality (anonymised aggregate)."""
    return [r["velocity_per_day"] for r in rows(conn, """
        SELECT se.velocity_per_day FROM sales_estimate se
        JOIN shop s ON s.shop_id = se.shop_id
        WHERE se.sku_id = ? AND se.shop_id != ?
          AND s.locality = (SELECT locality FROM shop WHERE shop_id = ?)
          AND se.velocity_per_day IS NOT NULL""", sku_id, shop_id, shop_id)]


def item_state(conn, shop_id: str, item: dict, now: datetime | None = None) -> ItemState:
    now = now or clock.now()
    sku = item["sku_id"]
    checks = rows(conn, """SELECT * FROM shelf_check WHERE shop_id=? AND sku_id=? AND checked_on<=?
                           ORDER BY checked_on""", shop_id, sku, iso(now))
    buys = rows(conn, """SELECT * FROM purchase WHERE shop_id=? AND sku_id=? AND status='ok'
                         AND received_on<=? ORDER BY received_on""", shop_id, sku, iso(now))
    for b in buys:
        b["_t"] = parse_dt(b["received_on"])
        b["_units"] = (b["qty"] or 0) + (b["free_qty"] or 0)

    counted = [c for c in checks if c["count"] is not None]
    for c in checks:
        c["_t"] = parse_dt(c["checked_on"])

    # --- velocity -----------------------------------------------------------
    start = now - timedelta(days=WINDOW_DAYS)
    in_window = [c for c in counted if c["_t"] >= start]
    intervals = []
    for a, b in zip(in_window, in_window[1:]):
        units_in = sum(p["_units"] for p in buys if a["_t"] < p["_t"] <= b["_t"])
        intervals.append(rules.CountInterval(_days(a["_t"], b["_t"]), a["count"], b["count"], units_in))
    vel = rules.velocity_from_intervals(intervals)

    source = "own"
    if vel and vel.observed_days >= 3:
        velocity, observed, censored = vel.per_day, vel.observed_days, vel.censored
        band = rules.confidence_band(velocity, observed)
    else:
        nb = neighbour_velocity(conn, shop_id, sku)
        recent_in = sum(p["_units"] for p in buys if p["_t"] >= start)
        if nb:
            nb.sort()
            velocity = nb[len(nb) // 2]
            band = rules.Band(min(nb) * 0.8, max(nb) * 1.2, "cohort")
            source = "cohort"
        elif recent_in:
            velocity = recent_in / WINDOW_DAYS
            band = rules.confidence_band(velocity, 0)
            source = "purchases"
        else:
            velocity, band, source = 0.0, rules.Band(0, 0, "none"), "none"
        observed, censored = 0.0, False

    # --- on hand ------------------------------------------------------------
    last_count = counted[-1] if counted else None
    if last_count:
        since = last_count["_t"]
        on_hand = last_count["count"] + sum(p["_units"] for p in buys if p["_t"] > since)
        on_hand -= velocity * _days(since, now)
    else:
        on_hand = sum(p["_units"] for p in buys)
        if buys:
            on_hand -= velocity * _days(buys[0]["_t"], now)
    last_check = checks[-1] if checks else None
    if last_check and last_check["status"] == "running_low" and (
            not last_count or last_check["_t"] >= last_count["_t"]):
        on_hand = min(on_hand, max(1.0, velocity))  # "kam hai": about a day left
    on_hand = max(0.0, on_hand)

    # --- batches: what is left is the newest stock (oldest sells first) -----
    batches: list[Batch] = []
    left = on_hand
    for p in sorted(buys, key=lambda p: p["_t"], reverse=True):
        if left <= 0.0001:
            break
        units = min(p["_units"], left)
        batches.append(Batch(p["id"], p["_t"], parse_expiry(p["expiry"]), units,
                             p["unit_price"] or item["unit_cost"]))
        left -= units
    if left > 0.0001:  # opening stock from before the first bill we saw
        batches.append(Batch(0, start, None, left, item["unit_cost"]))
    batches.sort(key=lambda b: b.received_on)

    ahead = 0.0
    for b in batches:
        if b.expiry is not None:
            b.days_to_expiry = (b.expiry - now.date()).days
            can_sell = max(0.0, velocity * max(0, b.days_to_expiry) - ahead)
            b.at_risk_units = max(0.0, b.units - can_sell)
        ahead += b.units

    return ItemState(sku, item["canonical_name"], item["category"], item["shelf_life_days"],
                     item["unit_cost"], item["mrp"], item["case_qty"], velocity, band, observed,
                     censored, on_hand, last_check, batches, source)


def stocked_items(conn, shop_id: str) -> list[dict]:
    """Only items this shop has bought or counted. Munshi never proposes an item the shop doesn't keep."""
    return rows(conn, """SELECT * FROM item WHERE sku_id IN (
                           SELECT sku_id FROM purchase WHERE shop_id=? AND status='ok' AND sku_id IS NOT NULL
                           UNION SELECT sku_id FROM shelf_check WHERE shop_id=?)
                         ORDER BY sku_id""", shop_id, shop_id)


def all_states(conn, shop_id: str, now: datetime | None = None) -> list[ItemState]:
    return [item_state(conn, shop_id, it, now) for it in stocked_items(conn, shop_id)]


def recompute(conn, shop_id: str, now: datetime | None = None) -> list[ItemState]:
    """Recompute and store SalesEstimate rows (README: derived, recomputed after every event)."""
    now = now or clock.now()
    states = all_states(conn, shop_id, now)
    for s in states:
        cover = s.cover_days
        conn.execute("""INSERT OR REPLACE INTO sales_estimate VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (shop_id, s.sku_id, s.velocity, s.band.low, s.band.high, s.band.label,
                      s.on_hand, None if math.isinf(cover) else cover, int(s.censored), iso(now)))
    return states


def item_by_sku(conn, sku_id: str) -> dict | None:
    return row(conn, "SELECT * FROM item WHERE sku_id=?", sku_id)
