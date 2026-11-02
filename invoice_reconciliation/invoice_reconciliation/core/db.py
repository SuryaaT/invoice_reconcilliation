"""SQLite layer. Non-destructive init (CREATE IF NOT EXISTS), foreign keys ON, Decimals stored as TEXT."""
import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers(id TEXT PRIMARY KEY, name TEXT NOT NULL, gstin TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, kind TEXT NOT NULL, original_name TEXT NOT NULL, stored_path TEXT NOT NULL,
  sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, extraction_method TEXT, uploaded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchase_orders(id TEXT PRIMARY KEY, po_number TEXT NOT NULL UNIQUE, supplier_id TEXT NOT NULL REFERENCES suppliers(id),
  po_date TEXT, currency TEXT NOT NULL, authorized_freight TEXT NOT NULL DEFAULT '0', document_id TEXT REFERENCES documents(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS po_lines(id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id), line_ref TEXT NOT NULL,
  description TEXT NOT NULL, hsn TEXT, qty TEXT NOT NULL, unit TEXT NOT NULL, unit_price TEXT NOT NULL, gst_rate TEXT, UNIQUE(po_id, line_ref));
CREATE TABLE IF NOT EXISTS grns(id TEXT PRIMARY KEY, grn_number TEXT NOT NULL UNIQUE, po_number TEXT NOT NULL, grn_date TEXT,
  document_id TEXT REFERENCES documents(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grn_lines(id TEXT PRIMARY KEY, grn_id TEXT NOT NULL REFERENCES grns(id), po_line_ref TEXT NOT NULL,
  description TEXT, hsn TEXT, received_qty TEXT NOT NULL, accepted_qty TEXT NOT NULL, unit TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices(id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, supplier_id TEXT NOT NULL REFERENCES suppliers(id),
  invoice_no TEXT NOT NULL, norm_invoice_no TEXT NOT NULL, invoice_date TEXT NOT NULL, po_number TEXT, buyer_gstin TEXT, currency TEXT NOT NULL,
  subtotal TEXT NOT NULL, cgst_total TEXT NOT NULL, sgst_total TEXT NOT NULL, igst_total TEXT NOT NULL, freight TEXT NOT NULL,
  other_charges TEXT NOT NULL, round_off TEXT NOT NULL, grand_total TEXT NOT NULL,
  original_json TEXT NOT NULL,            -- immutable snapshot of what was first extracted/loaded
  document_id TEXT REFERENCES documents(id), demo_scenario TEXT,
  status TEXT, latest_run_id TEXT, human_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_lines(id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), line_no INTEGER NOT NULL,
  po_line_ref TEXT, description TEXT NOT NULL, hsn TEXT, qty TEXT NOT NULL, unit TEXT NOT NULL, unit_price TEXT NOT NULL,
  taxable_value TEXT, gst_rate TEXT, cgst TEXT, sgst TEXT, igst TEXT, line_total TEXT, UNIQUE(invoice_id, line_no));
CREATE TABLE IF NOT EXISTS line_match_overrides(id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), line_no INTEGER NOT NULL,
  po_line_ref TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT, ts TEXT NOT NULL, UNIQUE(invoice_id, line_no));
CREATE TABLE IF NOT EXISTS field_corrections(id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), field TEXT NOT NULL,
  previous_value TEXT, new_value TEXT, reason TEXT NOT NULL, actor TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconciliation_runs(id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), run_at TEXT NOT NULL,
  status TEXT NOT NULL, tolerances_json TEXT NOT NULL, result_json TEXT NOT NULL, exception_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS exceptions(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES reconciliation_runs(id), invoice_id TEXT NOT NULL REFERENCES invoices(id),
  line_no INTEGER, code TEXT NOT NULL, severity TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL, expected TEXT, actual TEXT, difference TEXT);
CREATE TABLE IF NOT EXISTS review_decisions(id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), run_id TEXT REFERENCES reconciliation_runs(id),
  decision TEXT NOT NULL, system_status TEXT, is_override INTEGER NOT NULL, reason TEXT NOT NULL, reviewer TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(id TEXT PRIMARY KEY, ts TEXT NOT NULL, event_type TEXT NOT NULL, invoice_id TEXT, entity_type TEXT,
  entity_id TEXT, actor TEXT, details_json TEXT);
"""


def uid() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=None) -> None:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _v(x):
    if isinstance(x, (Decimal, date)):
        return str(x) if isinstance(x, Decimal) else x.isoformat()
    return x


def ins(conn, table: str, **cols) -> None:
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [_v(v) for v in cols.values()])


def upd(conn, table: str, row_id: str, **cols) -> None:
    conn.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in cols)} WHERE id=?",
                 [_v(v) for v in cols.values()] + [row_id])


def audit(conn, event_type: str, invoice_id=None, entity_type=None, entity_id=None, actor="system", **details) -> None:
    ins(conn, "audit_events", id=uid(), ts=now(), event_type=event_type, invoice_id=invoice_id, entity_type=entity_type,
        entity_id=entity_id, actor=actor, details_json=json.dumps(details, default=str))
