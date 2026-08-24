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
