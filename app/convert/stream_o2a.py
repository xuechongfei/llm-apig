import time

from app.convert.o2a import _STOP_MAP


class O2AStreamConverter:
    """Anthropic SSE 事件流 → OpenAI chat.completion.chunk 流（增量状态机）。"""

    def __init__(self, model: str):
        self.model = model
        self._id = "chatcmpl-gateway"
        self._created = int(time.time())
        self._tool_indices: dict[int, int] = {}  # anthropic block index -> oa index
        self._input_tokens = 0
        self._started = False

    def _chunk(self, delta: dict, finish_reason: str | None = None,
               usage: dict | None = None) -> dict:
        chunk = {"id": self._id, "object": "chat.completion.chunk",
                 "created": self._created, "model": self.model,
                 "choices": [{"index": 0, "delta": delta,
                              "finish_reason": finish_reason}]}
        if usage:
            chunk["usage"] = usage
        return chunk

    def feed(self, event: dict) -> list[dict]:
        etype = event.get("type")
        if etype == "message_start":
            self._started = True
            msg = event.get("message") or {}
            self._id = msg.get("id") or self._id
            self._input_tokens = (msg.get("usage") or {}).get("input_tokens", 0)
            return [self._chunk({"role": "assistant", "content": ""})]

        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                oa_idx = len(self._tool_indices)
                self._tool_indices[event.get("index", 0)] = oa_idx
                return [self._chunk({"tool_calls": [{
                    "index": oa_idx, "id": block.get("id"),
                    "type": "function",
                    "function": {"name": block.get("name", ""),
                                 "arguments": ""}}]})]
            return []

        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [self._chunk({"content": delta.get("text", "")})]
            if delta.get("type") == "input_json_delta":
                oa_idx = self._tool_indices.get(event.get("index", 0), 0)
                return [self._chunk({"tool_calls": [{
                    "index": oa_idx,
                    "function": {"arguments": delta.get("partial_json", "")}}]})]
            return []

        if etype == "message_delta":
            stop = (event.get("delta") or {}).get("stop_reason")
            output = (event.get("usage") or {}).get("output_tokens", 0)
            usage = {"prompt_tokens": self._input_tokens,
                     "completion_tokens": output,
                     "total_tokens": self._input_tokens + output}
            return [self._chunk({}, _STOP_MAP.get(stop, "stop"), usage)]

        return []  # content_block_stop / message_stop / ping 等忽略

    def finish(self) -> list[dict]:
        return []
