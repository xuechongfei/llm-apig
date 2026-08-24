from app.convert.stream_o2a import O2AStreamConverter


def _flow():
    c = O2AStreamConverter("ds")
    chunks = []
    chunks += c.feed({"type": "message_start", "message": {
        "id": "msg_1", "model": "claude-x",
        "usage": {"input_tokens": 9, "output_tokens": 0}}})
    chunks += c.feed({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}})
    chunks += c.feed({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": "Hi"}})
    chunks += c.feed({"type": "content_block_stop", "index": 0})
    chunks += c.feed({"type": "message_delta",
                      "delta": {"stop_reason": "end_turn"},
                      "usage": {"output_tokens": 4}})
    chunks += c.feed({"type": "message_stop"})
    chunks += c.finish()
    return chunks


def test_text_flow():
    chunks = _flow()
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[0]["model"] == "ds"
    assert chunks[1]["choices"][0]["delta"] == {"content": "Hi"}
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"] == {"prompt_tokens": 9, "completion_tokens": 4,
                             "total_tokens": 13}


def test_tool_flow():
    c = O2AStreamConverter("ds")
    chunks = []
    chunks += c.feed({"type": "message_start", "message": {
        "id": "msg_1", "usage": {"input_tokens": 1, "output_tokens": 0}}})
    chunks += c.feed({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "tool_use", "id": "t1",
                                        "name": "get", "input": {}}})
    chunks += c.feed({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "input_json_delta",
                                "partial_json": '{"q":'}})
    chunks += c.feed({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "input_json_delta",
                                "partial_json": '1}'}})
    chunks += c.feed({"type": "content_block_stop", "index": 0})
    chunks += c.feed({"type": "message_delta",
                      "delta": {"stop_reason": "tool_use"},
                      "usage": {"output_tokens": 2}})
    first_tc = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    assert first_tc["id"] == "t1"
    assert first_tc["function"] == {"name": "get", "arguments": ""}
    arg_chunks = [ch["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
                  for ch in chunks[2:4]]
    assert "".join(arg_chunks) == '{"q":1}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_ping_ignored():
    c = O2AStreamConverter("ds")
    assert c.feed({"type": "ping"}) == []
