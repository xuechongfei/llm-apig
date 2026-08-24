import pytest

from app.convert.a2o import ConversionError, request, response


def test_request_system_and_text():
    p = {"model": "claude-sonnet", "max_tokens": 1024, "system": "be nice",
         "messages": [{"role": "user", "content": "hi"}]}
    o = request(p)
    assert o["messages"] == [{"role": "system", "content": "be nice"},
                             {"role": "user", "content": "hi"}]
    assert o["max_tokens"] == 1024


def test_request_image_block():
    p = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "what"},
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png", "data": "AAA"}},
    ]}]}
    parts = request(p)["messages"][0]["content"]
    assert parts[1] == {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAA"}}


def test_request_tools_and_tool_result():
    p = {"messages": [
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me"},
            {"type": "tool_use", "id": "t1", "name": "get", "input": {"q": 1}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "42"}]},
    ], "tools": [{"name": "get", "description": "d",
                  "input_schema": {"type": "object"}}]}
    o = request(p)
    asst = o["messages"][0]
    assert asst["content"] == "let me"
    assert asst["tool_calls"][0]["function"]["arguments"] == '{"q": 1}'
    assert o["messages"][1] == {"role": "tool", "tool_call_id": "t1", "content": "42"}
    assert o["tools"][0]["function"]["parameters"] == {"type": "object"}


def test_request_stream_adds_usage_option():
    o = request({"messages": [], "stream": True})
    assert o["stream"] is True and o["stream_options"] == {"include_usage": True}


def test_request_unknown_block_raises():
    with pytest.raises(ConversionError):
        request({"messages": [{"role": "user", "content": [
            {"type": "computer_use", "x": 1}]}]})


def test_response_text_and_tool_use():
    oa = {"id": "chatcmpl-1", "choices": [{"finish_reason": "tool_calls",
          "message": {"role": "assistant", "content": "ok",
                      "tool_calls": [{"id": "c1", "type": "function",
                                      "function": {"name": "get",
                                                   "arguments": '{"q":1}'}}]}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    a = response(oa, "claude-sonnet")
    assert a["type"] == "message" and a["model"] == "claude-sonnet"
    assert a["content"][0] == {"type": "text", "text": "ok"}
    assert a["content"][1] == {"type": "tool_use", "id": "c1",
                               "name": "get", "input": {"q": 1}}
    assert a["stop_reason"] == "tool_use"
    assert a["usage"] == {"input_tokens": 10, "output_tokens": 5}
