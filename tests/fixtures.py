"""Test fixtures: one onboarded shop with three weeks of bills and shelf counts, two neighbours,
and the extraction result for a distributor bill (what Claude returns for a perfect read)."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.db import init_db, iso, load_catalogue_csv, locality_key

SHOP = "919000000001"
NEIGHBOURS = {"919000000002": {"SURF1": 0.40, "HAL200": 0.15, "PG5": 7.0},
              "919000000003": {"SURF1": 0.25, "HAL200": 0.10}}
DIST_PHONE = "919000000099"

#          c21  c14  c7   q20  q13   (counts on Wednesday evenings, deliveries on Thursdays)
HISTORY = {
    "PG5": (30, 39, 24, 72, 48), "MAG70": (20, 26, 8, 48, 24), "MAG140": (10, 3, 8, 0, 12),
    "YIP70": (4, 4, 3, 0, 0), "TSALT1": (12, 11, 10, 10, 10), "ASH5": (6, 6, 3, 4, 0),
    "FSO1": (8, 8, 8, 6, 6), "SUG1": (15, 11, 7, 10, 10), "TUR1": (5, 6, 6, 5, 4),
    "RL250": (4, 7, 5, 6, 0), "BRU50": (3, 2, 2, 0, 0), "RJ1L": (3, 2, 2, 0, 0),
    "HAL200": (8, 7, 5, 0, 0), "LAY52": (20, 23, 26, 24, 24), "DM13": (30, 38, 10, 36, 0),
    "SURF1": (5, 8, 5, 6, 0), "VIM200": (15, 7, 23, 0, 24), "COL100": (6, 14, 10, 12, 0),
    "LB125": (10, 5, 12, 0, 12), "GDC100": (8, 4, 12, 0, 12), "BRB150": (10, 16, 10, 12, 0),
    "HOR500": (1, 1, 0, 0, 0), "KJ200": (1, 1, 0, 0, 0),
}

# Last Thursday's delivery: sku, qty, printed name, near-expiry offset (days from today) or None
BILL = [
    ("PG5", 48, "PARLE G 5RS", None), ("MAG70", 24, "MAGGI NDL 70G", None), ("TSALT1", 10, "TATA SALT 1KG", None),
    ("ASH5", 4, "AASHIRVAAD ATTA 5KG", None), ("SUG1", 10, "SUGAR 1KG PCH", None), ("TUR1", 5, "TOOR DAL 1KG", None),
    ("RJ1L", 4, "REAL JCE MXD FRT 1L", 18), ("HAL200", 6, "HLDRM ALOO BHJ 200G", 17),
    ("BRB150", 8, "BRIT BOURBON 150G", 19), ("DM13", 36, "CDBRY DM 13G", None),
    ("GDC100", 12, "GD CSHW 100G", None),  # not in alias memory -> one yes/no question
    ("VIM200", 24, "VIM BAR 200G", None), ("LB125", 12, "LIFEBUOY 125G", None), ("RL250", 6, "RED LABEL 250G", None),
]
BILL_ALIASES = {name: sku for sku, _, name, _ in BILL if sku != "GDC100"}
VOICE_ALIASES = {"biscuit chhota": "PG5", "parle": "PG5", "maggi": "MAG70", "maggi bada": "MAG140",
                 "uppu packet": "TSALT1", "namkeen": "HAL200", "juice": "RJ1L"}
SCHEMES = [("HAL200", 12, 1), ("SURF1", 10, 1), ("PG5", 24, 2)]


def last_thursday(now: datetime) -> datetime:
    d = now - timedelta(days=1)
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d.replace(hour=11, minute=0)


def bill_extraction(now: datetime, extra_lines: list[dict] | None = None) -> dict:
    cat = {c["sku_id"]: c for c in load_catalogue_csv()}
    day = last_thursday(now)
    items = []
    for sku, qty, printed, exp_days in BILL:
        price = float(cat[sku]["unit_cost"])
        expiry = ((now + timedelta(days=exp_days)).date().isoformat() if exp_days is not None
                  else (day + timedelta(days=int(cat[sku]["shelf_life_days"]) - 20)).strftime("%Y-%m"))
        items.append({"name": printed, "qty": qty, "unit": "pc", "unit_price": price, "mrp": float(cat[sku]["mrp"]),
                      "line_total": round(qty * price, 2), "batch": "B1", "expiry": expiry, "scheme": None,
                      "confidence": 0.97})
    items += extra_lines or []
    return {"distributor": "Sharma Distributors", "bill_date": day.date().isoformat(),
            "bill_total": round(sum(i["line_total"] for i in items), 2), "items": items}


def build_shop(conn, now: datetime, onboarded_neighbours: bool = True) -> None:
    init_db(conn)
    x = conn.execute
    cur = x("INSERT INTO distributor (shop_id, name, phone) VALUES (?,?,?)", (SHOP, "Sharma Distributors", DIST_PHONE))
    key = locality_key("Kuvempunagar, Mysuru")
    x("""INSERT INTO shop (shop_id, name, owner_name, owner_phone, language, cash_budget, cash_on_hand, commitments,
         locality, locality_key, salesman_day, distributor_id, created_at, last_inbound_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (SHOP, "Sri Lakshmi Stores", "Ravi", SHOP, "en", 5000, 4500, 400, "Kuvempunagar, Mysuru", key, "Thursday",
       cur.lastrowid, iso(now - timedelta(days=30)), iso(now - timedelta(hours=1))))
    for sid, vels in NEIGHBOURS.items():
        x("INSERT INTO shop (shop_id, name, owner_phone, locality, locality_key, onboarding_step, last_inbound_at) "
          "VALUES (?,?,?,?,?,?,?)", (sid, f"Shop {sid[-1]}", sid, "Kuvempunagar", key,
                                    None if onboarded_neighbours else "name", iso(now - timedelta(hours=2))))
        for sku, v in vels.items():
            x("INSERT INTO sales_estimate VALUES (?,?,?,?,?,'high',NULL,NULL,0,?)", (sid, sku, v, v * .85, v * 1.15,
                                                                                       iso(now)))
    learned = iso(now - timedelta(days=14))
    for phrase, sku in {**{k.lower(): v for k, v in BILL_ALIASES.items()}, **VOICE_ALIASES}.items():
        x("INSERT INTO alias VALUES (?,?,?,?,?,?)", (SHOP, phrase, sku, 0.95, "seed", learned))

    cat = {c["sku_id"]: c for c in load_catalogue_csv()}
    thu = last_thursday(now)
    deliveries = [thu - timedelta(days=14), thu - timedelta(days=7)]
    walks = [(thu - timedelta(days=d)).replace(hour=20, minute=30) for d in (15, 8, 1)]
    for sku, (c21, c14, c7, q20, q13) in HISTORY.items():
        for when, count in zip(walks, (c21, c14, c7)):
            x("INSERT INTO shelf_check (shop_id, sku_id, count, status, source, checked_on) VALUES (?,?,?,?,?,?)",
              (SHOP, sku, count, "count", "voice", iso(when)))
        for when, qty in zip(deliveries, (q20, q13)):
            if qty:
                exp = (when + timedelta(days=int(cat[sku]["shelf_life_days"]) - 20)).date().isoformat()
                x("""INSERT INTO purchase (shop_id, sku_id, printed_name, qty, unit_price, expiry, received_on)
                     VALUES (?,?,?,?,?,?,?)""", (SHOP, sku, sku, qty, float(cat[sku]["unit_cost"]), exp, iso(when)))
    for sku, buy, free in SCHEMES:
        x("INSERT INTO scheme (shop_id, sku_id, buy_n, free_m, valid_till, offered_on) VALUES (?,?,?,?,?,?)",
          (SHOP, sku, buy, free, (now + timedelta(days=3)).date().isoformat(), iso(now)))
    for customer, amount, days_ago in [("Ramesh", 800, 5), ("Lakshmamma", 600, 25), ("Manju", 1200, 50)]:
        x("INSERT INTO udhaar (shop_id, customer, amount, given_on) VALUES (?,?,?,?)",
          (SHOP, customer, amount, (now - timedelta(days=days_ago)).date().isoformat()))
