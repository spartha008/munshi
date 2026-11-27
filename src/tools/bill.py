"""parse_bill(image) -> Purchase[]   (Claude vision, strict JSON, reconciled against the total)"""
from __future__ import annotations

from .. import llm
from ..trace import traced


class BillUnreadable(Exception):
    pass


def reconcile(data: dict) -> tuple[bool, float]:
    """README: the lines must match bill_total within 1%, after the bill's own discount and tax lines."""
    lines = round(sum(i.get("line_total") or 0 for i in data.get("items", [])), 2)
    total = data.get("bill_total")
    if not total:
        return False, lines
    adjusted = lines - (data.get("discount") or 0) + (data.get("tax") or 0)
    slack = max(0.01 * total, 1.0)   # a rupee of rounding is normal on printed bills
    return (abs(lines - total) <= slack or abs(adjusted - total) <= slack), lines


@traced("parse_bill")
def parse_bill(image: bytes, media_type: str) -> dict:
    if not llm.enabled():
        raise BillUnreadable("I can't read bill photos right now (bill reading is not set up on the server).")
    try:
        data = llm.extract_bill(image, media_type)
        ok, lines = reconcile(data)
        if not ok and data.get("bill_total"):
            # Retry once with the mismatch described (README: Prompts -> Bill extraction).
            data = llm.extract_bill(image, media_type, mismatch_note=(
                f"line totals add up to {lines:.2f} but the printed total is {data['bill_total']:.2f}."))
            ok, lines = reconcile(data)
    except llm.LLMRefused as e:
        raise BillUnreadable("I couldn't read that photo. Please send a clear photo of the distributor bill.") from e
    except llm.LLMUnavailable as e:
        raise BillUnreadable("I couldn't read the bill just now. Please send it again in a few minutes.") from e
    if not data.get("items"):
        raise BillUnreadable("I couldn't find any items in that photo. Is it a distributor bill? "
                             "Try a flat, well-lit photo of the whole bill.")
    data.update(extractor="claude", reconciled=ok, lines_total=lines)
    return data
