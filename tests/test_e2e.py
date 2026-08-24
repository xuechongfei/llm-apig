import json

import httpx

import app.db as db
import app.executor as ex
from app.main import app


async def test_e2e_anthropic_client_failover_to_openai_channel(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key) VALUES"
                 " (1,'broke-claude','anthropic','http://a','k1'),"
                 " (2,'deepseek','openai','http://b','k2')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'my-claude')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority) VALUES"
                 " (1,1,'claude-sonnet-4-5',1),(1,2,'deepseek-chat',2)")
    conn.commit(); conn.close()

    calls = []

    def upstream(request):
        calls.append(str(request.url))
        if "http://a/" in str(request.url):
            return httpx.Response(402, json={
                "error": {"message": "Insufficient Balance"}})
        # OpenAI 渠道：验证收到的是转换后的 OpenAI 格式
        body = json.loads(request.content)
        assert body["model"] == "deepseek-chat"
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={
            "id": "c1", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "你好"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages", json={
            "model": "my-claude", "max_tokens": 100, "system": "你是助手",
            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"  # 客户端拿到 Anthropic 格式
    assert body["content"][0] == {"type": "text", "text": "你好"}
    assert len(calls) == 2  # 第一次 402，第二次成功
    conn = db.connect()
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["status"] == "success"
    attempts = conn.execute("SELECT * FROM attempt ORDER BY id").fetchall()
    assert attempts[0]["error_category"] == "insufficient_balance"
    assert attempts[1]["converted"] == 1
    conn.close()
