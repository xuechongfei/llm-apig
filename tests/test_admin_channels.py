import httpx

import app.db as db
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


async def test_dashboard_ok(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin")
    assert r.status_code == 200 and "渠道" in r.text


async def test_channel_crud(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/channels", data={
            "name": "官方", "protocol": "anthropic",
            "base_url": "https://api.anthropic.com", "api_key": "sk-x"})
        assert r.status_code == 303
        conn = db.connect()
        row = conn.execute("SELECT * FROM channel WHERE name='官方'").fetchone()
        assert row["protocol"] == "anthropic" and row["enabled"] == 1
        cid = row["id"]
        r = await c.post(f"/admin/channels/{cid}/toggle")
        conn2 = db.connect()
        assert conn2.execute("SELECT enabled FROM channel WHERE id=?",
                             (cid,)).fetchone()["enabled"] == 0
        r = await c.post(f"/admin/channels/{cid}/delete")
        assert db.connect().execute(
            "SELECT COUNT(*) c FROM channel").fetchone()["c"] == 0
        conn.close(); conn2.close()


async def test_channel_edit_masks_key(tmp_path, monkeypatch):
    """编辑页回显脱敏密钥（确认已配置），但明文不出现在页面里"""
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'c','anthropic','http://up','sk-secret-1234567890abcd')")
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/channels/1/edit")
    assert r.status_code == 200
    assert "已配置 sk-sec…abcd" in r.text          # 脱敏回显
    assert "sk-secret-1234567890abcd" not in r.text  # 明文不泄漏
    assert "留空则不修改" in r.text


async def test_channel_test_endpoint(tmp_path, monkeypatch):
    import app.admin.routes as admin
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'c','anthropic','http://up','k')")
    conn.commit(); conn.close()

    def handler(request):
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "h"}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(admin, "_transport", httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/admin/channels/1/test")
    assert r.json()["ok"] is True
