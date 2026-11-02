from app.reconstruct import AnthropicStreamReconstructor, parse_sse_event


def test_text_message_roundtrip():
    r = AnthropicStreamReconstructor()
    r.feed({"type": "message_start", "message": {
        "id": "msg_1", "model": "claude-x",
        "usage": {"input_tokens": 5, "output_tokens": 1,
                  "cache_read_input_tokens": 40,
                  "cache_creation_input_tokens": 2}}})
    r.feed({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})
    r.feed({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "你"}})
    r.feed({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "好"}})
    r.feed({"type": "content_block_stop", "index": 0})
    r.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 7}})
    r.feed({"type": "message_stop"})
    msg = r.to_json()
    assert msg["id"] == "msg_1" and msg["role"] == "assistant"
    assert msg["content"] == [{"type": "text", "text": "你好"}]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"]["cache_read_input_tokens"] == 40
    assert msg["usage"]["output_tokens"] == 7


def test_tool_use_input_assembled_from_partial_json():
    r = AnthropicStreamReconstructor()
    r.feed({"type": "message_start", "message": {"id": "m"}})
    r.feed({"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "t1", "name": "get", "input": {}}})
    for piece in ('{"q":', '1}'):
        r.feed({"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": piece}})
    r.feed({"type": "content_block_stop", "index": 0})
    block = r.to_json()["content"][0]
    assert block["input"] == {"q": 1}
    assert block["id"] == "t1" and block["name"] == "get"


def test_bad_partial_json_does_not_raise():
    r = AnthropicStreamReconstructor()
    r.feed({"type": "message_start", "message": {"id": "m"}})
    r.feed({"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "t1", "name": "get", "input": {}}})
    r.feed({"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{oops"}})
    r.feed({"type": "content_block_stop", "index": 0})
    assert r.to_json()["content"][0]["input"] == {}


def test_thinking_block_reconstructed():
    r = AnthropicStreamReconstructor()
    r.feed({"type": "message_start", "message": {"id": "m"}})
    r.feed({"type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""}})
    r.feed({"type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "想"}})
    r.feed({"type": "content_block_delta", "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"}})
    r.feed({"type": "content_block_stop", "index": 0})
    block = r.to_json()["content"][0]
    assert block["thinking"] == "想" and block["signature"] == "sig"


def test_blocks_sorted_by_index_across_interleaving():
    r = AnthropicStreamReconstructor()
    r.feed({"type": "message_start", "message": {"id": "m"}})
    r.feed({"type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""}})
    r.feed({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})
    r.feed({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "a"}})
    r.feed({"type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "b"}})
    contents = r.to_json()["content"]
    assert [c["text"] for c in contents] == ["a", "b"]


def test_empty_stream_returns_none():
    assert AnthropicStreamReconstructor().to_json() is None


def test_parse_sse_event():
    assert parse_sse_event('event: x\ndata: {"type": "ping"}') == {
        "type": "ping"}
    assert parse_sse_event("event: x\ndata: [DONE]") is None
    assert parse_sse_event("event: x") is None
    assert parse_sse_event("event: x\ndata: not json") is None
    assert parse_sse_event('data: [1, 2]') is None
