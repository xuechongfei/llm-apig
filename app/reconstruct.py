"""把 Anthropic SSE 事件流旁路还原成一条完整的 message JSON。

供日志落库使用：不参与转发，解析失败也不抛出，调用方直接忽略即可。
"""

import json


def _as_dict(value: object) -> dict:
    """只接受 dict；其余类型一律当作空 dict，避免越界访问。"""
    return value if isinstance(value, dict) else {}


def _as_index(value: object) -> int | None:
    """把 block index 规整成 int；无法转换时返回 None 以丢弃该事件。"""
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _str_or_empty(value: object) -> str:
    """只接受 str，用于拼接已有块字段，非 str 视为空串。"""
    return value if isinstance(value, str) else ""


def _parse_partial_json(raw: str) -> dict:
    """解析累积的 partial_json；坏 JSON / 非对象一律回退为空 dict。"""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        # 嵌套过深时 json 会抛 RecursionError；旁路日志器一律静默回退
        return {}
    return value if isinstance(value, dict) else {}


def parse_sse_event(raw_event: str) -> dict | None:
    """从一个 SSE 事件块中取出 data 并解析为 dict，失败返回 None。"""
    if not isinstance(raw_event, str):
        return None
    lines = [ln[5:] for ln in raw_event.splitlines() if ln.startswith("data:")]
    if not lines:
        return None
    data = "\n".join(lines).strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except (ValueError, RecursionError):
        # 嵌套过深时 json 会抛 RecursionError；旁路日志器一律静默跳过
        return None
    return obj if isinstance(obj, dict) else None


class AnthropicStreamReconstructor:
    """逐事件累积，最终给出 message_start 声明的整条消息。"""

    def __init__(self):
        self._message: dict | None = None
        self._blocks: dict[int, dict] = {}  # block index -> 内容块
        self._partial: dict[int, str] = {}  # block index -> 未解析 JSON

    def feed(self, event: dict) -> None:
        """喂入一个已解析事件；任何意外形状都不允许向上抛出。"""
        if not isinstance(event, dict):
            return
        try:
            self._dispatch(event)
        except Exception:
            # 旁路日志器不能影响转发：未知 payload 形状一律静默丢弃
            pass

    def _dispatch(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "message_start":
            self._on_message_start(event)
        elif etype == "content_block_start":
            self._on_block_start(event)
        elif etype == "content_block_delta":
            self._on_delta(event)
        elif etype == "content_block_stop":
            idx = _as_index(event.get("index"))
            if idx is not None:
                self._finalize_block(idx)
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
        msg = _as_dict(event.get("message"))
        # 实例可能被复用：清空上一轮的内容块与未解析 JSON
        self._blocks.clear()
        self._partial.clear()
        self._message = {
            "id": msg.get("id", ""),
            "type": "message",
            "role": msg.get("role", "assistant"),
            "model": msg.get("model", ""),
            "content": [],
            "stop_reason": msg.get("stop_reason"),
            "stop_sequence": msg.get("stop_sequence"),
            "usage": dict(_as_dict(msg.get("usage"))),
        }

    def _on_block_start(self, event: dict) -> None:
        idx = _as_index(event.get("index"))
        if idx is None:
            return
        self._message_or_init()
        block = dict(_as_dict(event.get("content_block")))
        self._blocks[idx] = block
        self._partial.pop(idx, None)

    def _on_delta(self, event: dict) -> None:
        idx = _as_index(event.get("index"))
        if idx is None:
            return
        self._message_or_init()
        delta = _as_dict(event.get("delta"))
        dtype = delta.get("type")
        block = self._blocks.get(idx)
        if block is None:  # 缺失 start 的 delta：按文本块兜底
            block = {"type": "text", "text": ""}
            self._blocks[idx] = block
        # 只拼接 str 载荷；null / 数字等异常值直接跳过
        if dtype == "text_delta":
            piece = delta.get("text")
            if isinstance(piece, str):
                block["text"] = _str_or_empty(block.get("text")) + piece
        elif dtype == "thinking_delta":
            piece = delta.get("thinking")
            if isinstance(piece, str):
                block["thinking"] = (
                    _str_or_empty(block.get("thinking")) + piece)
        elif dtype == "signature_delta":
            piece = delta.get("signature")
            if isinstance(piece, str):
                block["signature"] = (
                    _str_or_empty(block.get("signature")) + piece)
        elif dtype == "input_json_delta":
            piece = delta.get("partial_json")
            if isinstance(piece, str):
                self._partial[idx] = self._partial.get(idx, "") + piece

    def _finalize_block(self, idx: int) -> None:
        raw = self._partial.pop(idx, None)
        block = self._blocks.get(idx)
        if block is None or raw is None:
            return
        # tool_use 无 input 键也要补；server_tool_use 等有 input 就解析
        if "input" in block or block.get("type") == "tool_use":
            block["input"] = _parse_partial_json(raw)

    def _on_message_delta(self, event: dict) -> None:
        msg = self._message_or_init()
        delta = _as_dict(event.get("delta"))
        if delta.get("stop_reason") is not None:
            msg["stop_reason"] = delta["stop_reason"]
        if delta.get("stop_sequence") is not None:
            msg["stop_sequence"] = delta["stop_sequence"]
        usage = _as_dict(event.get("usage"))
        if usage:
            msg["usage"].update(usage)

    def to_json(self) -> dict | None:
        if self._message is None and not self._blocks:
            return None
        msg = self._message_or_init()
        for idx in list(self._partial):
            self._finalize_block(idx)
        content: list[dict] = []
        for idx in sorted(self._blocks):
            block = self._blocks[idx]
            if isinstance(block, dict):  # 尽力输出：跳过无法输出的块
                content.append(block)
        msg["content"] = content
        return msg
