import app.db as db


def test_init_db_creates_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"channel", "model_group", "model_mapping", "request_log",
            "attempt", "settings", "channel_state"} <= tables
    db.init_db()  # 幂等，不报错
    conn.close()


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    assert db.get_setting(conn, "cooldown_balance", "600") == "600"
    db.set_setting(conn, "cooldown_balance", "300")
    assert db.get_setting(conn, "cooldown_balance", "600") == "300"
    conn.close()


def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", None)
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path))
    db.init_db()
    assert (tmp_path / "gateway.db").exists()


def test_db_path_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "env"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "explicit.db")
    db.init_db()
    assert (tmp_path / "explicit.db").exists()
    assert not (tmp_path / "env").exists()


def test_migration_preserves_data(tmp_path, monkeypatch):
    """模拟旧库 → 新库迁移，数据不丢失"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    # 模拟旧 schema：先手动建表（含旧约束）
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channel (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        protocol TEXT NOT NULL,
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
        UNIQUE (group_id, channel_id)
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
    """)
    conn.execute("INSERT INTO channel (id,name,protocol,base_url) VALUES (1,'a','anthropic','http://a')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping (id,group_id,channel_id,actual_model,priority) VALUES (1,1,1,'m1',10)")
    conn.commit()
    # 不设置 schema_version，模拟旧库
    conn.close()

    # 执行迁移
    db.init_db()

    conn = db.connect()
    # 数据保留
    m = conn.execute("SELECT * FROM model_mapping WHERE id=1").fetchone()
    assert m is not None
    assert m["actual_model"] == "m1"
    assert m["priority"] == 10
    # schema_version 已更新
    ver = db.get_setting(conn, "schema_version", "0")
    assert ver == str(len(db.MIGRATIONS))
    # 新约束生效：同渠道同模型重复插入报错
    import sqlite3
    try:
        conn.execute("INSERT INTO model_mapping (group_id,channel_id,actual_model) VALUES (1,1,'m1')")
        conn.commit()
        assert False, "should raise IntegrityError"
    except sqlite3.IntegrityError:
        pass
    conn.close()
