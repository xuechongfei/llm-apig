from app.convert.stream_a2o import A2OStreamConverter


def _text_flow():
    c = A2OStreamConverter("claude-sonnet")
    events = []
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"role": "assistant", "content": None},
                      "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"content": "Hel"}, "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"content": "lo"}, "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {},
                      "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    events += c.finish()
    return events


def test_text_stream_event_sequence():
    types = [e["type"] for e in _text_flow()]
    assert types == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]


def test_text_stream_content():
    events = _text_flow()
    deltas = [e["delta"]["text"] for e in events
              if e["type"] == "content_block_delta"]
    assert deltas == ["Hel", "lo"]
    msg = events[0]["message"]
    assert msg["model"] == "claude-sonnet" and msg["content"] == []
    md = [e for e in events if e["type"] == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"] == {"output_tokens": 2}


def test_tool_call_stream():
    c = A2OStreamConverter("m")
    events = []
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "get", "arguments": ""}}]},
        "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]},
        "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]},
        "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {},
                      "finish_reason": "tool_calls"}]})
    events += c.finish()
    types = [e["type"] for e in events]
    assert types == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]
    start = events[1]
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "get"
    json_deltas = [e["delta"]["partial_json"] for e in events
                   if e["type"] == "content_block_delta"]
    assert "".join(json_deltas) == '{"q":1}'
    md = [e for e in events if e["type"] == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use"


def test_text_then_tool_gets_two_blocks():
    c = A2OStreamConverter("m")
    events = []
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"content": "ok"}, "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": "call_1",
                        "function": {"name": "f", "arguments": "{}"}}]},
        "finish_reason": None}]})
    events += c.finish()
    starts = [e for e in events if e["type"] == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1]
    stops = [e for e in events if e["type"] == "content_block_stop"]
    assert [s["index"] for s in stops] == [0, 1]
