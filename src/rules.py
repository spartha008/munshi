"""Munshi decision rules.

Every number the owner sees comes from here. These are plain arithmetic over the
ledger; the LLM never does the maths (README: "Decision rules").
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TARGET_COVER_DAYS = 7          # how many days of stock an order buys up to
VELOCITY_FLOOR = 0.1           # units/day; below this an item is a slow mover


# --------------------------------------------------------------------------- #
# Velocity, cover, expiry
# --------------------------------------------------------------------------- #
@dataclass
class CountInterval:
    """Two consecutive shelf counts and what arrived between them."""
    days: float          # time between the two counts, in days
    count_start: float
    count_end: float
    units_in: float      # purchased (incl. free) units received in between


@dataclass
class Velocity:
    per_day: float
    observed_days: float
    sold_units: float
    censored: bool       # the item ran out at least once, so true demand may be higher


def velocity_from_intervals(intervals: list[CountInterval]) -> Velocity | None:
    """velocity = (units_in - delta shelf_count) / days_in_window."""
    usable = [iv for iv in intervals if iv.days > 0]
    if not usable:
        return None
    sold = 0.0
    days = 0.0
    censored = False
    for iv in usable:
        # A negative value means a count or a bill is missing; never count it as sales.
        sold += max(0.0, iv.count_start + iv.units_in - iv.count_end)
        days += iv.days
        if iv.count_end == 0:
            censored = True
    return Velocity(per_day=sold / days, observed_days=days, sold_units=sold, censored=censored)


def days_of_cover(on_hand: float, velocity: float) -> float:
    if velocity <= 0:
        return math.inf
    return on_hand / velocity


def expiry_ratio(days_to_expiry: float, cover_days: float) -> float:
    """< 1 means the stock will expire on the shelf."""
    if cover_days <= 0:
        return math.inf
    return days_to_expiry / cover_days


def rupees_at_risk(on_hand: float, velocity: float, days_to_expiry: float, unit_cost: float) -> float:
    units = max(0.0, on_hand - velocity * max(0.0, days_to_expiry))
    return units * unit_cost


@dataclass
class Band:
    low: float
    high: float
    label: str           # "high" | "medium" | "low" | "cohort"


def confidence_band(velocity: float, observed_days: float) -> Band:
    """Every forecast carries a band. Wider when we have watched the item for less time."""
    if observed_days >= 14:
        spread, label = 0.15, "high"
    elif observed_days >= 7:
        spread, label = 0.30, "medium"
    else:
        spread, label = 0.50, "low"
    return Band(low=velocity * (1 - spread), high=velocity * (1 + spread), label=label)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
@dataclass
class Budget:
    owner_cap: float
    cash_on_hand: float
    expected_udhaar: float
    commitments: float

    @property
    def available(self) -> float:
        return self.cash_on_hand + self.expected_udhaar - self.commitments

    @property
    def cap(self) -> float:
        """Never more than the owner's cap, never more than the cash that will exist."""
        return max(0.0, min(self.owner_cap, self.available))


def expected_udhaar(entries: list[tuple[float, float]]) -> float:
    """entries: (amount, likelihood of being paid back before the order is paid for)."""
    return sum(amount * max(0.0, min(1.0, p)) for amount, p in entries)


# --------------------------------------------------------------------------- #
# Scheme evaluator
# --------------------------------------------------------------------------- #
TAKE, SKIP, POOL = "TAKE", "SKIP", "POOL"
SELL_THROUGH_FLOOR = 0.7


@dataclass
class SchemeVerdict:
    verdict: str
    saving: float
    cash_locked: float
    cover_days: float
    threshold_days: float
    expiry_risk: bool
    pooled_cover_days: float | None
    reason: str


def evaluate_scheme(
    unit_price: float,
    velocity: float,
    buy_n: int,
    free_m: int,
    shelf_life_days: float,
    target_cover: float = TARGET_COVER_DAYS,
    neighbour_velocity: float = 0.0,
    past_sell_through: float | None = None,
) -> SchemeVerdict:
    """README rule:

    saving      = free_units * unit_price
    cash_locked = paid_units * unit_price
    cover_days  = (paid_units + free_units) / velocity
    verdict     = SKIP if cover_days > shelf_life_days or cover_days > 2 * target_cover
                = POOL if a neighbouring shop's demand brings cover_days under the threshold
                = TAKE otherwise

    Learn: if this shop's earlier scheme lots of the item sold less than 70% within the
    window (scheme.sell_through), a would-be TAKE becomes SKIP.
    """
    saving = free_m * unit_price
    cash_locked = buy_n * unit_price
    lot = buy_n + free_m
    cover = lot / velocity if velocity > 0 else math.inf
    threshold = min(shelf_life_days, 2 * target_cover)
    expiry_risk = cover > shelf_life_days

    if cover <= threshold and past_sell_through is not None and past_sell_through < SELL_THROUGH_FLOOR:
        return SchemeVerdict(SKIP, saving, cash_locked, cover, threshold, expiry_risk, None,
                             f"the last scheme lot only {past_sell_through:.0%} sold in time")
    if cover <= threshold:
        return SchemeVerdict(TAKE, saving, cash_locked, cover, threshold, expiry_risk, None,
                             f"sells through in {cover:.0f} days, saves ₹{saving:.0f}")

    pooled = None
    if neighbour_velocity > 0:
        pooled = lot / (velocity + neighbour_velocity)
        if pooled <= threshold:
            return SchemeVerdict(POOL, saving, cash_locked, cover, threshold, expiry_risk, pooled,
                                 f"alone it locks ₹{cash_locked:.0f} for {cover:.0f} days; "
                                 f"split with a nearby shop it clears in {pooled:.0f} days")

    why = f"would lock ₹{cash_locked:.0f} for {cover:.0f} days to save ₹{saving:.0f}"
    if expiry_risk:
        why += f"; shelf life is only {shelf_life_days:.0f} days"
    return SchemeVerdict(SKIP, saving, cash_locked, cover, threshold, expiry_risk, pooled, why)


