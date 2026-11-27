"""SQLite ledger. Tables follow the README data model, one row set per shop (WhatsApp number).

On Render the file lives on the persistent disk (MUNSHI_DB=/var/data/munshi.db).
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.environ.get("MUNSHI_DB", ROOT / "munshi.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS distributor (
    distributor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT,
    return_days_before_expiry INTEGER NOT NULL DEFAULT 15
);
CREATE TABLE IF NOT EXISTS shop (
    shop_id TEXT PRIMARY KEY,               -- the owner's WhatsApp id (digits, with country code)
    name TEXT,
    owner_name TEXT,
    owner_phone TEXT,
    language TEXT DEFAULT 'en',             -- en | kn | hi
    cash_budget REAL NOT NULL DEFAULT 5000, -- set and changed only by the owner
    cash_on_hand REAL,
    commitments REAL NOT NULL DEFAULT 0,
    locality TEXT,
    salesman_day TEXT DEFAULT 'Thursday',
    target_cover_days REAL DEFAULT 7,
    distributor_id INTEGER,
    onboarding_step TEXT,                   -- NULL once onboarded
    channel TEXT DEFAULT 'meta',            -- meta (Cloud API) | twilio: how this owner reaches Munshi
    created_at TEXT,
    last_inbound_at TEXT
);
CREATE TABLE IF NOT EXISTS item (
    sku_id TEXT PRIMARY KEY,
    shop_id TEXT,                           -- NULL: shared catalogue; set: learned from this shop's bills
    canonical_name TEXT NOT NULL,
    pack_size TEXT,
    category TEXT,
    shelf_life_days INTEGER,
    unit_cost REAL,
    mrp REAL,
    case_qty INTEGER DEFAULT 1,
    keywords TEXT
);
CREATE TABLE IF NOT EXISTS alias (
    shop_id TEXT NOT NULL,
    phrase TEXT NOT NULL,
    sku_id TEXT NOT NULL REFERENCES item(sku_id),
    confidence REAL NOT NULL,
    source TEXT,
    last_confirmed TEXT,
    PRIMARY KEY (shop_id, phrase)
);
CREATE TABLE IF NOT EXISTS bill (
    bill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    distributor TEXT,
    bill_date TEXT,
    bill_total REAL,
    lines_total REAL,
    reconciled INTEGER,
    extractor TEXT,
    image_path TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS purchase (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    bill_id INTEGER,
    sku_id TEXT REFERENCES item(sku_id),    -- NULL until the owner confirms the name
    printed_name TEXT,
    qty REAL NOT NULL,
    free_qty REAL NOT NULL DEFAULT 0,
    unit_price REAL,
    mrp REAL,
    batch TEXT,
    expiry TEXT,
    scheme_terms TEXT,
    received_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok'       -- ok | pending
);
CREATE TABLE IF NOT EXISTS shelf_check (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    sku_id TEXT NOT NULL REFERENCES item(sku_id),
    count REAL,
    status TEXT,                            -- count | sold_out | running_low
    source TEXT,
    raw TEXT,
    checked_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales_estimate (
    shop_id TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    velocity_per_day REAL,
    band_low REAL,
    band_high REAL,
    confidence TEXT,
    on_hand REAL,
    days_of_cover REAL,
    censored INTEGER,
    computed_on TEXT,
    PRIMARY KEY (shop_id, sku_id)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    distributor_id INTEGER,
    proposed_on TEXT,
    for_date TEXT,
    status TEXT NOT NULL,                   -- proposed | edited | approved | sent | cancelled
    budget_cap REAL,
    budget_used REAL,
    approved_on TEXT,
    approval_text TEXT,
    sent_on TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS order_line (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(order_id),
    sku_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    free_qty INTEGER DEFAULT 0,
    unit_cost REAL,
    reason TEXT,
    scheme_verdict TEXT
);
CREATE TABLE IF NOT EXISTS scheme (
    scheme_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT,
    distributor_id INTEGER,
    sku_id TEXT NOT NULL,
    buy_n INTEGER NOT NULL,
    free_m INTEGER NOT NULL,
    valid_till TEXT,
    verdict TEXT,
    reason TEXT,
    sell_through REAL,
    offered_on TEXT
);
CREATE TABLE IF NOT EXISTS udhaar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    customer TEXT,
    amount REAL NOT NULL,                   -- still outstanding
    given_on TEXT,
    likelihood REAL,                        -- optional override; otherwise from age
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS upi_sale (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    amount REAL NOT NULL,
    received_at TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    asked_on TEXT,                          -- set when it is actually sent
    created_on TEXT,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | open | answered
    answer TEXT
);
CREATE TABLE IF NOT EXISTS reminder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    sku_id TEXT,
    due_on TEXT,
    reason TEXT,
    rupees REAL,                            -- stock value saved if acted on (KPI: expiring stock caught)
    status TEXT DEFAULT 'scheduled'         -- scheduled | sent
);
CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    direction TEXT NOT NULL,                -- in | out
    kind TEXT NOT NULL,                     -- text | image | voice | upi | system
    body TEXT,
    media_path TEXT,
    wa_message_id TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    to_name TEXT,
    to_phone TEXT,
    body TEXT,
    channel TEXT,                           -- whatsapp | forward
    status TEXT,
    error TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id TEXT NOT NULL,
    message_id INTEGER,
    tool TEXT NOT NULL,
    input TEXT,
    output TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS wa_inbound (
    wa_message_id TEXT PRIMARY KEY,         -- Meta retries webhooks; process each message once
    received_at TEXT
);
CREATE TABLE IF NOT EXISTS job_run (
    shop_id TEXT NOT NULL,
    job TEXT NOT NULL,
    run_date TEXT NOT NULL,
    PRIMARY KEY (shop_id, job, run_date)
);
CREATE INDEX IF NOT EXISTS ix_purchase_shop_sku ON purchase(shop_id, sku_id);
CREATE INDEX IF NOT EXISTS ix_check_shop_sku ON shelf_check(shop_id, sku_id);
CREATE INDEX IF NOT EXISTS ix_message_shop ON message(shop_id, id);
CREATE INDEX IF NOT EXISTS ix_trace_shop ON trace(shop_id, id);
CREATE INDEX IF NOT EXISTS ix_question_shop ON question(shop_id, status);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = str(path or DB_PATH)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if p != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def load_catalogue_csv() -> list[dict]:
    with open(DATA / "catalogue.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


MIGRATIONS = [("reminder", "rupees", "REAL"),  # additive columns for databases created by older versions
              ("shop", "channel", "TEXT DEFAULT 'meta'")]


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if missing, add new columns, load the shared seed catalogue (idempotent)."""
    conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    for c in load_catalogue_csv():
        conn.execute("""INSERT OR IGNORE INTO item (sku_id, shop_id, canonical_name, pack_size, category,
                        shelf_life_days, unit_cost, mrp, case_qty, keywords) VALUES (?,NULL,?,?,?,?,?,?,?,?)""",
                     (c["sku_id"], c["canonical_name"], c["pack_size"], c["category"], int(c["shelf_life_days"]),
                      float(c["unit_cost"]), float(c["mrp"]), int(c["case_qty"]), c["keywords"]))


def iso(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M")
    return dt.isoformat()


def parse_dt(s: str) -> datetime:
    if len(s) == 10:
        return datetime.fromisoformat(s + "T00:00")
    return datetime.fromisoformat(s)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def rows(conn, sql, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(conn, sql, *args) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def shop_items(conn, shop_id: str) -> list[dict]:
    """The catalogue this shop can match against: shared items plus items learned from its bills."""
    return rows(conn, "SELECT * FROM item WHERE shop_id IS NULL OR shop_id=? ORDER BY sku_id", shop_id)
