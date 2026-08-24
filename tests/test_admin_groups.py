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
