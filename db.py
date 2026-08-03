"""SQLite storage layer for locally cached (read-only) Square data."""
import json
import os
import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    name TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS catalog_categories (
    id TEXT PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS catalog_items (
    id TEXT PRIMARY KEY,
    name TEXT,
    category_id TEXT
);

CREATE TABLE IF NOT EXISTS catalog_variations (
    id TEXT PRIMARY KEY,
    item_id TEXT,
    item_name TEXT,
    variation_name TEXT,
    price_cents INTEGER,
    currency TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    location_id TEXT,
    created_at TEXT,
    closed_at TEXT,
    state TEXT,
    customer_id TEXT,
    total_money_cents INTEGER,
    currency TEXT
);

CREATE TABLE IF NOT EXISTS order_line_items (
    order_id TEXT,
    line_uid TEXT,
    catalog_object_id TEXT,
    name TEXT,
    quantity REAL,
    total_money_cents INTEGER,
    currency TEXT,
    PRIMARY KEY (order_id, line_uid)
);

CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    created_at TEXT,
    amount_cents INTEGER,
    currency TEXT,
    source_type TEXT,
    status TEXT,
    customer_id TEXT,
    location_id TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    location_id TEXT,
    customer_id TEXT,
    status TEXT,
    start_at TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS booking_segments (
    booking_id TEXT,
    segment_index INTEGER,
    service_variation_id TEXT,
    duration_minutes INTEGER,
    team_member_id TEXT,
    PRIMARY KEY (booking_id, segment_index)
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    given_name TEXT,
    family_name TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    detail TEXT
);
"""


def init_db():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def clear_all(conn):
    tables = [
        "locations",
        "catalog_categories",
        "catalog_items",
        "catalog_variations",
        "orders",
        "order_line_items",
        "payments",
        "bookings",
        "booking_segments",
        "customers",
    ]
    for t in tables:
        conn.execute(f"DELETE FROM {t}")


def upsert_location(conn, loc):
    conn.execute(
        "INSERT OR REPLACE INTO locations (id, name, raw) VALUES (?, ?, ?)",
        (loc.get("id"), loc.get("name"), json.dumps(loc)),
    )


def upsert_catalog_category(conn, cat_id, name):
    conn.execute(
        "INSERT OR REPLACE INTO catalog_categories (id, name) VALUES (?, ?)",
        (cat_id, name),
    )


def upsert_catalog_item(conn, item_id, name, category_id):
    conn.execute(
        "INSERT OR REPLACE INTO catalog_items (id, name, category_id) VALUES (?, ?, ?)",
        (item_id, name, category_id),
    )


def upsert_catalog_variation(conn, var_id, item_id, item_name, variation_name, price_cents, currency):
    conn.execute(
        """INSERT OR REPLACE INTO catalog_variations
           (id, item_id, item_name, variation_name, price_cents, currency)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (var_id, item_id, item_name, variation_name, price_cents, currency),
    )


def upsert_order(conn, order_id, location_id, created_at, closed_at, state, customer_id, total_money_cents, currency):
    conn.execute(
        """INSERT OR REPLACE INTO orders
           (id, location_id, created_at, closed_at, state, customer_id, total_money_cents, currency)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, location_id, created_at, closed_at, state, customer_id, total_money_cents, currency),
    )


def upsert_order_line_item(conn, order_id, line_uid, catalog_object_id, name, quantity, total_money_cents, currency):
    conn.execute(
        """INSERT OR REPLACE INTO order_line_items
           (order_id, line_uid, catalog_object_id, name, quantity, total_money_cents, currency)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (order_id, line_uid, catalog_object_id, name, quantity, total_money_cents, currency),
    )


def upsert_payment(conn, payment_id, order_id, created_at, amount_cents, currency, source_type, status, customer_id, location_id):
    conn.execute(
        """INSERT OR REPLACE INTO payments
           (id, order_id, created_at, amount_cents, currency, source_type, status, customer_id, location_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (payment_id, order_id, created_at, amount_cents, currency, source_type, status, customer_id, location_id),
    )


def upsert_booking(conn, booking_id, location_id, customer_id, status, start_at, created_at):
    conn.execute(
        """INSERT OR REPLACE INTO bookings
           (id, location_id, customer_id, status, start_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (booking_id, location_id, customer_id, status, start_at, created_at),
    )


def upsert_booking_segment(conn, booking_id, segment_index, service_variation_id, duration_minutes, team_member_id):
    conn.execute(
        """INSERT OR REPLACE INTO booking_segments
           (booking_id, segment_index, service_variation_id, duration_minutes, team_member_id)
           VALUES (?, ?, ?, ?, ?)""",
        (booking_id, segment_index, service_variation_id, duration_minutes, team_member_id),
    )


def upsert_customer(conn, customer_id, given_name, family_name, created_at):
    conn.execute(
        """INSERT OR REPLACE INTO customers (id, given_name, family_name, created_at)
           VALUES (?, ?, ?, ?)""",
        (customer_id, given_name, family_name, created_at),
    )


def log_sync_start(conn):
    cur = conn.execute(
        "INSERT INTO sync_log (started_at, status) VALUES (datetime('now'), 'running')"
    )
    return cur.lastrowid


def log_sync_finish(conn, sync_id, status, detail):
    conn.execute(
        "UPDATE sync_log SET finished_at = datetime('now'), status = ?, detail = ? WHERE id = ?",
        (status, detail, sync_id),
    )


def get_last_sync(conn):
    row = conn.execute(
        "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def has_any_data(conn):
    row = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()
    return row["c"] > 0
