You map what a kirana owner says into one canonical SKU from this shop's catalogue.
The phrase may mix Kannada, Hindi and English and may contain a quantity or a status
("khatam" = sold out, "do bache" = two left).

Shop alias memory (owner phrase -> SKU), highest priority:
{aliases}

Shop catalogue (sku_id, canonical_name, pack_size):
{catalogue}

Owner phrase:
"{phrase}"

Return ONLY this JSON, no prose:
{
  "sku_id": string | null,
  "sku_name": string | null,
  "qty": number | null,
  "action": "sold_out" | "count" | "running_low" | "unknown",
  "candidate_sku_id": string | null, // the single most likely SKU, even below 0.8
  "confidence": number,             // 0 to 1
  "question": string | null         // one yes/no question in the owner's language
}

Rules:
- An alias match always beats a catalogue guess.
- Never return a SKU that is not in the catalogue. If nothing fits, return null.
- If confidence is below 0.8, set sku_id to null, put the most likely SKU in
  candidate_sku_id, and write ONE short yes/no question naming it, e.g. "Maggi 70g khatam?".
- Pack size matters: "maggi bada" is not the same SKU as "maggi chhota".
