import httpx

import app.db as db
import app.update_check as uc
from app.errors import ErrorCategory, classify_error
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


def test_classify_with_custom_patterns():
    v = classify_error(400, "xxx 账户已冻结 xxx",
                       balance_patterns=["账户已冻结"])
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE
    # 默认 patterns 不命中 → 仍是 client error
    assert classify_error(400, "xxx 账户已冻结 xxx").category == ErrorCategory.CLIENT


async def test_logs_pages(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO request_log (created_at,client_protocol,group_name,"
                 "path,stream,status) VALUES (1.0,'anthropic','g','/v1/messages',0,"
                 "'failed')")
    conn.commit()
    log_id = conn.execute("SELECT id FROM request_log").fetchone()["id"]
    conn.execute("INSERT INTO attempt (log_id,created_at,channel_name,http_status,"
                 "error_summary) VALUES (?,1.0,'c1',402,'no money')", (log_id,))
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/logs?status=failed")
        assert r.status_code == 200 and "402" not in r.text  # 列表不含 attempt
        assert "'g'" in r.text or ">g<" in r.text
        r = await c.get(f"/admin/logs/{log_id}")
        assert r.status_code == 200 and "no money" in r.text


async def test_settings_page(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/settings", data={
            "cooldown_balance": "300", "cooldown_ratelimit": "45",
            "cooldown_auth": "1800", "cooldown_server": "60",
            "api_key": "k", "balance_patterns": "余额不足\nquota",
            "capability_patterns": "not support image"})
        assert r.status_code == 303
        conn = db.connect()
        assert db.get_setting(conn, "cooldown_balance", "") == "300"
        assert "quota" in db.get_setting(conn, "balance_patterns", "")
        conn.close()


async def test_update_api_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    uc._cache.clear()
    conn = db.connect()
    db.set_setting(conn, "update_url", "")
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/api/update")
    assert r.status_code == 200
    assert r.json() == {"update": None}


async def test_settings_page_shows_update_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://jihulab/x/version.json")
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/settings")
    assert r.status_code == 200
    assert "http://jihulab/x/version.json" in r.text
