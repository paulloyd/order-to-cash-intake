import sqlite3, json
from datetime import datetime
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, account_no TEXT UNIQUE, name TEXT, email_domain TEXT,
  credit_limit REAL, balance REAL, credit_hold INTEGER DEFAULT 0, discount_pct REAL DEFAULT 0,
  requires_po INTEGER DEFAULT 1, default_ship_code TEXT);
CREATE TABLE IF NOT EXISTS contacts(email TEXT PRIMARY KEY, customer_id INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS ship_to(id INTEGER PRIMARY KEY, customer_id INTEGER, code TEXT, address TEXT, city TEXT, state TEXT, zip TEXT);
CREATE TABLE IF NOT EXISTS items(sku TEXT PRIMARY KEY, description TEXT, uom TEXT DEFAULT 'EA', list_price REAL, moq INTEGER, pack_multiple INTEGER);
CREATE TABLE IF NOT EXISTS customer_xref(customer_id INTEGER, customer_part TEXT, sku TEXT, PRIMARY KEY(customer_id, customer_part));
CREATE TABLE IF NOT EXISTS inventory(sku TEXT PRIMARY KEY, on_hand INTEGER, reserved INTEGER);

CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ref TEXT UNIQUE, channel TEXT, received_at TEXT, sender TEXT,
  subject TEXT, raw_text TEXT, content_hash TEXT UNIQUE, extracted_json TEXT, extractor TEXT, overrides_json TEXT DEFAULT '[]',
  customer_id INTEGER, po_number TEXT, status TEXT, run_no INTEGER DEFAULT 0, processing_ms REAL DEFAULT 0,
  touchless INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS exceptions(id INTEGER PRIMARY KEY, message_id INTEGER, run_no INTEGER, code TEXT, severity TEXT,
  path TEXT, line_no INTEGER, message TEXT, question TEXT, fix_json TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE, customer_id INTEGER, po_number TEXT,
  requested_date TEXT, ship_to_code TEXT, bc_order_no TEXT, hubspot_deal_id TEXT, sync_note TEXT, total REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS order_lines(id INTEGER PRIMARY KEY, order_id INTEGER, line_no INTEGER, sku TEXT, qty INTEGER,
  unit_price REAL, backorder_qty INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY, message_id INTEGER, status TEXT, note TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS outbound_emails(id INTEGER PRIMARY KEY, message_id INTEGER, kind TEXT, to_addr TEXT, subject TEXT,
  body TEXT, status TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, at TEXT, actor TEXT, action TEXT, ref TEXT, detail TEXT);

-- Simulated external systems (only used in mock mode) ---------------------------------
CREATE TABLE IF NOT EXISTS mock_bc_sales_orders(number TEXT PRIMARY KEY, customer_number TEXT, external_doc_no TEXT,
  payload_json TEXT, status TEXT, tracking TEXT, created_at TEXT, UNIQUE(customer_number, external_doc_no));
CREATE TABLE IF NOT EXISTS mock_hs_contacts(email TEXT PRIMARY KEY, name TEXT, company TEXT, hs_id TEXT);
CREATE TABLE IF NOT EXISTS mock_hs_deals(id TEXT PRIMARY KEY, name TEXT, amount REAL, stage TEXT, contact_email TEXT, case_ref TEXT);
CREATE TABLE IF NOT EXISTS mock_magento_imported(increment_id TEXT PRIMARY KEY, message_id INTEGER);
"""


def connect(path=None):
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def q(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def now_iso(dt=None):
    return (dt or datetime.utcnow()).replace(microsecond=0).isoformat(sep=" ")


def audit(db, actor, action, ref="", detail="", at=None):
    db.execute("INSERT INTO audit_log(at,actor,action,ref,detail) VALUES (?,?,?,?,?)",
               (at or now_iso(), actor, action, ref, detail if isinstance(detail, str) else json.dumps(detail)))
