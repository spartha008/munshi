"""normalise_item(phrase, shop) -> {sku, confidence, question}

Order of trust: this shop's alias memory > Claude (if available) > offline fuzzy match.
Below 0.8 confidence nothing is written to the ledger; the owner gets one yes/no question.
"""
from __future__ import annotations

import difflib
import re

from .. import clock, llm
from ..db import iso, row, rows
from ..trace import log, traced

CONFIDENCE_OK = 0.8

SOLD_OUT = {"khatam", "khatm", "khattam", "khalas", "khali", "finished", "over", "out", "mugide",
            "mugidide", "mugithu", "mugiythu", "illa", "nahi", "nahin", "zero", "nil"}
RUNNING_LOW = {"kam", "low", "kadime", "swalpa", "thoda", "less"}
NUMBERS = {
    # Hindi
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "barah": 12, "bees": 20,
    # Kannada
    "ondu": 1, "eradu": 2, "yeradu": 2, "mooru": 3, "muru": 3, "nalku": 4, "naalku": 4, "aidu": 5,
    "aidhu": 5, "aaru": 6, "elu": 7, "yelu": 7, "entu": 8, "yentu": 8, "ombattu": 9, "hattu": 10,
    "hanneradu": 12, "ippattu": 20,
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "dozen": 12,
}
FILLER = {"hai", "he", "hain", "ide", "ive", "bache", "bacha", "bachi", "bachey", "left", "only", "sirf",
          "bas", "is", "are", "the", "of", "pieces", "pcs", "pc", "baaki", "ulidide", "ulide", "hogide",
          "ho", "gaya", "gayi", "gaye", "aagide", "stock"}
SIZE_RE = re.compile(r"\d+(?:\.\d+)?(?:g|gm|gms|kg|ml|l|ltr|pc|pcs|rs)?")
# Split on punctuation and connectors, but never inside a number such as 2,205.00
SPLIT_RE = re.compile(r"\n+|(?<!\d)[,.;|]+|[,.;|]+(?!\d)|\s+(?:aur|and|mattu|matthu|also|phir)\s+", re.I)


def split_note(text: str) -> list[str]:
    """'Maggi khatam, Parle-G do bache' -> ['Maggi khatam', 'Parle-G do bache']"""
    return [p.strip() for p in SPLIT_RE.split(text) if p and p.strip()]


def parse_status(phrase: str) -> tuple[str, float | None, str]:
    """Pull the quantity/status words out of a phrase. Returns (item_phrase, qty, action)."""
    tokens = re.findall(r"[\w'\-₹]+", phrase.lower())
    qty: float | None = None
    action = "unknown"
    keep = []
    for i, t in enumerate(tokens):
        if t in SOLD_OUT and not (t in {"nahi", "nahin"} and i == 0):
            action, qty = "sold_out", 0
        elif t in RUNNING_LOW:
            action = "running_low"
        elif re.fullmatch(r"\d+", t) and (i > 0 or len(tokens) > 1):
            qty, action = float(t), "count"
        elif t in NUMBERS and i > 0:
            qty, action = float(NUMBERS[t]), "count"
        elif t in FILLER:
            continue
        else:
            keep.append(t)
    return " ".join(keep).strip(), qty, action


def _catalogue(conn, shop_id: str) -> list[dict]:
    """Shared catalogue + this shop's learned items; items the shop already stocks come first."""
    return rows(conn, """SELECT i.sku_id, i.canonical_name, i.pack_size, i.keywords, i.unit_cost,
                                (SELECT COUNT(*) FROM purchase p WHERE p.shop_id = ? AND p.sku_id = i.sku_id) AS stocked
                         FROM item i WHERE i.shop_id IS NULL OR i.shop_id = ?
                         ORDER BY stocked DESC, i.sku_id""", shop_id, shop_id)


def _alias(conn, shop_id: str, phrase: str) -> dict | None:
    return row(conn, "SELECT * FROM alias WHERE shop_id=? AND phrase=?", shop_id, phrase.lower().strip())


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9₹]+", s.lower())


