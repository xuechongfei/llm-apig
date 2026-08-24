import json


class ConversionError(Exception):
    pass


_FINISH_MAP = {"stop": "end_turn", "length": "max_tokens",
               "tool_calls": "tool_use", "content_filter": "stop_sequence"}


def _blocks_to_parts(blocks: list) -> list[dict]:
    parts = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            src = b.get("source", {})
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type')};base64,{src.get('data')}"
            else:
                url = src.get("url", "")
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif t in ("thinking", "redacted_thinking"):
            continue  # 跨协议丢弃
        else:
            raise ConversionError(f"block type {t!r} 不支持跨协议转换")
    return parts


def _convert_message(m: dict) -> list[dict]:
    content = m.get("content")
    if isinstance(content, str):
        return [{"role": m["role"], "content": content}]

    out: list[dict] = []
    text_image_blocks, tool_uses = [], []
    for b in content:
        if b.get("type") == "tool_use":
            tool_uses.append(b)
        elif b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "".join(x.get("text", "") for x in c if x.get("type") == "text")
            out.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                        "content": c or ""})
        else:
            text_image_blocks.append(b)

    msg: dict = {"role": m["role"]}
    if text_image_blocks:
        parts = _blocks_to_parts(text_image_blocks)
        if len(parts) == 1 and parts[0]["type"] == "text":
            msg["content"] = parts[0]["text"]
        else:
            msg["content"] = parts
    if tool_uses:
        msg.setdefault("content", "")
        msg["tool_calls"] = [
            {"id": t["id"], "type": "function",
             "function": {"name": t["name"],
                          "arguments": json.dumps(t.get("input", {}),
                                                  ensure_ascii=False)}}
            for t in tool_uses
        ]
    if msg.get("content") or msg.get("tool_calls"):
        out.insert(0, msg) if tool_uses and out else out.append(msg)
    return out


def request(payload: dict) -> dict:
    messages: list[dict] = []
    system = payload.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(b.get("text", "") for b in system if b.get("type") == "text")
        if text:
            messages.append({"role": "system", "content": text})

    for m in payload.get("messages", []):
        messages.extend(_convert_message(m))

    out: dict = {"model": payload.get("model"), "messages": messages}
    for a, o in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                 ("top_p", "top_p"), ("stop_sequences", "stop"),
                 ("stream", "stream")):
        if payload.get(a) is not None:
            out[o] = payload[a]
    if payload.get("stream"):
        out["stream_options"] = {"include_usage": True}
    if payload.get("tools"):
        out["tools"] = [
            {"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {})}}
            for t in payload["tools"]
        ]
    tc = payload.get("tool_choice")
    if tc:
        kind = tc.get("type")
        if kind in ("auto", "any", "none"):
            out["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}[kind]
        elif kind == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc.get("name")}}
    return out


def response(oa: dict, model: str) -> dict:
    choice = oa["choices"][0]
    msg = choice["message"]
    content: list[dict] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        content.append({"type": "tool_use", "id": tc["id"],
                        "name": tc["function"]["name"], "input": args})
    usage = oa.get("usage") or {}
    return {
        "id": oa.get("id", ""), "type": "message", "role": "assistant",
        "content": content, "model": model,
        "stop_reason": _FINISH_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }
