import time

import app.db as db
from app.logging_ import (add_attempt, clear_cooldown, create_log,
                          finish_log, set_cooldown)


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url) "
                 "VALUES (1,'c','anthropic','http://a')")
    conn.commit()
    return conn


def test_log_lifecycle(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    log_id = create_log(conn, client_protocol="anthropic",
                        group_name="g", path="/v1/messages", stream=True)
    add_attempt(conn, log_id, channel_id=1, channel_name="c",
                actual_model="m", http_status=402,
                error_category="insufficient_balance",
                error_summary="no money", raw_error='{"e":1}')
    add_attempt(conn, log_id, channel_id=1, channel_name="c",
                actual_model="m", http_status=200)
    finish_log(conn, log_id, status="success", total_ms=120,
               input_tokens=3, output_tokens=5)
    log = conn.execute("SELECT * FROM request_log WHERE id=?", (log_id,)).fetchone()
    assert log["status"] == "success" and log["stream"] == 1
    assert log["output_tokens"] == 5
    attempts = conn.execute(
        "SELECT * FROM attempt WHERE log_id=? ORDER BY id", (log_id,)).fetchall()
    assert len(attempts) == 2
    assert attempts[0]["error_category"] == "insufficient_balance"
    assert attempts[0]["raw_error"] == '{"e":1}'
    conn.close()


def test_cooldown(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    set_cooldown(conn, 1, 60, "boom")
    row = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert row["cooldown_until"] > time.time() + 50
    assert row["last_error"] == "boom"
    clear_cooldown(conn, 1)
    row = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert row["cooldown_until"] == 0
    conn.close()


def test_cooldown_zero_only_records_error(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    set_cooldown(conn, 1, 0, "note")
    row = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert row["cooldown_until"] == 0 and row["last_error"] == "note"
    conn.close()
