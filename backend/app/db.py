"""SQLite 连接与 schema 初始化。

设计要点：
- WAL 模式 + busy_timeout，支持多读单写并发；
- 所有写操作在 services 层通过 write_tx() 以 BEGIN IMMEDIATE 开启事务，
  保证"检查-写入"原子化，并发交接不会产生双重链路；
- 幂等由数据库唯一约束兜底：custody_events.idempotency_key、
  custody_events(sample_id, seq)、samples.barcode、
  temp_readings(batch_id, sample_id, recorded_at, temperature)。
"""
import os
import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('staff', 'approver', 'admin')),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    temp_min   REAL NOT NULL,
    temp_max   REAL NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    barcode    TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- 每样本一行的当前保管状态，与链尾事件在同一事务内更新
CREATE TABLE IF NOT EXISTS custody_state (
    sample_id      INTEGER PRIMARY KEY REFERENCES samples(id),
    current_holder TEXT NOT NULL,
    last_seq       INTEGER NOT NULL,
    last_hash      TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 保管链：每样本 seq 严格递增，hash 链接前一事件，篡改可检测
CREATE TABLE IF NOT EXISTS custody_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    seq             INTEGER NOT NULL,
    from_holder     TEXT NOT NULL,
    to_holder       TEXT NOT NULL,
    location        TEXT NOT NULL,
    scanned_at      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (sample_id, seq)
);

CREATE TABLE IF NOT EXISTS temp_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    filename    TEXT,
    imported_by TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    accepted    INTEGER NOT NULL DEFAULT 0,
    duplicates  INTEGER NOT NULL DEFAULT 0,
    rejected    INTEGER NOT NULL DEFAULT 0
);

-- sample_id = 0 表示批次级读数（避免 NULL 破坏唯一约束）
CREATE TABLE IF NOT EXISTS temp_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id   INTEGER NOT NULL REFERENCES temp_imports(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    sample_id   INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    UNIQUE (batch_id, sample_id, recorded_at, temperature)
);

-- 超限事件：open 状态下冻结对应样本（或整批）的后续交接
CREATE TABLE IF NOT EXISTS excursions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id  INTEGER NOT NULL UNIQUE REFERENCES temp_readings(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    sample_id   INTEGER NOT NULL DEFAULT 0,
    temperature REAL NOT NULL,
    temp_min    REAL NOT NULL,
    temp_max    REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'released', 'corrected')),
    created_at  TEXT NOT NULL
);

-- 处置记录：授权人员提交，必须带理由
CREATE TABLE IF NOT EXISTS dispositions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    excursion_id INTEGER NOT NULL REFERENCES excursions(id),
    action       TEXT NOT NULL CHECK (action IN ('release', 'correct')),
    reason       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 交接冲突（持有人不符 / 冻结期尝试 / 未登记条码），持久化待处置
CREATE TABLE IF NOT EXISTS conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER,
    barcode         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('holder_mismatch', 'frozen', 'unknown_sample')),
    expected_holder TEXT,
    actual_holder   TEXT,
    attempted_to    TEXT,
    location        TEXT,
    actor           TEXT NOT NULL,
    detail          TEXT,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged')),
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT,
    created_at      TEXT NOT NULL
);

-- 追加式审计日志，全局哈希链
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail    TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_custody_events_sample ON custody_events(sample_id, seq);
CREATE INDEX IF NOT EXISTS idx_excursions_open ON excursions(status, batch_id, sample_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts(status);
CREATE INDEX IF NOT EXISTS idx_temp_readings_batch ON temp_readings(batch_id);
"""

# 初始账号（首次启动时写入，可用环境变量控制演示数据，账号固定内置）
SEED_USERS = [
    ("staff1", "staff123", "staff"),
    ("staff2", "staff123", "staff"),
    ("lead1", "lead123", "approver"),
    ("admin", "admin123", "admin"),
]


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def read_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写事务：BEGIN IMMEDIATE 立即取得写锁，串行化所有写者。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    from .security import hash_password  # 避免循环导入
    from .util import now_iso

    conn = connect()
    try:
        conn.executescript(SCHEMA)  # executescript 自带隐式提交，不包写事务
        conn.execute("PRAGMA journal_mode = WAL")
        for username, password, role in SEED_USERS:
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (username, hash_password(password), role, now_iso()),
            )
        conn.commit()
    finally:
        conn.close()
