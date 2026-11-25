import math

import pytest

from src import rules
from src.rules import Candidate, CountInterval


def test_worked_scheme_example_from_the_deck():
    """Item at ₹50, sells 6 a month, scheme is buy 12 get 1 free -> SKIP."""
    v = rules.evaluate_scheme(unit_price=50, velocity=6 / 30, buy_n=12, free_m=1, shelf_life_days=60)
    assert v.saving == 50
    assert v.cash_locked == 600
    assert v.cover_days == pytest.approx(65)
    assert v.verdict == rules.SKIP
    assert v.expiry_risk  # shelf life 60 < 65 days of cover


def test_scheme_take_when_it_sells_through():
    v = rules.evaluate_scheme(unit_price=4.4, velocity=9, buy_n=24, free_m=2, shelf_life_days=180)
    assert v.verdict == rules.TAKE
    assert v.cover_days == pytest.approx(26 / 9)


def test_scheme_pool_when_a_neighbour_brings_cover_under_threshold():
    alone = rules.evaluate_scheme(120, 0.45, 10, 1, 720)
    assert alone.verdict == rules.SKIP
    pooled = rules.evaluate_scheme(120, 0.45, 10, 1, 720, neighbour_velocity=0.4)
    assert pooled.verdict == rules.POOL
    assert pooled.pooled_cover_days == pytest.approx(11 / 0.85)


def test_scheme_still_skip_if_neighbour_too_small():
    v = rules.evaluate_scheme(50, 0.2, 12, 1, 60, neighbour_velocity=0.2)
    assert v.verdict == rules.SKIP
    assert v.pooled_cover_days == pytest.approx(32.5)


def test_velocity_from_counts_and_purchases():
    # 20 on shelf, 24 arrive, 8 left after 7 days -> 36 sold -> ~5.1/day
    v = rules.velocity_from_intervals([CountInterval(days=7, count_start=20, count_end=8, units_in=24)])
    assert v.per_day == pytest.approx(36 / 7)
    assert not v.censored


def test_velocity_flags_stockout_as_censored_and_ignores_negative_gaps():
    v = rules.velocity_from_intervals([
        CountInterval(days=7, count_start=10, count_end=0, units_in=0),
        CountInterval(days=7, count_start=0, count_end=5, units_in=0),  # missing bill
    ])
    assert v.censored
    assert v.per_day == pytest.approx(10 / 14)


def test_cover_expiry_ratio_and_rupees_at_risk():
    assert rules.days_of_cover(10, 2) == 5
    assert rules.days_of_cover(10, 0) == math.inf
    assert rules.expiry_ratio(days_to_expiry=3, cover_days=6) == 0.5
    # 10 on hand, sells 1/day, expires in 4 days -> 6 units die -> ₹300 at ₹50
    assert rules.rupees_at_risk(10, 1, 4, 50) == 300


def test_budget_never_exceeds_owner_cap():
    b = rules.Budget(owner_cap=5000, cash_on_hand=4500, expected_udhaar=1000, commitments=200)
    assert b.available == 5300
    assert b.cap == 5000
    b2 = rules.Budget(owner_cap=5000, cash_on_hand=2000, expected_udhaar=500, commitments=300)
    assert b2.cap == 2200


def test_expected_udhaar_is_weighted_by_likelihood():
    assert rules.expected_udhaar([(800, 0.7), (600, 0.5), (1200, 0.15)]) == pytest.approx(1040)


def _cand(sku, velocity, on_hand, cost, mrp, case=1, **kw):
    return Candidate(sku, sku, velocity, on_hand, cost, mrp, case, **kw)


def test_draft_order_respects_budget_and_priority():
    cands = [
        _cand("fast", 10, 5, 10, 14, case=12),     # margin/day 40
        _cand("mid", 2, 0, 100, 115, case=6),      # margin/day 30
        _cand("slow", 0.05, 0, 200, 260),          # below floor
        _cand("full", 3, 60, 20, 25),              # enough cover
    ]
    order = rules.draft_order(cands, budget_cap=1000)
    assert order.total <= 1000
    names = [l.sku_id for l in order.lines]
    assert names[0] == "fast"
    assert order.lines[0].qty == 72  # need 65, rounded up to cases of 12
    whys = {s.sku_id: s.why for s in order.skipped}
    assert whys["slow"] == "slow"
    assert whys["full"] == "enough"


def test_draft_order_shrinks_a_line_to_fit_cash():
    order = rules.draft_order([_cand("a", 10, 0, 10, 12, case=10)], budget_cap=450)
    assert order.lines[0].qty == 40
    assert order.total <= 450


def test_draft_order_uses_take_scheme_lots_and_reports_refusals():
    take = rules.evaluate_scheme(4.4, 9, 24, 2, 180)
    skip = rules.evaluate_scheme(50, 0.2, 12, 1, 60)
    cands = [
        _cand("pg", 9, 2, 4.4, 5, case=24, scheme=take, scheme_buy=24, scheme_free=2),
        _cand("nm", 0.2, 6, 50, 60, case=12, scheme=skip, scheme_buy=12, scheme_free=1),
    ]
    order = rules.draft_order(cands, budget_cap=5000)
    pg = next(l for l in order.lines if l.sku_id == "pg")
    assert pg.qty % 24 == 0 and pg.free_qty == pg.qty // 24 * 2
    assert [c.sku_id for c, _ in order.refused_schemes] == ["nm"]
    assert all(l.sku_id != "nm" for l in order.lines)


def test_confidence_band_widens_with_less_history():
    assert rules.confidence_band(10, 21).label == "high"
    assert rules.confidence_band(10, 8).label == "medium"
    low = rules.confidence_band(10, 3)
    assert low.label == "low" and low.low == 5 and low.high == 15
