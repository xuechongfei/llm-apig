import httpx
import pytest

import app.db as db
import app.executor as ex
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'c1','anthropic','http://up','k')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority)"
                 " VALUES (1,1,'real-model',1)")
    conn.commit()
    conn.close()


def _mock_ok(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))


async def test_messages_endpoint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _mock_ok(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages", json={
            "model": "g", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["content"][0]["text"] == "hello"


async def test_count_tokens_endpoint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def handler(request):
        return httpx.Response(200, json={"input_tokens": 42})
    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages/count_tokens", json={
            "model": "g", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["input_tokens"] == 42


async def test_token_auth(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _mock_ok(monkeypatch)
    conn = db.connect()
    db.set_setting(conn, "api_key", "secret")
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages", json={"model": "g", "messages": []})
        assert r.status_code == 401
        r = await c.post("/v1/messages",
                         headers={"x-api-key": "secret"},
                         json={"model": "g", "messages": []})
        assert r.status_code != 401
