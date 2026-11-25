"""Cold-start festival forecast: locality cohort uplift x this shop's velocity, always with a band."""
from __future__ import annotations

import csv
import math
from datetime import date

from . import clock, ledger
from .db import DATA


def festivals() -> list[dict]:
    with open(DATA / "festivals.csv", encoding="utf-8") as f:
        return [{**r, "date": date.fromisoformat(r["date"])} for r in csv.DictReader(f)]


def uplifts(festival: str) -> dict[str, dict]:
    with open(DATA / "cohort.csv", encoding="utf-8") as f:
        return {r["category"]: {k: float(r[k]) for k in ("uplift_pct", "low_pct", "high_pct", "n_shops")}
                for r in csv.DictReader(f) if r["festival"] == festival}


def next_festival(today: date | None = None) -> dict | None:
    today = today or clock.today()
    upcoming = [f for f in festivals() if f["date"] >= today]
    return min(upcoming, key=lambda f: f["date"]) if upcoming else None


def festival_forecast(conn, shop_id: str) -> dict | None:
    fest = next_festival()
    if not fest:
        return None
    up = uplifts(fest["festival"])
    lines = []
    for s in ledger.all_states(conn, shop_id):
        u = up.get(s.category)
        if not u or s.velocity < 0.2:
            continue
        week = s.velocity * 7
        lines.append({"sku_id": s.sku_id, "name": s.name, "category": s.category,
                      "normal_week": round(week),
                      "festival_week": round(week * (1 + u["uplift_pct"] / 100)),
                      "low": round(s.band.low * 7 * (1 + u["low_pct"] / 100)),
                      "high": round(s.band.high * 7 * (1 + u["high_pct"] / 100)),
                      "uplift_pct": u["uplift_pct"]})
    lines.sort(key=lambda l: l["festival_week"] - l["normal_week"], reverse=True)
    # Uplifts are starting estimates (data/cohort.csv) until locality history covers a festival.
    return {"festival": fest["festival"], "date": fest["date"].isoformat(), "date_note": fest["date_note"],
            "source": "prior",
            "days_away": (fest["date"] - clock.today()).days,
            "categories": {k: v for k, v in up.items()}, "lines": lines[:6]}


def uplift_in_window(category: str, start: date, days: float) -> dict | None:
    """The festival uplift for a category if a festival falls inside an order's cover window."""
    end = start.toordinal() + int(math.ceil(days))
    for f in sorted(festivals(), key=lambda f: f["date"]):
        if start.toordinal() <= f["date"].toordinal() <= end:
            u = uplifts(f["festival"]).get(category)
            if u:
                return {"festival": f["festival"], "date": f["date"], **u}
    return None
