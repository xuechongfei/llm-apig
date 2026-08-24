from app.convert.o2a import request, response


def test_request_system_extracted():
    o = {"model": "ds", "messages": [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hi"}]}
    a = request(o)
    assert a["system"] == "be nice"
    assert a["messages"] == [{"role": "user", "content": "hi"}]
    assert a["max_tokens"] == 4096  # 默认值


def test_request_tool_calls_and_tool_role():
    o = {"model": "ds", "messages": [
        {"role": "assistant", "content": "let me", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get", "arguments": '{"q": 1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "42"}]}
    a = request(o)
    asst = a["messages"][0]["content"]
    assert asst[0] == {"type": "text", "text": "let me"}
    assert asst[1] == {"type": "tool_use", "id": "c1", "name": "get",
                       "input": {"q": 1}}
    assert a["messages"][1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": "42"}]}


def test_request_image_url_data_uri():
    o = {"model": "ds", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]}
    block = request(o)["messages"][0]["content"][0]
    assert block == {"type": "image", "source": {"type": "base64",
                     "media_type": "image/png", "data": "AAA"}}


def test_request_tools():
    o = {"model": "ds", "messages": [], "tools": [
        {"type": "function", "function": {"name": "get", "description": "d",
                                          "parameters": {"type": "object"}}}]}
    a = request(o)
    assert a["tools"] == [{"name": "get", "description": "d",
                           "input_schema": {"type": "object"}}]


def test_response_to_openai():
    a = {"id": "msg_1", "type": "message", "role": "assistant",
         "content": [{"type": "text", "text": "hi"},
                     {"type": "tool_use", "id": "t1", "name": "get",
                      "input": {"q": 1}}],
         "stop_reason": "tool_use",
         "usage": {"input_tokens": 3, "output_tokens": 7}}
    o = response(a, "ds")
    assert o["object"] == "chat.completion" and o["model"] == "ds"
    msg = o["choices"][0]["message"]
    assert msg["content"] == "hi"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"q": 1}'
    assert o["choices"][0]["finish_reason"] == "tool_calls"
    assert o["usage"] == {"prompt_tokens": 3, "completion_tokens": 7,
                          "total_tokens": 10}
