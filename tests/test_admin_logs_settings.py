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


async def test_settings_save_invalidates_update_cache(tmp_path, monkeypatch):
    """保存 update_url 后 /admin/api/update 立即反映新值（不命中旧缓存）"""
    _setup(tmp_path, monkeypatch)
    uc._cache.clear()
    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(200, json={
            "version": "99.0.0", "notes": "", "url": "http://rel"})))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.get("/admin/api/update")  # 无 update_url → 缓存 {update: None}
        assert r.json() == {"update": None}
        r = await c.post("/admin/settings", data={
            "update_url": "http://x/version.json"})
        assert r.status_code == 303
        r = await c.get("/admin/api/update")  # 缓存已清 → 重新检查
        assert r.json()["update"]["latest"] == "99.0.0"


async def test_update_api_fresh_bypasses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()
    uc._cache.clear()

    async def call(payload, fresh=False):
        monkeypatch.setattr(uc, "_transport", httpx.MockTransport(
            lambda req: httpx.Response(200, json=payload)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.get("/admin/api/update" + ("?fresh=1" if fresh else ""))
        return r.json()

    no_new = {"version": uc.current_version()}  # 同版本 → 无更新
    assert (await call({"version": "99.0.0"}))["update"]["latest"] == "99.0.0"
    # 缓存命中：即使远端已无新版本，仍返回旧的 99.0.0
    assert (await call(no_new))["update"]["latest"] == "99.0.0"
    # fresh=1 绕过缓存
    assert await call(no_new, fresh=True) == {"update": None}
