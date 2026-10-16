"""SQLite mock calendar. One table, minimal PII (name + phone only)."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_name     TEXT NOT NULL,
    phone            TEXT NOT NULL,
    service          TEXT NOT NULL,
    start_ts         TEXT NOT NULL,   -- ISO 'YYYY-MM-DDTHH:MM'
    end_ts           TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('booked','cancel_pending','cancelled')),
    idempotency_key  TEXT UNIQUE,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appt_start ON appointments(start_ts);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False: the web API serves requests from a thread pool; ClinicTools serialises access with a lock
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