# --------------------------------------------------------------------------- #
# Budget-capped order
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    sku_id: str
    name: str
    velocity: float
    on_hand: float
    unit_cost: float
    mrp: float
    case_qty: int = 1
    scheme: SchemeVerdict | None = None
    scheme_buy: int = 0
    scheme_free: int = 0
    note: str = ""       # e.g. "ran out on Mon"
    band_low: float | None = None   # every forecast carries its band
    band_high: float | None = None
    basis: str = ""      # "" (own history) | "shops nearby" | "bills only"
    uplift_note: str = ""  # e.g. "Christmas on 25 Dec: +35% (15–55%)"

    @property
    def margin_per_day(self) -> float:
        return self.velocity * max(0.0, self.mrp - self.unit_cost)


@dataclass
class Line:
    sku_id: str
    name: str
    qty: int
    free_qty: int
    unit_cost: float
    reason: str
    scheme_verdict: str | None = None

    @property
    def cost(self) -> float:
        return self.qty * self.unit_cost


@dataclass
class Skip:
    sku_id: str
    name: str
    why: str             # "slow" | "enough" | "budget"
    detail: str


@dataclass
class DraftOrder:
    budget_cap: float
    lines: list[Line] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    refused_schemes: list[tuple[Candidate, SchemeVerdict]] = field(default_factory=list)
    pool_schemes: list[tuple[Candidate, SchemeVerdict]] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(l.cost for l in self.lines)


def _round_up(x: float, step: int) -> int:
    step = max(1, step)
    return int(math.ceil(x / step) * step)


def draft_order(candidates: list[Candidate], budget_cap: float,
                target_cover: float = TARGET_COVER_DAYS,
                velocity_floor: float = VELOCITY_FLOOR) -> DraftOrder:
    """Greedy by velocity x margin, buy up to target_cover, stop at the cash cap."""
    order = DraftOrder(budget_cap=budget_cap)
    remaining = budget_cap

    for c in sorted(candidates, key=lambda c: c.margin_per_day, reverse=True):
        if c.scheme is not None and c.scheme.verdict == SKIP:
            order.refused_schemes.append((c, c.scheme))
        if c.scheme is not None and c.scheme.verdict == POOL:
            order.pool_schemes.append((c, c.scheme))

        need = c.velocity * target_cover - c.on_hand
        if need <= 0:
            cover = days_of_cover(c.on_hand, c.velocity)
            order.skipped.append(Skip(c.sku_id, c.name, "enough",
                                      f"{c.on_hand:.0f} on hand, {cover:.0f} days of cover"
                                      if cover != math.inf else f"{c.on_hand:.0f} on hand"))
            continue
        if c.velocity < velocity_floor:
            per = 1 / c.velocity if c.velocity > 0 else None
            order.skipped.append(Skip(c.sku_id, c.name, "slow",
                                      f"sells about 1 every {per:.0f} days" if per else "no sales seen"))
            continue

        free = 0
        verdict = None
        if c.scheme is not None and c.scheme.verdict == TAKE and c.scheme_buy > 0:
            lots = max(1, round(need / (c.scheme_buy + c.scheme_free)))
            qty = lots * c.scheme_buy
            free = lots * c.scheme_free
            verdict = TAKE
        else:
            qty = _round_up(need, c.case_qty)
            if c.scheme is not None:
                verdict = c.scheme.verdict

        # Shrink to fit the cash that is left, one case (or lot) at a time.
        step = c.scheme_buy if verdict == TAKE else max(1, c.case_qty)
        while qty > 0 and qty * c.unit_cost > remaining:
            qty -= step
            if verdict == TAKE:
                free -= c.scheme_free
        if qty <= 0:
            order.skipped.append(Skip(c.sku_id, c.name, "budget",
                                      f"needs ₹{_round_up(need, c.case_qty) * c.unit_cost:.0f}, "
                                      f"only ₹{remaining:.0f} left"))
            continue

        rate = f"sells ~{c.velocity:.1f}/day"
        if c.band_low is not None and c.band_high is not None:
            rate += f" ({c.band_low:.1f}–{c.band_high:.1f})"
        if c.basis:
            rate += f", estimate from {c.basis}"
        reason = f"{rate}, {c.on_hand:.0f} left"
        if c.uplift_note:
            reason += f"; {c.uplift_note}"
        if c.note:
            reason = f"{c.note}; {reason}"
        if verdict == TAKE:
            reason += (f"; scheme {c.scheme_buy}+{c.scheme_free} taken: {free} free worth ₹{free * c.unit_cost:.0f}, "
                       f"sells through in {c.scheme.cover_days:.0f} days")
        order.lines.append(Line(c.sku_id, c.name, qty, max(0, free), c.unit_cost, reason, verdict))
        remaining -= qty * c.unit_cost

    return order