def fuzzy_match(phrase: str, catalogue: list[dict]) -> tuple[dict | None, float]:
    """Offline guess. Capped below 0.8 unless one SKU's keyword matches exactly, so Munshi asks once."""
    p = phrase.lower().strip()
    if not p:
        return None, 0.0
    exact = []
    for c in catalogue:
        kws = [k.strip() for k in (c["keywords"] or "").split(";") if k.strip()]
        if p in kws or p == c["canonical_name"].lower():
            exact.append(c)
    if len(exact) == 1:
        return exact[0], 0.85
    if len(exact) > 1:  # e.g. "maggi" fits 70g and 140g: ask, guessing the pack the shop buys, else the everyday one
        exact.sort(key=lambda c: (-(c.get("stocked") or 0), c.get("unit_cost") or 0))
        return exact[0], 0.6

    ptoks = _tokens(p)
    vocabs = {c["sku_id"]: set(_tokens(c["canonical_name"]) + _tokens((c["keywords"] or "").replace(";", " ")))
              for c in catalogue}
    covering = [c for c in catalogue if ptoks and set(ptoks) <= vocabs[c["sku_id"]]]
    if len(covering) == 1:
        return covering[0], 0.85

    # Pack sizes ("200g", "1kg") only confirm or contradict a match; they never make one.
    words = [t for t in ptoks if not SIZE_RE.fullmatch(t)]
    sizes = {t for t in ptoks if SIZE_RE.fullmatch(t)}
    if not words:
        return None, 0.0
    best, best_score = None, 0.0
    for c in catalogue:
        vocab = {v for v in vocabs[c["sku_id"]] if not SIZE_RE.fullmatch(v)}
        if not vocab:
            continue
        per_tok = [max(difflib.SequenceMatcher(None, t, v).ratio() for v in vocab) for t in words]
        score = sum(per_tok) / len(per_tok)
        c_sizes = {v for v in vocabs[c["sku_id"]] if SIZE_RE.fullmatch(v)}
        if sizes and c_sizes:
            score += 0.05 if sizes & c_sizes else -0.1
        if score > best_score:
            best, best_score = c, score
    if best_score < 0.5:  # too weak to be worth one of the owner's three questions
        return None, best_score * 0.9
    return best, min(0.75, best_score * 0.9)


def _question(phrase: str, sku_name: str | None, action: str, qty: float | None) -> str:
    if not sku_name:
        return f"I don't know '{phrase}' yet. Which item is it?"
    if action == "sold_out":
        return f"'{phrase}': is that {sku_name}, sold out? (yes/no)"
    if action == "count" and qty is not None:
        return f"'{phrase}': is that {sku_name}, {qty:.0f} left? (yes/no)"
    return f"Is '{phrase}' {sku_name}? (yes/no)"


