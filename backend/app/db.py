"""SQLite 数据访问层。

- WAL 模式 + busy_timeout，读写可并发；
- 所有写操作通过 tx() 进入 BEGIN IMMEDIATE 事务，并由进程内写锁串行化，
  配合唯一约束（idempotency_key / (sample_id, seq) 等）保证重复与并发提交幂等。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    temp_min    REAL NOT NULL,
    temp_max    REAL NOT NULL,
    frozen      INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode    TEXT NOT NULL UNIQUE,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_batch ON samples(batch_id);

CREATE TABLE IF NOT EXISTS custody_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    seq             INTEGER NOT NULL,
    from_holder     TEXT NOT NULL,
    to_holder       TEXT NOT NULL,
    location        TEXT NOT NULL,
    scanned_at      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL UNIQUE,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(sample_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_links_sample ON custody_links(sample_id, seq);

CREATE TABLE IF NOT EXISTS temperature_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    barcode     TEXT NOT NULL DEFAULT '',
    temp        REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    import_id   TEXT NOT NULL,
    reading_uid TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_batch ON temperature_readings(batch_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid  TEXT NOT NULL UNIQUE,
    type       TEXT NOT NULL CHECK (type IN ('OUT_OF_RANGE', 'RELEASE', 'CORRECTION')),
    status     TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'CLOSED')),
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    parent_id  INTEGER REFERENCES events(id),
    reading_id INTEGER REFERENCES temperature_readings(id),
    details    TEXT NOT NULL DEFAULT '{}',
    reason     TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, status);
-- 每个异常事件只允许一次解除（并发提交由该部分唯一索引兜底）
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_release_per_event
    ON events(parent_id) WHERE type = 'RELEASE';

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seq        INTEGER NOT NULL UNIQUE,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""

_write_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with _write_lock, connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def tx():
    """写事务：进程内写锁 + BEGIN IMMEDIATE，保证并发写串行。"""
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


@contextmanager
def read():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
