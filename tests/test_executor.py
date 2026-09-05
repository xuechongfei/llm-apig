import json

import httpx
import pytest

import app.db as db
import app.executor as ex
from app.executor import execute, render_sse


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db.connect()


def _add_channel(conn, id, name, protocol, priority, model="m"):
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (?,?,?,?,?)", (id, name, protocol, "http://up", "k"))
    conn.execute("INSERT OR IGNORE INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority,supports_image)"
                 " VALUES (1,?,?,?,1)", (id, model, priority))
    conn.commit()


def _mock(hits):
    """hits: list of (status, body_dict)。返回 transport 和调用记录。"""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        status, payload = hits[len(calls) - 1]
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), calls


async def test_success_anthropic_passthrough(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    transport, calls = _mock([(200, {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 2}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 200 and r.body["content"][0]["text"] == "hi"
    assert calls[0]["model"] == "m"  # 改写为 actual_model
    assert calls[0]["messages"] == []
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["status"] == "success" and log["output_tokens"] == 2
    conn.close()


async def test_balance_failover_to_next_channel(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "broke", "anthropic", 1)
    _add_channel(conn, 2, "rich", "anthropic", 2)
    transport, calls = _mock([
        (402, {"error": {"message": "Insufficient Balance"}}),
        (200, {"id": "m", "type": "message", "role": "assistant",
               "content": [{"type": "text", "text": "ok"}],
               "stop_reason": "end_turn",
               "usage": {"input_tokens": 1, "output_tokens": 1}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 200
    assert len(calls) == 2
    st = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert st["cooldown_until"] > 0  # 402 的渠道被冷却
    attempts = conn.execute("SELECT * FROM attempt ORDER BY id").fetchall()
    assert len(attempts) == 2
    assert attempts[0]["error_category"] == "insufficient_balance"
    conn.close()


async def test_client_error_not_retried(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    _add_channel(conn, 2, "c2", "anthropic", 2)
    transport, calls = _mock([(400, {"error": {"message": "bad param"}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 400 and len(calls) == 1
    conn.close()


async def test_all_fail_returns_last_error(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    _add_channel(conn, 2, "c2", "anthropic", 2)
    transport, _ = _mock([(500, {"e": "x"}), (503, {"e": "y"})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 503
    assert conn.execute("SELECT * FROM request_log").fetchone()["status"] == "failed"
    conn.close()


async def test_no_candidates(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 502
    assert r.body["error"]["type"] == "api_error"
    conn.close()


async def test_cross_protocol_conversion(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "ds", "openai", 1, model="deepseek-chat")
    transport, calls = _mock([(200, {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "max_tokens": 10,
                               "system": "s",
                               "messages": [{"role": "user", "content": "hi"}]},
                      stream=False)
    assert r.status == 200
    assert r.body["type"] == "message"  # 响应已转回 Anthropic 格式
    assert calls[0]["messages"][0] == {"role": "system", "content": "s"}
    assert conn.execute("SELECT * FROM attempt").fetchone()["converted"] == 1
    conn.close()


async def test_stream_error_status_retried(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    _add_channel(conn, 2, "c2", "anthropic", 2)
    sse = (b'event: message_start\ndata: {"type":"message_start","message":'
           b'{"id":"m","usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta",'
           b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
           b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": [], "stream": True},
                      stream=True)
    assert r.status == 200 and r.events is not None
    text = "".join([e async for e in r.events])
    assert "text_delta" in text and len(calls) == 2
    conn.close()


async def test_max_attempts_setting_limits_failover(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    for ch in range(1, 4):
        _add_channel(conn, ch, f"c{ch}", "anthropic", ch)
    db.set_setting(conn, "max_attempts", "2")
    transport, calls = _mock([(500, {"e": "x"})] * 3)
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 500
    assert len(calls) == 2  # 3 个候选只尝试了 2 个
    n = conn.execute(
        "SELECT COUNT(*) c FROM attempt WHERE skipped IS NULL"
    ).fetchone()["c"]
    assert n == 2
    conn.close()


async def test_max_attempts_invalid_values_fall_back(tmp_path, monkeypatch):
    for i, bad in enumerate(["abc", "0"]):
        conn = _conn(tmp_path / str(i), monkeypatch)
        for ch in range(1, 7):
            _add_channel(conn, ch, f"c{ch}", "anthropic", ch)
        db.set_setting(conn, "max_attempts", bad)
        transport, calls = _mock([(500, {"e": "x"})] * 6)
        monkeypatch.setattr(ex, "_transport", transport)
        r = await execute(conn, entry_protocol="anthropic", group_name="g",
                          payload={"model": "g", "messages": []}, stream=False)
        assert r.status == 500
        assert len(calls) == 5, f"max_attempts={bad!r} 应回退为 5 次"
        conn.close()


def test_render_sse_formats():
    ev = [{"type": "message_stop"}]
    assert render_sse(ev, "anthropic") == [
        'event: message_stop\ndata: {"type": "message_stop"}\n\n']
    assert render_sse(ev, "openai")[0].startswith("data: ")
