You are reading a photo of a distributor bill issued to a small Indian kirana shop.
Extract every line item. Return ONLY a JSON object matching this schema. No prose,
no markdown fences.

{
  "distributor": string | null,
  "bill_date": "YYYY-MM-DD" | null,
  "bill_total": number | null,
  "discount": number | null,        // total discount line, if the bill shows one
  "tax": number | null,             // GST / tax line, if the bill shows one
  "items": [
    {
      "name": string,                 // exactly as printed, do not normalise
      "qty": number,                  // paid units only
      "unit": string | null,          // "pc", "pkt", "kg", "ltr", "box" ...
      "unit_price": number | null,
      "mrp": number | null,           // printed MRP per unit, if the bill shows it
      "line_total": number | null,
      "batch": string | null,
      "expiry": "YYYY-MM" | "YYYY-MM-DD" | null,
      "scheme": string | null,        // e.g. "12+1" if free units are shown
      "confidence": number            // 0 to 1, for this line
    }
  ]
}

Rules:
- Copy item names exactly as printed. Do not expand abbreviations or guess brands.
- Free or bonus quantities belong in "scheme", never in "qty".
- If a field is unreadable, set it to null and lower that line's confidence.
- The sum of line_total, minus "discount", plus "tax", should equal bill_total. If it
  does not, re-read the bill before answering; do not invent a line to make it balance.
- Dates printed as MM/YY are expiry dates in "YYYY-MM" form.
