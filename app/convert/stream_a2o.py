from app.convert.a2o import _FINISH_MAP


class A2OStreamConverter:
    """OpenAI chat.completion.chunk 流 → Anthropic SSE 事件流（增量状态机）。"""

    def __init__(self, model: str):
        self.model = model
        self._started = False
        self._block_index = -1
        self._open: str | None = None  # "text" | "tool"
        self._tool_blocks: dict[int, int] = {}  # openai tool index -> block index
        self._stop_reason: str | None = None
        self._input_tokens = 0
        self._output_tokens = 0

    def _close_open(self, events: list[dict]) -> None:
        if self._open is not None:
            events.append({"type": "content_block_stop", "index": self._block_index})
            self._open = None

    def feed(self, chunk: dict) -> list[dict]:
        events: list[dict] = []
        if not self._started:
            self._started = True
            events.append({"type": "message_start", "message": {
                "id": chunk.get("id") or "msg_gateway",
                "type": "message", "role": "assistant", "content": [],
                "model": self.model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})

        usage = chunk.get("usage")
        if usage:
            self._input_tokens = usage.get("prompt_tokens", self._input_tokens)
            self._output_tokens = usage.get("completion_tokens", self._output_tokens)

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if self._open != "text":
                    self._close_open(events)
                    self._block_index += 1
                    self._open = "text"
                    events.append({"type": "content_block_start",
                                   "index": self._block_index,
                                   "content_block": {"type": "text", "text": ""}})
                events.append({"type": "content_block_delta",
                               "index": self._block_index,
                               "delta": {"type": "text_delta", "text": text}})

            for tc in delta.get("tool_calls") or []:
                oa_idx = tc.get("index", 0)
                if oa_idx not in self._tool_blocks:
                    self._close_open(events)
                    self._block_index += 1
                    self._tool_blocks[oa_idx] = self._block_index
                    self._open = "tool"
                    fn = tc.get("function") or {}
                    events.append({"type": "content_block_start",
                                   "index": self._block_index,
                                   "content_block": {
                                       "type": "tool_use",
                                       "id": tc.get("id") or f"toolu_{oa_idx}",
                                       "name": fn.get("name") or "",
                                       "input": {}}})
                args = (tc.get("function") or {}).get("arguments")
                if args:
                    events.append({"type": "content_block_delta",
                                   "index": self._tool_blocks[oa_idx],
                                   "delta": {"type": "input_json_delta",
                                             "partial_json": args}})

            if choice.get("finish_reason"):
                self._stop_reason = _FINISH_MAP.get(
                    choice["finish_reason"], "end_turn")

        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        if not self._started:
            return events
        self._close_open(events)
        events.append({"type": "message_delta",
                       "delta": {"stop_reason": self._stop_reason or "end_turn",
                                 "stop_sequence": None},
                       "usage": {"output_tokens": self._output_tokens}})
        events.append({"type": "message_stop"})
        return events
