"""SQLite persistence layer.

A monotonically increasing ``data_version`` (stored in ``meta``) tags every
state of the trial data.  Each publication records the version it was
computed from; a publication is stale iff its version differs from the
current one.  All mutations run inside ``BEGIN IMMEDIATE`` transactions
serialised by a process-wide write lock, so a publication always reflects
one consistent snapshot (blocks, differences, p-value and provenance are
same-version by construction).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plots (
    block_id     INTEGER NOT NULL CHECK (block_id BETWEEN 1 AND 12),
    treatment    TEXT    NOT NULL CHECK (treatment IN ('A', 'B')),
    area         REAL    NOT NULL CHECK (area > 0),
    fresh_weight REAL    NOT NULL CHECK (fresh_weight >= 0),
    moisture     REAL    CHECK (moisture IS NULL OR (moisture >= 0 AND moisture < 1)),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (block_id, treatment)
);
CREATE TABLE IF NOT EXISTS publications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    data_version   INTEGER NOT NULL,
    n_blocks       INTEGER NOT NULL,
    mean_diff      REAL    NOT NULL,
    p_value        REAL    NOT NULL,
    n_permutations INTEGER NOT NULL,
    payload        TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS fertilizer_operations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_no INTEGER NOT NULL UNIQUE CHECK (operation_no >= 1),
    start_weight REAL    NOT NULL CHECK (start_weight >= 0),
    created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS fertilizer_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_no     INTEGER NOT NULL,
    seq              INTEGER NOT NULL CHECK (seq >= 1),
    block_id         INTEGER NOT NULL CHECK (block_id BETWEEN 1 AND 12),
    treatment        TEXT    NOT NULL CHECK (treatment IN ('A', 'B')),
    remaining_weight REAL    NOT NULL CHECK (remaining_weight >= 0),
    refill           REAL    NOT NULL CHECK (refill >= 0),
    updated_at       TEXT    NOT NULL,
    UNIQUE (operation_no, seq),
    UNIQUE (operation_no, block_id, treatment),
    FOREIGN KEY (operation_no) REFERENCES fertilizer_operations(operation_no)
);
CREATE TABLE IF NOT EXISTS fertilizer_approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    data_version INTEGER NOT NULL,
    n_operations INTEGER NOT NULL,
    n_plots      INTEGER NOT NULL,
    n_anomalies  INTEGER NOT NULL,
    n_pending    INTEGER NOT NULL,
    payload      TEXT    NOT NULL
);
"""

_write_lock = threading.Lock()


def db_path() -> str:
    return os.environ.get("DB_PATH", "./trial.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with session(write=True) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0')"
        )


@contextmanager
def session(write: bool = False) -> Iterator[sqlite3.Connection]:
    """A connection wrapped in a transaction.

    ``write=True`` takes the process-wide write lock and begins an IMMEDIATE
    transaction, guaranteeing a consistent snapshot that no other writer can
    disturb until commit/rollback.
    """
    if write:
        _write_lock.acquire()
    conn = connect()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if write:
            _write_lock.release()


def get_data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row["value"])


def bump_data_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        " WHERE key = 'data_version'"
    )
    return get_data_version(conn)


def fetch_plots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT block_id, treatment, area, fresh_weight, moisture, updated_at"
        " FROM plots ORDER BY block_id, treatment"
    ).fetchall()


def fetch_fertilizer_operations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All fertilizer operations with their entries (current raw rows)."""
    op_rows = conn.execute(
        "SELECT operation_no, start_weight, created_at"
        " FROM fertilizer_operations ORDER BY operation_no"
    ).fetchall()
    operations: list[dict[str, Any]] = []
    for op in op_rows:
        entries = conn.execute(
            "SELECT seq, block_id, treatment, remaining_weight, refill, updated_at"
            " FROM fertilizer_entries WHERE operation_no = ? ORDER BY seq",
            (op["operation_no"],),
        ).fetchall()
        operations.append(
            {
                "operation_no": op["operation_no"],
                "start_weight": op["start_weight"],
                "created_at": op["created_at"],
                "entries": [dict(row) for row in entries],
            }
        )
    return operations


def next_fertilizer_operation_no(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(operation_no), 0) + 1 AS next_no"
        " FROM fertilizer_operations"
    ).fetchone()
    return int(row["next_no"])
