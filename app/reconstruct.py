"""把 Anthropic SSE 事件流旁路还原成一条完整的 message JSON。

供日志落库使用：不参与转发，解析失败也不抛出，调用方直接忽略即可。
"""

import json


def parse_sse_event(raw_event: str) -> dict | None:
    """从一个 SSE 事件块中取出 data 并解析为 dict，失败返回 None。"""
    lines = [ln[5:] for ln in raw_event.splitlines() if ln.startswith("data:")]
    if not lines:
        return None
    data = "\n".join(lines).strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class AnthropicStreamReconstructor:
    """逐事件累积，最终给出 message_start 声明的整条消息。"""

    def __init__(self):
        self._message: dict | None = None
        self._blocks: dict[int, dict] = {}  # block index -> 内容块
        self._partial: dict[int, str] = {}  # block index -> 未解析的 JSON

    def feed(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "message_start":
            self._on_message_start(event)
        elif etype == "content_block_start":
            self._on_block_start(event)
        elif etype == "content_block_delta":
            self._on_delta(event)
        elif etype == "content_block_stop":
            self._finalize_block(event.get("index", 0))
        elif etype == "message_delta":
            self._on_message_delta(event)

    def _message_or_init(self) -> dict:
        if self._message is None:
            self._message = {
                "id": "", "type": "message", "role": "assistant", "model": "",
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {},
            }
        return self._message

    def _on_message_start(self, event: dict) -> None:
        msg = event.get("message") or {}
        self._message = {
            "id": msg.get("id", ""),
            "type": "message",
            "role": msg.get("role", "assistant"),
            "model": msg.get("model", ""),
            "content": [],
            "stop_reason": msg.get("stop_reason"),
            "stop_sequence": msg.get("stop_sequence"),
            "usage": dict(msg.get("usage") or {}),
        }

    def _on_block_start(self, event: dict) -> None:
        self._message_or_init()
        idx = event.get("index", 0)
        block = dict(event.get("content_block") or {})
        self._blocks[idx] = block
        self._partial.pop(idx, None)

    def _on_delta(self, event: dict) -> None:
        self._message_or_init()
        idx = event.get("index", 0)
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        block = self._blocks.get(idx)
        if block is None:  # 缺失 start 的 delta：按文本块兜底
            block = {"type": "text", "text": ""}
            self._blocks[idx] = block
        if dtype == "text_delta":
            block["text"] = block.get("text", "") + delta.get("text", "")
        elif dtype == "thinking_delta":
            block["thinking"] = (block.get("thinking", "")
                                 + delta.get("thinking", ""))
        elif dtype == "signature_delta":
            block["signature"] = (block.get("signature", "")
                                  + delta.get("signature", ""))
        elif dtype == "input_json_delta":
            self._partial[idx] = (self._partial.get(idx, "")
                                  + delta.get("partial_json", ""))

    def _finalize_block(self, idx: int) -> None:
        raw = self._partial.pop(idx, None)
        block = self._blocks.get(idx)
        if block is None or raw is None:
            return
        if block.get("type") == "tool_use":
            try:
                block["input"] = json.loads(raw) if raw.strip() else {}
            except ValueError:
                block["input"] = {}

    def _on_message_delta(self, event: dict) -> None:
        msg = self._message_or_init()
        delta = event.get("delta") or {}
        if delta.get("stop_reason") is not None:
            msg["stop_reason"] = delta["stop_reason"]
        if delta.get("stop_sequence") is not None:
            msg["stop_sequence"] = delta["stop_sequence"]
        usage = event.get("usage") or {}
        if usage:
            msg["usage"].update(usage)

    def to_json(self) -> dict | None:
        if self._message is None and not self._blocks:
            return None
        msg = self._message_or_init()
        for idx in list(self._partial):
            self._finalize_block(idx)
        msg["content"] = [self._blocks[i] for i in sorted(self._blocks)]
        return msg
