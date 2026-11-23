"""SQLite persistence: process runs (audit trail) and the AP invoice ledger."""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .config import DATA_DIR, DB_PATH, ensure_dirs
from .utils import norm_doc_number

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    source TEXT,
    status TEXT NOT NULL,              -- running | completed | error
    decision TEXT,                     -- APPROVED | NEEDS_REVIEW | ON_HOLD | REJECTED
    vendor_name TEXT,
    vendor_id TEXT,
    invoice_number TEXT,
    po_number TEXT,
    total REAL,
    extractor TEXT,
    duration_ms INTEGER,
    headline TEXT,
    result_json TEXT,
    events_json TEXT,
    resolution_json TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    vendor_id TEXT NOT NULL,
    invoice_number TEXT,
    invoice_number_norm TEXT,
    invoice_date TEXT,
    po_number TEXT,
    subtotal REAL,
    tax_total REAL,
    total REAL,
    status TEXT NOT NULL,              -- APPROVED | PAID | NEEDS_REVIEW | ON_HOLD | REJECTED
    lines_json TEXT,                   -- {po_line_no: qty}
    file_hash TEXT,
    posted_at TEXT NOT NULL,
    source TEXT NOT NULL               -- seed | pipeline
);
"""

_conn: Optional[sqlite3.Connection] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        ensure_dirs()
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        if _conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            _seed_ledger(_conn)
    return _conn


@contextmanager
def locked() -> Iterator[sqlite3.Connection]:
    with _lock:
        yield conn()


def _seed_ledger(c: sqlite3.Connection) -> None:
    """Historical invoices already in the ERP before this tool went live."""
    with open(DATA_DIR / "ledger_seed.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lines = {p.split(":")[0]: float(p.split(":")[1]) for p in r["lines"].split("|") if p}
            c.execute(
                "INSERT INTO ledger (run_id, vendor_id, invoice_number, invoice_number_norm, invoice_date,"
                " po_number, subtotal, tax_total, total, status, lines_json, file_hash, posted_at, source)"
                " VALUES (NULL,?,?,?,?,?,?,?,?,?,?,NULL,?, 'seed')",
                (r["vendor_id"], r["invoice_number"], norm_doc_number(r["invoice_number"]), r["invoice_date"],
                 r["po_number"], float(r["subtotal"]), float(r["tax_total"]), float(r["total"]), r["status"],
                 json.dumps(lines), r["posted_at"]),
            )


def reset_all() -> None:
    with locked() as c:
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM ledger")
        c.execute("DELETE FROM sqlite_sequence WHERE name='ledger'")
        _seed_ledger(c)


# ---- runs -------------------------------------------------------------------
def create_run(run_id: str, filename: str, file_hash: str, source: str) -> None:
    with locked() as c:
        c.execute(
            "INSERT INTO runs (id, created_at, filename, file_hash, source, status, events_json)"
            " VALUES (?,?,?,?,?, 'running', '[]')",
            (run_id, now_iso(), filename, file_hash, source),
        )


def finish_run(run_id: str, *, status: str, result: dict[str, Any], events: list[dict], duration_ms: int) -> None:
    inv = result.get("invoice") or {}
    dec = result.get("decision") or {}
    with locked() as c:
        c.execute(
            "UPDATE runs SET finished_at=?, status=?, decision=?, vendor_name=?, vendor_id=?, invoice_number=?,"
            " po_number=?, total=?, extractor=?, duration_ms=?, headline=?, result_json=?, events_json=? WHERE id=?",
            (now_iso(), status, dec.get("outcome"), result.get("vendor", {}).get("name") or inv.get("vendor_name"),
             result.get("vendor", {}).get("vendor_id"), inv.get("invoice_number"), result.get("po_number"),
             inv.get("total"), result.get("extractor"), duration_ms, dec.get("headline"),
             json.dumps(result, default=str), json.dumps(events, default=str), run_id),
        )


def get_run(run_id: str) -> Optional[dict]:
    with locked() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("result_json", "events_json", "resolution_json"):
        d[k.replace("_json", "")] = json.loads(d.pop(k)) if d.get(k) else None
    return d


def list_runs(limit: int = 200) -> list[dict]:
    with locked() as c:
        rows = c.execute(
            "SELECT id, created_at, finished_at, filename, source, status, decision, vendor_name, invoice_number,"
            " po_number, total, extractor, duration_ms, headline, resolution_json FROM runs"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["resolution"] = json.loads(d.pop("resolution_json")) if d.get("resolution_json") else None
        out.append(d)
    return out


def set_resolution(run_id: str, resolution: dict) -> None:
    with locked() as c:
        c.execute("UPDATE runs SET resolution_json=? WHERE id=?", (json.dumps(resolution), run_id))


# ---- ledger -----------------------------------------------------------------
def ledger_for_vendor(vendor_id: str) -> list[dict]:
    with locked() as c:
        rows = c.execute("SELECT * FROM ledger WHERE vendor_id=? AND status != 'REJECTED'", (vendor_id,)).fetchall()
    return [dict(r) for r in rows]


def ledger_by_hash(file_hash: str) -> Optional[dict]:
    with locked() as c:
        row = c.execute("SELECT * FROM ledger WHERE file_hash=? AND status != 'REJECTED'", (file_hash,)).fetchone()
    return dict(row) if row else None


def ledger_for_po(po_number: str) -> list[dict]:
    with locked() as c:
        rows = c.execute("SELECT * FROM ledger WHERE po_number=? AND status != 'REJECTED'", (po_number,)).fetchall()
    return [dict(r) for r in rows]


def all_ledger() -> list[dict]:
    with locked() as c:
        rows = c.execute("SELECT * FROM ledger ORDER BY posted_at DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def insert_ledger(entry: dict) -> int:
    with locked() as c:
        cur = c.execute(
            "INSERT INTO ledger (run_id, vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number,"
            " subtotal, tax_total, total, status, lines_json, file_hash, posted_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pipeline')",
            (entry["run_id"], entry["vendor_id"], entry.get("invoice_number"),
             norm_doc_number(entry.get("invoice_number")), entry.get("invoice_date"), entry.get("po_number"),
             entry.get("subtotal"), entry.get("tax_total"), entry.get("total"), entry["status"],
             json.dumps(entry.get("lines") or {}), entry.get("file_hash"), now_iso()),
        )
        return cur.lastrowid


def update_ledger_status_for_run(run_id: str, status: str) -> None:
    with locked() as c:
        c.execute("UPDATE ledger SET status=? WHERE run_id=?", (status, run_id))
