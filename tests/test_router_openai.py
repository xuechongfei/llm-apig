import httpx

import app.db as db
import app.executor as ex
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'ds','openai','http://up','k')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'my-chat')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority)"
                 " VALUES (1,1,'deepseek-chat',1)")
    conn.commit()
    conn.close()


async def test_chat_completions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def handler(request):
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "yo"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}})
    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/chat/completions", json={
            "model": "my-chat",
            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "yo"


async def test_models_list(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["my-chat"]
