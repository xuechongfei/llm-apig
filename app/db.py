import os
import sqlite3
from pathlib import Path

DB_PATH: Path | None = None  # 测试注入点；None 时按 _db_path() 解析


def _db_path() -> Path:
    if DB_PATH is not None:
        return DB_PATH
    env = os.environ.get("LLMAPIG_DATA_DIR")
    if env:
        return Path(env) / "gateway.db"
    return Path(__file__).resolve().parent.parent / "data" / "gateway.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol IN ('anthropic', 'openai')),
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS model_group (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS model_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES model_group(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    actual_model TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    supports_image INTEGER NOT NULL DEFAULT 0,
    supports_video INTEGER NOT NULL DEFAULT 0,
    UNIQUE (group_id, channel_id, actual_model)
);
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    client_protocol TEXT NOT NULL,
    group_name TEXT NOT NULL,
    path TEXT NOT NULL,
    stream INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    total_duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL REFERENCES request_log(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    channel_id INTEGER,
    channel_name TEXT,
    actual_model TEXT,
    converted INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error_category TEXT,
    error_summary TEXT,
    raw_error TEXT,
    skipped TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id INTEGER PRIMARY KEY REFERENCES channel(id) ON DELETE CASCADE,
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


MIGRATIONS: list[str] = [
    # version 0 → 1: 模型组支持同渠道多模型
    """
    BEGIN;
    CREATE TABLE model_mapping_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL REFERENCES model_group(id) ON DELETE CASCADE,
        channel_id INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
        actual_model TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        supports_image INTEGER NOT NULL DEFAULT 0,
        supports_video INTEGER NOT NULL DEFAULT 0,
        UNIQUE (group_id, channel_id, actual_model)
    );
    INSERT INTO model_mapping_new SELECT * FROM model_mapping;
    DROP TABLE model_mapping;
    ALTER TABLE model_mapping_new RENAME TO model_mapping;
    COMMIT;
    """,
]


def connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        cur_ver = int(get_setting(conn, "schema_version", "0"))
        for i, sql in enumerate(MIGRATIONS):
            ver = i + 1
            if ver <= cur_ver:
                continue
            conn.executescript(sql)
            set_setting(conn, "schema_version", str(ver))
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
