"""The README's KPIs per shop, and the locality demand report distributors pay for.

KPIs: rupees of expiring stock caught · stockouts on the top-20 items · minutes per order.
Demand report: SalesEstimate grouped by locality and item, never joined back to a shop_id,
and only where at least K_MIN_SHOPS shops contribute (so no single shop can be read off it).
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from . import clock
from .db import iso, parse_dt, rows

K_MIN_SHOPS = 3


def shop_kpis(conn, shop_id: str) -> dict:
    now = clock.now()
    caught = conn.execute("SELECT COALESCE(SUM(rupees),0), COUNT(*) FROM reminder WHERE shop_id=? AND rupees>0",
                          (shop_id,)).fetchone()
    top = [r["sku_id"] for r in rows(conn, """SELECT sku_id FROM sales_estimate WHERE shop_id=? AND velocity_per_day>0
                                              ORDER BY velocity_per_day DESC LIMIT 20""", shop_id)]

    def stockouts(start, end) -> int:
        if not top:
            return 0
        marks = ",".join("?" * len(top))
        return conn.execute(f"""SELECT COUNT(DISTINCT sku_id || substr(checked_on,1,10)) FROM shelf_check
                                WHERE shop_id=? AND status='sold_out' AND checked_on>=? AND checked_on<?
                                AND sku_id IN ({marks})""", (shop_id, iso(start), iso(end), *top)).fetchone()[0]

    mins = [(parse_dt(o["approved_on"]) - parse_dt(o["proposed_on"])).total_seconds() / 60
            for o in rows(conn, "SELECT proposed_on, approved_on FROM orders WHERE shop_id=? AND approved_on IS NOT NULL",
                          shop_id)]
    return {
        "expiring_stock_caught_rupees": round(caught[0]),
        "expiry_actions": caught[1],
        "top20_stockouts_last_7d": stockouts(now - timedelta(days=7), now),
        "top20_stockouts_prev_7d": stockouts(now - timedelta(days=14), now - timedelta(days=7)),
        "orders_approved": len(mins),
        "median_minutes_to_ok": round(median(mins), 1) if mins else None,
    }


def demand_report(conn) -> list[dict]:
    """Next-week demand per locality and item, aggregated over at least K_MIN_SHOPS onboarded shops."""
    out = rows(conn, """
        SELECT s.locality AS locality, i.category AS category, i.sku_id AS sku_id, i.canonical_name AS item,
               COUNT(DISTINCT se.shop_id) AS shops,
               ROUND(SUM(se.velocity_per_day) * 7) AS next_week_units,
               ROUND(SUM(COALESCE(se.band_low, se.velocity_per_day)) * 7) AS low,
               ROUND(SUM(COALESCE(se.band_high, se.velocity_per_day)) * 7) AS high
        FROM sales_estimate se
        JOIN shop s ON s.shop_id = se.shop_id
        JOIN item i ON i.sku_id = se.sku_id
        WHERE s.onboarding_step IS NULL AND s.locality_key IS NOT NULL
          AND i.shop_id IS NULL                      -- shop-learned items could identify a shop
          AND se.velocity_per_day > 0
        GROUP BY s.locality_key, i.sku_id
        HAVING COUNT(DISTINCT se.shop_id) >= ?
        ORDER BY s.locality, i.category, next_week_units DESC""", K_MIN_SHOPS)
    return out
