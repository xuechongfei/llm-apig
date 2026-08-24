import json
import re

from app.convert.a2o import ConversionError

_STOP_MAP = {"end_turn": "stop", "max_tokens": "length",
             "tool_use": "tool_calls", "stop_sequence": "stop"}

_DATA_URI = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)


def _parts_to_blocks(parts: list) -> list[dict]:
    blocks = []
    for p in parts:
        t = p.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": p.get("text", "")})
        elif t == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            m = _DATA_URI.match(url)
            if m:
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": m.group(1),
                    "data": m.group(2)}})
            else:
                blocks.append({"type": "image", "source": {
                    "type": "url", "url": url}})
        else:
            raise ConversionError(f"part type {t!r} 不支持跨协议转换")
    return blocks


def request(payload: dict) -> dict:
    system_texts: list[str] = []
    messages: list[dict] = []

    for m in payload.get("messages", []):
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_texts.append(m["content"])
            continue
        if role == "tool":
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                 "content": m.get("content") or ""}]})
            continue
        content = m.get("content")
        tool_calls = m.get("tool_calls") or []
        if isinstance(content, str) and not tool_calls:
            # 纯文本消息原样保留（Anthropic 也接受字符串 content）
            messages.append({"role": role, "content": content})
            continue
        blocks: list[dict] = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            blocks.extend(_parts_to_blocks(content))
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append({"type": "tool_use", "id": tc["id"],
                           "name": tc["function"]["name"], "input": args})
        messages.append({"role": role,
                         "content": blocks if blocks else (content or "")})

    out: dict = {
        "model": payload.get("model"),
        "messages": messages,
        "max_tokens": payload.get("max_tokens")
        or payload.get("max_completion_tokens") or 4096,
    }
    if system_texts:
        out["system"] = "\n".join(system_texts)
    for o, a in (("temperature", "temperature"), ("top_p", "top_p"),
                 ("stop", "stop_sequences"), ("stream", "stream")):
        if payload.get(o) is not None:
            out[a] = payload[o]
    if isinstance(out.get("stop_sequences"), str):
        out["stop_sequences"] = [out["stop_sequences"]]
    if payload.get("tools"):
        out["tools"] = [
            {"name": t["function"]["name"],
             "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters", {})}
            for t in payload["tools"]
        ]
    tc = payload.get("tool_choice")
    if isinstance(tc, str):
        out["tool_choice"] = {"auto": {"type": "auto"},
                              "required": {"type": "any"},
                              "none": {"type": "none"}}.get(tc, {"type": "auto"})
    elif isinstance(tc, dict) and tc.get("type") == "function":
        out["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
    return out


def response(a: dict, model: str) -> dict:
    text_parts, tool_calls = [], []
    for b in a.get("content", []):
        if b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            tool_calls.append({
                "id": b["id"], "type": "function",
                "function": {"name": b["name"],
                             "arguments": json.dumps(b.get("input", {}),
                                                     ensure_ascii=False)}})
    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = a.get("usage") or {}
    return {
        "id": a.get("id", ""), "object": "chat.completion", "created": 0,
        "model": model,
        "choices": [{
            "index": 0, "message": message,
            "finish_reason": _STOP_MAP.get(a.get("stop_reason"), "stop"),
        }],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                  "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("input_tokens", 0)
                  + usage.get("output_tokens", 0)},
    }
