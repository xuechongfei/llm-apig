import httpx

import app.db as db
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url) VALUES"
                 " (1,'a','anthropic','http://a'),(2,'b','openai','http://b')")
    conn.commit(); conn.close()


async def test_group_create_and_mapping(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/groups", data={"name": "claude-sonnet"})
        assert r.status_code == 303
        gid = db.connect().execute(
            "SELECT id FROM model_group").fetchone()["id"]
        r = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "claude-sonnet-4-5",
            "priority": "10", "supports_image": "on"})
        assert r.status_code == 303
        conn = db.connect()
        m = conn.execute("SELECT * FROM model_mapping").fetchone()
        assert m["supports_image"] == 1 and m["priority"] == 10
        conn.close()


async def test_mapping_move_priority(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping (id,group_id,channel_id,actual_model,priority)"
                 " VALUES (1,1,1,'a',10),(2,1,2,'b',20)")
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        await c.post("/admin/mappings/2/move?dir=up")
    conn = db.connect()
    rows = conn.execute("SELECT id,priority FROM model_mapping ORDER BY priority").fetchall()
    assert (rows[0]["id"], rows[1]["id"]) == (2, 1)
    conn.close()


async def test_same_channel_multi_model(tmp_path, monkeypatch):
    """同一渠道在同一模型组下可以添加多个不同模型"""
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/groups", data={"name": "multi"})
        assert r.status_code == 303
        gid = db.connect().execute(
            "SELECT id FROM model_group WHERE name='multi'").fetchone()["id"]
        # 添加第一个模型
        r1 = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "model-a", "priority": "10"})
        assert r1.status_code == 303
        # 添加第二个模型（同渠道，不同模型名）
        r2 = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "model-b", "priority": "20"})
        assert r2.status_code == 303
        conn = db.connect()
        rows = conn.execute(
            "SELECT * FROM model_mapping WHERE group_id=? ORDER BY priority",
            (gid,)).fetchall()
        assert len(rows) == 2
        assert rows[0]["actual_model"] == "model-a"
        assert rows[1]["actual_model"] == "model-b"
        conn.close()


async def test_duplicate_model_rejected(tmp_path, monkeypatch):
    """同渠道同模型名重复添加应被拒绝"""
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/groups", data={"name": "dup"})
        assert r.status_code == 303
        gid = db.connect().execute(
            "SELECT id FROM model_group WHERE name='dup'").fetchone()["id"]
        # 第一次添加
        await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "same-model", "priority": "10"})
        # 第二次添加相同组合
        r2 = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "same-model", "priority": "20"})
        assert r2.status_code == 303
        assert "error=" in r2.headers["location"]
        conn = db.connect()
        rows = conn.execute(
            "SELECT * FROM model_mapping WHERE group_id=?", (gid,)).fetchall()
        assert len(rows) == 1  # 只有一条
        conn.close()


async def test_mapping_edit(tmp_path, monkeypatch):
    """编辑映射的模型名和优先级"""
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping (id,group_id,channel_id,actual_model,priority) VALUES (1,1,1,'old',10)")
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/mappings/1/edit", data={
            "actual_model": "new-model", "priority": "50",
            "supports_image": "on"})
        assert r.status_code == 303
    conn = db.connect()
    m = conn.execute("SELECT * FROM model_mapping WHERE id=1").fetchone()
    assert m["actual_model"] == "new-model"
    assert m["priority"] == 50
    assert m["supports_image"] == 1
    conn.close()