@traced("normalise_item")
def normalise_item(conn, shop_id: str, phrase: str, item_only: bool = False) -> dict:
    """item_only=True for bill names, where the whole phrase is the item."""
    if item_only:
        item_phrase, qty, action = phrase.strip(), None, "unknown"
    else:
        item_phrase, qty, action = parse_status(phrase)
    cat = _catalogue(conn, shop_id)
    by_sku = {c["sku_id"]: c for c in cat}

    hit = _alias(conn, shop_id, item_phrase) or _alias(conn, shop_id, phrase)
    if hit:
        c = by_sku[hit["sku_id"]]
        return {"sku_id": c["sku_id"], "sku_name": c["canonical_name"], "qty": qty, "action": action,
                "confidence": hit["confidence"], "question": None, "via": "alias", "phrase": item_phrase}

    if llm.enabled():
        try:
            aliases = [(a["phrase"], a["sku_id"]) for a in
                       rows(conn, "SELECT phrase, sku_id FROM alias WHERE shop_id=? ORDER BY last_confirmed DESC "
                                  "LIMIT 100", shop_id)]
            res = llm.normalise(phrase, aliases, [(c["sku_id"], c["canonical_name"], c["pack_size"])
                                                  for c in cat[:50]])
            sku = res.get("sku_id")
            if sku and sku not in by_sku:  # never accept a SKU outside the catalogue
                sku = None
            conf = float(res.get("confidence") or 0)
            guess = by_sku.get(sku) if sku else None
            if conf < CONFIDENCE_OK:
                # Below the bar nothing is written; the owner gets one yes/no question about the likeliest
                # SKU. The question is ours (English) so the worker can translate it like every reply.
                cand = res.get("candidate_sku_id") or sku
                guess = by_sku.get(cand) if cand in by_sku else fuzzy_match(item_phrase, cat)[0]
                act = res.get("action") or action
                if act == "unknown":
                    act = action
                q_qty = res.get("qty") if res.get("qty") is not None else qty
                return {"sku_id": None, "guess": guess and guess["sku_id"],
                        "sku_name": guess and guess["canonical_name"], "qty": q_qty, "action": act,
                        "confidence": conf, "via": "claude", "phrase": item_phrase,
                        "question": _question(phrase if item_only else item_phrase,
                                              guess and guess["canonical_name"], act, q_qty)}
            return {"sku_id": sku, "sku_name": guess and guess["canonical_name"],
                    "qty": res.get("qty") if res.get("qty") is not None else qty,
                    "action": res.get("action") if res.get("action") != "unknown" else action,
                    "confidence": conf, "question": None, "via": "claude", "phrase": item_phrase}
        except (llm.LLMUnavailable, llm.LLMRefused) as e:
            log("normalise_item.fallback", {"phrase": phrase}, {"reason": str(e)})

    guess, conf = fuzzy_match(item_phrase, cat)
    if guess and conf >= CONFIDENCE_OK:
        return {"sku_id": guess["sku_id"], "sku_name": guess["canonical_name"], "qty": qty, "action": action,
                "confidence": conf, "question": None, "via": "offline", "phrase": item_phrase}
    return {"sku_id": None, "guess": guess and guess["sku_id"], "sku_name": guess and guess["canonical_name"],
            "qty": qty, "action": action, "confidence": conf, "via": "offline", "phrase": item_phrase,
            "question": _question(phrase if item_only else item_phrase, guess and guess["canonical_name"],
                                  action, qty)}


def new_item_from_bill(conn, shop_id: str, line: dict, bill_date: str | None) -> str:
    """A bill line Munshi has never seen becomes this shop's own catalogue item."""
    from datetime import date as _date
    from ..ledger import parse_expiry

    printed = line["name"].strip()
    base = re.sub(r"[^A-Z0-9]+", "", printed.upper())[:10] or "ITEM"
    sku = f"{shop_id[-4:]}-{base}"
    n = 1
    while row(conn, "SELECT 1 FROM item WHERE sku_id=?", sku):
        n += 1
        sku = f"{shop_id[-4:]}-{base}{n}"
    cost = line.get("unit_price") or 0
    mrp = line.get("mrp") or (round(cost * 1.15, 2) if cost else None)  # margin unknown until MRP is seen
    shelf = 180
    exp = parse_expiry(line.get("expiry"))
    if exp and bill_date:
        try:
            shelf = max(7, (exp - _date.fromisoformat(bill_date[:10])).days)
        except ValueError:
            pass
    name = " ".join(w if any(ch.isdigit() for ch in w) else w.capitalize() for w in printed.split())
    conn.execute("""INSERT INTO item (sku_id, shop_id, canonical_name, pack_size, category, shelf_life_days,
                    unit_cost, mrp, case_qty, keywords) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (sku, shop_id, name, None, "other", shelf, cost, mrp, 1, printed.lower()))
    learn_alias(conn, shop_id, printed, sku, "bill")
    log("new_item", {"printed_name": printed}, {"sku_id": sku, "shelf_life_days": shelf})
    return sku


def learn_alias(conn, shop_id: str, phrase: str, sku_id: str, source: str) -> None:
    """A confirmed answer becomes memory, so the same question is never asked again."""
    conn.execute("INSERT OR REPLACE INTO alias VALUES (?,?,?,?,?,?)",
                 (shop_id, phrase.lower().strip(), sku_id, 0.95, source, iso(clock.now())))
    log("learn_alias", {"phrase": phrase}, {"sku_id": sku_id})
