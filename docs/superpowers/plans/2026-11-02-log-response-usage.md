# 日志耗时 / 出参 / 缓存 token 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让请求日志以秒展示耗时、记录并格式化展示入参/出参，并采集展示 Anthropic 缓存读取/写入 token（流式与非流式都支持）。

**Architecture:** 新增两个纯函数模块——`app/usage.py` 归一化上游 usage、`app/reconstruct.py` 旁路把 SSE 事件还原成完整 message JSON；`app/executor.py` 在转发的同时采集，落库到 `request_log` 新增的三列；管理端模板用两个 Jinja 过滤器做秒显示与 JSON 格式化。

**Tech Stack:** Python 3.11、FastAPI、httpx、SQLite、Jinja2、pytest（`asyncio_mode=auto`）

## Global Constraints

- Python `>=3.11`；不新增第三方依赖。
- 所有测试用 `uv run pytest tests/ -v` 运行；改动前基线为 `115 passed`。
- 代码注释与界面文案使用中文，与现有代码风格一致（行宽约 79 列，类型注解用 `X | None`）。
- 数据库只通过 `app/db.py` 的 `MIGRATIONS` 列表做增量迁移，不改写已有 `SCHEMA` 语句。
- 原始 SSE 继续原样转发给客户端；采集必须是旁路，任何解析异常都不能影响转发。
- 提交信息用中文 conventional commit 前缀（`feat:` / `fix:` / `test:` / `docs:`）。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `app/usage.py`（新建） | 上游 usage → 网关统一四字段；反向转回 Anthropic wire 格式 |
| `app/reconstruct.py`（新建） | SSE 事件旁路还原为完整 Anthropic message JSON；SSE 块解析 |
| `app/db.py` | 新增迁移 v2→v3（三列） |
| `app/logging_.py` | `finish_log` 支持新字段 |
| `app/executor.py` | 非流式/流式采集 usage 与 `response_body` |
| `app/convert/stream_a2o.py` | 暴露累计 usage（含上游缓存字段） |
| `app/admin/routes.py` | `dur` / `pretty` 两个 Jinja 过滤器 |
| `app/admin/templates/logs.html`、`dashboard.html`、`log_detail.html` | 展示层 |
| `tests/test_usage.py`、`tests/test_reconstruct.py`（新建） | 新模块单测 |
| `tests/test_db.py`、`tests/test_logging.py`、`tests/test_executor.py`、`tests/test_admin_logs_settings.py` | 扩展 |

---

### Task 1: usage 归一化模块

**Files:**
- Create: `app/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `FIELDS: tuple[str, ...]` = `("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")`
  - `empty() -> dict`，四个键全部为 `None`
  - `from_anthropic_usage(usage: dict | None) -> dict`
  - `from_openai_usage(usage: dict | None) -> dict`
  - `to_anthropic_usage(usage: dict) -> dict` —— 归一化字典转回 Anthropic wire 形（`cache_read_input_tokens` 等），值为 `None` 的键省略

- [ ] **Step 1: 写失败测试 `tests/test_usage.py`**

```python
from app.usage import (empty, from_anthropic_usage, from_openai_usage,
                       to_anthropic_usage)


def test_anthropic_usage_maps_cache_fields():
    u = from_anthropic_usage({
        "input_tokens": 12, "output_tokens": 34,
        "cache_read_input_tokens": 56,
        "cache_creation_input_tokens": 78})
    assert u == {"input_tokens": 12, "output_tokens": 34,
                 "cache_read_tokens": 56, "cache_creation_tokens": 78}


def test_anthropic_usage_missing_fields_are_none():
    assert from_anthropic_usage({"input_tokens": 1}) == {
        "input_tokens": 1, "output_tokens": None,
        "cache_read_tokens": None, "cache_creation_tokens": None}
    assert from_anthropic_usage(None) == empty()


def test_openai_usage_cached_tokens_details():
    u = from_openai_usage({
        "prompt_tokens": 100, "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 80}})
    assert u["input_tokens"] == 100 and u["output_tokens"] == 5
    assert u["cache_read_tokens"] == 80
    assert u["cache_creation_tokens"] is None


def test_openai_usage_prompt_cache_hit_fallback():
    u = from_openai_usage({"prompt_tokens": 9, "completion_tokens": 1,
                           "prompt_cache_hit_tokens": 7})
    assert u["cache_read_tokens"] == 7


def test_openai_usage_without_usage_block():
    assert from_openai_usage({}) == empty()


def test_to_anthropic_usage_omits_none():
    assert to_anthropic_usage({
        "input_tokens": 3, "output_tokens": 4,
        "cache_read_tokens": 5, "cache_creation_tokens": None}) == {
        "input_tokens": 3, "output_tokens": 4,
        "cache_read_input_tokens": 5}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_usage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage'`

- [ ] **Step 3: 实现 `app/usage.py`**

```python
"""上游 usage 与网关统一字段之间的换算。

Anthropic 与 OpenAI 的输入语义不同：
- Anthropic：input_tokens 不含缓存部分，三者并列；
- OpenAI：prompt_tokens 含缓存部分。
两者都按上游原样记录 input_tokens，不做加减换算，避免误导。
"""

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
          "cache_creation_tokens")


def empty() -> dict:
    return dict.fromkeys(FIELDS)


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def from_anthropic_usage(usage: dict | None) -> dict:
    usage = usage or {}
    return {
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
        "cache_read_tokens": _int_or_none(
            usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": _int_or_none(
            usage.get("cache_creation_input_tokens")),
    }


def from_openai_usage(usage: dict | None) -> dict:
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens")
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens")
    return {
        "input_tokens": _int_or_none(usage.get("prompt_tokens")),
        "output_tokens": _int_or_none(usage.get("completion_tokens")),
        "cache_read_tokens": _int_or_none(cached),
        "cache_creation_tokens": None,
    }


def to_anthropic_usage(usage: dict) -> dict:
    """归一化字典 → Anthropic wire 形，None 值省略。"""
    pairs = (("input_tokens", "input_tokens"),
             ("output_tokens", "output_tokens"),
             ("cache_read_input_tokens", "cache_read_tokens"),
             ("cache_creation_input_tokens", "cache_creation_tokens"))
    return {wire: usage[key] for wire, key in pairs
            if usage.get(key) is not None}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_usage.py -v`
Expected: `6 passed`

- [ ] **Step 5: 提交**

```bash
git add app/usage.py tests/test_usage.py
git commit -m "feat: 新增 usage 归一化模块（含 Anthropic 缓存字段）"
```

---

### Task 2: 流式出参重建器

**Files:**
- Create: `app/reconstruct.py`
- Test: `tests/test_reconstruct.py`

**Interfaces:**
- Consumes: 无（不依赖 `app.usage`，usage 原样透传）
- Produces:
  - `parse_sse_event(raw_event: str) -> dict | None` —— 从一个 SSE 事件块提取 data 并解析；无 data、`[DONE]`、非 JSON、非 dict 均返回 `None`
  - `class AnthropicStreamReconstructor`：
    - `feed(event: dict) -> None`
    - `to_json() -> dict | None` —— 从未收到任何事件时返回 `None`；否则返回完整 message，`content` 按 block index 排序，`tool_use.input` 由 partial_json 解析（坏 JSON → `{}`）

- [ ] **Step 1: 写失败测试 `tests/test_reconstruct.py`**

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_reconstruct.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.reconstruct'`

- [ ] **Step 3: 实现 `app/reconstruct.py`**

```python
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_reconstruct.py -v`
Expected: `8 passed`

- [ ] **Step 5: 提交**

```bash
git add app/reconstruct.py tests/test_reconstruct.py
git commit -m "feat: 新增 Anthropic 流式出参重建器"
```

---

### Task 3: 数据库迁移与日志写入

**Files:**
- Modify: `app/db.py:77-100`（`MIGRATIONS` 追加一条）
- Modify: `app/logging_.py:16-23`（`finish_log`）
- Test: `tests/test_db.py`、`tests/test_logging.py`

**Interfaces:**
- Consumes: 无
- Produces: `finish_log(conn, log_id, *, status: str, total_ms: int, input_tokens: int | None, output_tokens: int | None, cache_read_tokens: int | None = None, cache_creation_tokens: int | None = None, response_body: str | None = None) -> None`

- [ ] **Step 1: 写失败测试**

在 `tests/test_db.py` 末尾追加：

```python
def test_migration_2_to_3_adds_response_columns(tmp_path, monkeypatch):
    """v2 → v3：request_log 增加响应报文与缓存 token 列，旧数据保留"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    conn.executescript(db.SCHEMA)  # 基础表（v2 形态，无新列）
    db.set_setting(conn, "schema_version", "2")
    conn.execute("INSERT INTO request_log (created_at,client_protocol,"
                 "group_name,path,status) VALUES"
                 " (1.0,'anthropic','g','/v1/messages','success')")
    conn.commit()
    conn.close()

    db.init_db()

    conn = db.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(request_log)")}
    assert {"response_body", "cache_read_tokens",
            "cache_creation_tokens"} <= cols
    row = conn.execute("SELECT * FROM request_log WHERE id=1").fetchone()
    assert row["status"] == "success" and row["response_body"] is None
    assert db.get_setting(conn, "schema_version", "0") == str(
        len(db.MIGRATIONS))
    conn.close()
```

在 `tests/test_logging.py` 的 `test_log_lifecycle` 之后追加：

```python
def test_finish_log_records_response_and_cache(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    log_id = create_log(conn, client_protocol="anthropic",
                        group_name="g", path="/v1/messages", stream=False)
    finish_log(conn, log_id, status="success", total_ms=1500,
               input_tokens=10, output_tokens=20,
               cache_read_tokens=900, cache_creation_tokens=50,
               response_body='{"ok": true}')
    log = conn.execute("SELECT * FROM request_log WHERE id=?",
                       (log_id,)).fetchone()
    assert log["total_duration_ms"] == 1500
    assert log["cache_read_tokens"] == 900
    assert log["cache_creation_tokens"] == 50
    assert log["response_body"] == '{"ok": true}'
    conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_db.py::test_migration_2_to_3_adds_response_columns tests/test_logging.py::test_finish_log_records_response_and_cache -v`
Expected: FAIL — `sqlite3.OperationalError: no such column: response_body` / `TypeError: finish_log() got an unexpected keyword argument 'cache_read_tokens'`

- [ ] **Step 3: 修改 `app/db.py` 的 `MIGRATIONS`**

在 `MIGRATIONS` 列表末尾（现有 v1→v2 那条字符串之后）追加：

```python
    # version 2 → 3: 请求日志增加响应报文与缓存 token
    """
    ALTER TABLE request_log ADD COLUMN response_body TEXT;
    ALTER TABLE request_log ADD COLUMN cache_read_tokens INTEGER;
    ALTER TABLE request_log ADD COLUMN cache_creation_tokens INTEGER;
    """,
```

注意：不要把这些列加进 `SCHEMA` 的 `CREATE TABLE request_log`——新库靠迁移补齐，加进 `SCHEMA` 会让 `ALTER TABLE` 在新库上重复加列而报错。

- [ ] **Step 4: 修改 `app/logging_.py` 的 `finish_log`**

```python
def finish_log(conn, log_id: int, *, status: str, total_ms: int,
               input_tokens: int | None, output_tokens: int | None,
               cache_read_tokens: int | None = None,
               cache_creation_tokens: int | None = None,
               response_body: str | None = None) -> None:
    conn.execute(
        "UPDATE request_log SET status=?, total_duration_ms=?,"
        " input_tokens=?, output_tokens=?, cache_read_tokens=?,"
        " cache_creation_tokens=?, response_body=? WHERE id=?",
        (status, total_ms, input_tokens, output_tokens, cache_read_tokens,
         cache_creation_tokens, response_body, log_id),
    )
    conn.commit()
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `uv run pytest tests/test_db.py tests/test_logging.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add app/db.py app/logging_.py tests/test_db.py tests/test_logging.py
git commit -m "feat: 日志表记录响应报文与缓存 token"
```

---

### Task 4: 非流式采集

**Files:**
- Modify: `app/executor.py:200-237`（`_try_non_stream`）
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `app.usage.from_anthropic_usage` / `from_openai_usage`；`finish_log` 的新参数
- Produces: 无（内部行为）

- [ ] **Step 1: 写失败测试**

在 `tests/test_executor.py` 末尾追加：

```python
async def test_non_stream_records_response_and_cache_tokens(
        tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    transport, _ = _mock([(200, {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 4,
                  "cache_read_input_tokens": 900,
                  "cache_creation_input_tokens": 50}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 200
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["input_tokens"] == 10 and log["output_tokens"] == 4
    assert log["cache_read_tokens"] == 900
    assert log["cache_creation_tokens"] == 50
    body = json.loads(log["response_body"])
    assert body["content"] == [{"type": "text", "text": "hi"}]
    conn.close()


async def test_openai_entry_non_stream_maps_cached_tokens(
        tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "oa", "openai", 1, model="gpt-x")
    transport, _ = _mock([(200, {
        "id": "c1", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 8,
                  "prompt_tokens_details": {"cached_tokens": 64}}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="openai", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 200
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["input_tokens"] == 100 and log["output_tokens"] == 8
    assert log["cache_read_tokens"] == 64
    assert json.loads(log["response_body"])["choices"][0]["message"][
        "content"] == "hi"
    conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_executor.py::test_non_stream_records_response_and_cache_tokens tests/test_executor.py::test_openai_entry_non_stream_maps_cached_tokens -v`
Expected: FAIL — `cache_read_tokens` 为 `None`

- [ ] **Step 3: 修改 `_try_non_stream`**

文件顶部 import 区加入：

```python
from app import usage as usage_mod
from app.reconstruct import AnthropicStreamReconstructor, parse_sse_event
```

（`AnthropicStreamReconstructor`、`parse_sse_event` 在 Task 6 才用到，本步一并加入 import 不影响运行。）

把 `_try_non_stream` 中 `if resp.status_code < 400:` 分支改为：

```python
    if resp.status_code < 400:
        body = resp.json()
        u = (usage_mod.from_anthropic_usage(body.get("usage"))
             if cand.protocol == "anthropic"
             else usage_mod.from_openai_usage(body.get("usage")))
        if cand.needs_conversion:
            body = (o2a.response(body, group_name) if entry_protocol == "openai"
                    else a2o.response(body, group_name))
        add_attempt(conn, log_id, channel_id=cand.channel_id,
                    channel_name=cand.channel_name,
                    actual_model=cand.actual_model,
                    converted=cand.needs_conversion, http_status=resp.status_code)
        finish_log(conn, log_id, status="success", total_ms=_elapsed(started),
                   input_tokens=u["input_tokens"],
                   output_tokens=u["output_tokens"],
                   cache_read_tokens=u["cache_read_tokens"],
                   cache_creation_tokens=u["cache_creation_tokens"],
                   response_body=_sanitize_body(body))
        return GatewayResult(resp.status_code, body=body), (resp.status_code, body)
```

说明：usage 必须从**转换前**的原始 body 提取，否则 OpenAI 上游的 `prompt_tokens_details` 在 `a2o.response` 里会被丢掉。

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_executor.py -v`
Expected: 全部 PASS（含原有 8 个）

- [ ] **Step 5: 提交**

```bash
git add app/executor.py tests/test_executor.py
git commit -m "feat: 非流式请求记录出参与缓存 token"
```

---

### Task 5: 协议转换器暴露累计用量

**Files:**
- Modify: `app/convert/stream_a2o.py`
- Test: `tests/convert/test_stream_a2o.py`

**Interfaces:**
- Consumes: `app.usage.from_openai_usage`
- Produces: `A2OStreamConverter.usage() -> dict` —— 归一化四字段（`input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens`）；上游未发送 usage 时四值均为 `None`

- [ ] **Step 1: 写失败测试**

在 `tests/convert/test_stream_a2o.py` 末尾追加：

```python
def test_usage_exposes_cache_tokens():
    c = A2OStreamConverter("m")
    c.feed({"id": "c1", "choices": [], "usage": {
        "prompt_tokens": 100, "completion_tokens": 8,
        "prompt_tokens_details": {"cached_tokens": 64}}})
    assert c.usage() == {"input_tokens": 100, "output_tokens": 8,
                         "cache_read_tokens": 64,
                         "cache_creation_tokens": None}


def test_usage_empty_when_upstream_sends_none():
    c = A2OStreamConverter("m")
    c.feed({"id": "c1", "choices": [{"index": 0, "delta": {"content": "hi"},
                                     "finish_reason": None}]})
    assert c.usage() == {"input_tokens": None, "output_tokens": None,
                         "cache_read_tokens": None,
                         "cache_creation_tokens": None}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/convert/test_stream_a2o.py -v`
Expected: FAIL — `AttributeError: 'A2OStreamConverter' object has no attribute 'usage'`

- [ ] **Step 3: 给 `A2OStreamConverter` 加 usage 累计**

修改 `app/convert/stream_a2o.py`：

顶部 import 区改为：

```python
from app.convert.a2o import _FINISH_MAP
from app.usage import from_openai_usage
```

`__init__` 中 `self._output_tokens = 0` 之后增加：

```python
        self._cached_tokens: int | None = None
```

`feed()` 中处理 usage 的代码块改为：

```python
        usage = chunk.get("usage")
        if usage:
            self._input_tokens = usage.get("prompt_tokens", self._input_tokens)
            self._output_tokens = usage.get("completion_tokens",
                                            self._output_tokens)
            details = usage.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens")
            if cached is None:
                cached = usage.get("prompt_cache_hit_tokens")
            if cached is not None:
                self._cached_tokens = cached
```

类末尾（`finish` 之后）增加：

```python
    def usage(self) -> dict:
        """上游 include_usage 尾包中的累计用量，映射为网关统一字段。"""
        raw = {"prompt_tokens": self._input_tokens or None,
               "completion_tokens": self._output_tokens or None}
        if self._cached_tokens is not None:
            raw["prompt_tokens_details"] = {"cached_tokens": self._cached_tokens}
        return from_openai_usage(raw)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/convert/ -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/convert/stream_a2o.py tests/convert/test_stream_a2o.py
git commit -m "feat: 协议转换器暴露累计用量与缓存 token"
```

---

### Task 6: 流式采集接线（直通 + 转换）

**Files:**
- Modify: `app/executor.py:259-355`（`_try_stream` 的 `events()`，并删除 `_sse_data`）
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `AnthropicStreamReconstructor`、`parse_sse_event`（Task 2）、`A2OStreamConverter.usage()`（Task 5）、`finish_log` 新参数（Task 3）
- Produces: 无（内部行为）

- [ ] **Step 1: 写失败测试**

在 `tests/test_executor.py` 末尾追加两个测试——直通与转换各一：

```python
async def test_stream_records_usage_cache_and_response(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    sse = (
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"id":"m","model":"m","usage":{"input_tokens":3,"output_tokens":1,'
        b'"cache_read_input_tokens":77,"cache_creation_input_tokens":5}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start",'
        b'"index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
        b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: content_block_stop\ndata: {"type":"content_block_stop",'
        b'"index":0}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta",'
        b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n')

    def handler(request):
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": [], "stream": True},
                      stream=True)
    forwarded = "".join([e async for e in r.events])
    assert forwarded == sse.decode()  # 转发内容一字不改
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["status"] == "success"
    assert log["input_tokens"] == 3 and log["output_tokens"] == 9
    assert log["cache_read_tokens"] == 77
    assert log["cache_creation_tokens"] == 5
    body = json.loads(log["response_body"])
    assert body["content"] == [{"type": "text", "text": "hi"}]
    assert body["usage"]["cache_read_input_tokens"] == 77
    conn.close()


async def test_stream_conversion_records_usage_and_response(
        tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "ds", "openai", 1, model="deepseek-chat")
    sse = (b'data: {"id":"c1","choices":[{"index":0,"delta":'
           b'{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n'
           b'data: {"id":"c1","choices":[{"index":0,"delta":{},'
           b'"finish_reason":"stop"}]}\n\n'
           b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":12,'
           b'"completion_tokens":6,"prompt_tokens_details":'
           b'{"cached_tokens":8}}}\n\n'
           b'data: [DONE]\n\n')

    def handler(request):
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": [], "stream": True},
                      stream=True)
    forwarded = "".join([e async for e in r.events])
    assert "text_delta" in forwarded
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["status"] == "success"
    assert log["input_tokens"] == 12 and log["output_tokens"] == 6
    assert log["cache_read_tokens"] == 8
    body = json.loads(log["response_body"])
    assert body["content"][0]["text"] == "hi"
    assert body["usage"]["input_tokens"] == 12
    assert body["usage"]["cache_read_input_tokens"] == 8
    conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_executor.py::test_stream_records_usage_cache_and_response tests/test_executor.py::test_stream_conversion_records_usage_and_response -v`
Expected: FAIL — `log["input_tokens"]` 为 `None`，`json.loads(None)` 抛 `TypeError`

- [ ] **Step 3: 重写 `events()`，删除 `_sse_data`**

将 `_try_stream` 中从 `async def events()` 到 `return GatewayResult(200, events=events()), (200, {})` 整段替换为：

```python
    async def events() -> AsyncIterator[str]:
        failed_mid = None
        # OpenAI 入口的流式采集不在本轮范围，recon 为 None 时保持现状
        recon = (AnthropicStreamReconstructor()
                 if entry_protocol == "anthropic" else None)
        conv_usage = None
        try:
            if not cand.needs_conversion:
                buf = ""
                async for chunk in resp.aiter_text():
                    yield chunk  # 原样转发
                    if recon is None:
                        continue
                    buf += chunk
                    while "\n\n" in buf:
                        raw_event, buf = buf.split("\n\n", 1)
                        event = parse_sse_event(raw_event)
                        if event is not None:
                            recon.feed(event)
            else:
                converter = (stream_o2a.O2AStreamConverter(group_name)
                             if cand.protocol == "anthropic"
                             else stream_a2o.A2OStreamConverter(group_name))
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        raw_event, buf = buf.split("\n\n", 1)
                        obj = parse_sse_event(raw_event)
                        if obj is None:
                            continue
                        for event in converter.feed(obj):
                            if recon is not None:
                                recon.feed(event)
                            yield render_sse([event], entry_protocol)[0]
                for event in converter.finish():
                    if recon is not None:
                        recon.feed(event)
                    yield render_sse([event], entry_protocol)[0]
                if entry_protocol == "openai":
                    yield "data: [DONE]\n\n"
                if recon is not None:
                    conv_usage = converter.usage()
        except httpx.HTTPError as e:
            failed_mid = str(e)
            for out in render_sse(
                    [{"type": "error", "error": {
                        "type": "api_error",
                        "message": f"上游流中断: {e}"}}], entry_protocol):
                yield out
            if entry_protocol == "openai":
                yield "data: [DONE]\n\n"
        finally:
            await resp.aclose()
            await client.aclose()
            # 流式生成器的生命周期可能长于路由层传入的连接：新开连接写日志
            wconn = connect()
            try:
                status = "success" if failed_mid is None else "failed"
                if recon is not None:
                    final = recon.to_json()
                    if final is not None and conv_usage is not None:
                        final["usage"] = usage_mod.to_anthropic_usage(conv_usage)
                    response_body = _sanitize_body(final) if final else None
                    u = (conv_usage
                         or usage_mod.from_anthropic_usage(
                             (final or {}).get("usage")))
                    finish_log(wconn, log_id, status=status,
                               total_ms=_elapsed(started),
                               input_tokens=u["input_tokens"],
                               output_tokens=u["output_tokens"],
                               cache_read_tokens=u["cache_read_tokens"],
                               cache_creation_tokens=u["cache_creation_tokens"],
                               response_body=response_body)
                else:
                    finish_log(wconn, log_id, status=status,
                               total_ms=_elapsed(started),
                               input_tokens=None, output_tokens=None)
                if failed_mid is not None:
                    verdict = classify_error(None, failed_mid)
                    _record_failure(wconn, log_id, cand, verdict, None,
                                    failed_mid)
            finally:
                wconn.close()

    return GatewayResult(200, events=events()), (200, {})
```

同时删除文件末尾已无人调用的 `_sse_data` 函数：

```python
def _sse_data(raw_event: str) -> str | None:
    """从一个 SSE 事件块中提取 data 字段（可能多行）。"""
    lines = [ln[5:] for ln in raw_event.splitlines() if ln.startswith("data:")]
    if not lines:
        return None
    return "\n".join(lines)
```

说明：`parse_sse_event` 已覆盖 `_sse_data` 的全部职责（提取 data、跳过 `[DONE]` 与非 JSON），转换分支改用它即可。

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_executor.py tests/test_e2e.py -v`
Expected: 全部 PASS（含原有流式重试测试 `test_stream_error_status_retried`）

- [ ] **Step 5: 提交**

```bash
git add app/executor.py tests/test_executor.py
git commit -m "feat: 流式请求旁路采集出参与缓存 token"
```

---

### Task 7: 界面展示（耗时秒 + JSON 格式化 + 缓存列）

**Files:**
- Modify: `app/admin/routes.py:1-38`（新增过滤器）
- Modify: `app/admin/templates/logs.html`
- Modify: `app/admin/templates/dashboard.html:60`
- Modify: `app/admin/templates/log_detail.html`
- Test: `tests/test_admin_logs_settings.py`

**Interfaces:**
- Consumes: `request_log` 新列
- Produces: Jinja 过滤器 `dur`（毫秒→`1.23 s`，None→`—`）、`pretty`（JSON 缩进，失败回退原文）

- [ ] **Step 1: 写失败测试**

在 `tests/test_admin_logs_settings.py` 末尾追加：

```python
async def test_log_pages_show_seconds_pretty_json_and_cache(
        tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute(
        "INSERT INTO request_log (created_at,client_protocol,group_name,path,"
        "stream,status,total_duration_ms,input_tokens,output_tokens,"
        "cache_read_tokens,cache_creation_tokens,request_body,response_body)"
        " VALUES (1.0,'anthropic','g','/v1/messages',0,'success',1230,10,4,"
        "900,50,'{\"model\":\"g\"}',"
        "'{\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]}')")
    conn.commit()
    log_id = conn.execute("SELECT id FROM request_log").fetchone()["id"]
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/logs")
        assert "1.23 s" in r.text
        assert "900" in r.text and "50" in r.text
        assert "1230ms" not in r.text
        r = await c.get(f"/admin/logs/{log_id}")
        assert r.status_code == 200
        assert "1.23 s" in r.text
        assert "响应报文" in r.text
        # JSON 已缩进且经 HTML 转义（引号为 &#34;）
        assert "&#34;type&#34;: &#34;text&#34;" in r.text
        assert '{\n  &#34;content&#34;' in r.text
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_admin_logs_settings.py::test_log_pages_show_seconds_pretty_json_and_cache -v`
Expected: FAIL — 页面里仍是 `1230ms`，且找不到 `响应报文`

- [ ] **Step 3: 在 `app/admin/routes.py` 增加过滤器**

顶部 import 区加入 `import json`（与 `import time`、`import sqlite3` 同组，按字母序放最前）：

```python
import json
import sqlite3
import time
```

在 `templates.env.filters["mask_key"] = mask_key` 之后追加：

```python
def fmt_duration(value):
    """毫秒 → 秒（两位小数）"""
    if value is None:
        return "—"
    try:
        return f"{float(value) / 1000:.2f} s"
    except (TypeError, ValueError):
        return "—"


def pretty_json(value):
    """JSON 字符串格式化为缩进文本，解析失败原样返回"""
    if not value:
        return ""
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return value


templates.env.filters["dur"] = fmt_duration
templates.env.filters["pretty"] = pretty_json
```

- [ ] **Step 4: 改 `logs.html`**

表头与行替换为（`colspan` 同步改为 12）：

```html
<thead><tr><th>#</th><th>时间</th><th>协议</th><th>模型</th><th>流式</th>
<th>状态</th><th>耗时</th><th>输入</th><th>输出</th><th>缓存读</th><th>缓存写</th><th></th></tr></thead>
<tbody>
{% for l in logs %}
<tr>
<td class="code" style="color:var(--ink-faint)">{{ l["id"] }}</td>
<td class="code">{{ l["created_at"] | ts }}</td>
<td><span class="chip">{{ l["client_protocol"] }}</span></td>
<td>{{ l["group_name"] }}</td>
<td>{{ "是" if l["stream"] else "—" }}</td>
<td><span class="badge {{ 'ok' if l["status"]=='success' else 'bad' }}">{{ "成功" if l["status"]=="success" else "失败" }}</span></td>
<td class="code">{{ l["total_duration_ms"] | dur }}</td>
<td class="code">{{ l["input_tokens"] if l["input_tokens"] is not none else "—" }}</td>
<td class="code">{{ l["output_tokens"] if l["output_tokens"] is not none else "—" }}</td>
<td class="code">{{ l["cache_read_tokens"] if l["cache_read_tokens"] is not none else "—" }}</td>
<td class="code">{{ l["cache_creation_tokens"] if l["cache_creation_tokens"] is not none else "—" }}</td>
<td><a href="/admin/logs/{{ l["id"] }}">详情</a></td>
</tr>
{% else %}
<tr><td colspan="12" class="empty">没有匹配的请求记录。</td></tr>
{% endfor %}
</tbody>
```

- [ ] **Step 5: 改 `dashboard.html`**

把最近请求表格里的 `<td class="code">{{ l["total_duration_ms"] }}ms</td>` 改为：

```html
<td class="code">{{ l["total_duration_ms"] | dur }}</td>
```

- [ ] **Step 6: 改 `log_detail.html`**

`meta-grid` 替换为 8 项：

```html
<dl class="meta-grid">
  <div><dt>协议</dt><dd><span class="chip">{{ log["client_protocol"] }}</span></dd></div>
  <div><dt>模型</dt><dd class="code">{{ log["group_name"] }}</dd></div>
  <div><dt>路径</dt><dd class="code">{{ log["path"] }}</dd></div>
  <div><dt>总耗时</dt><dd class="code">{{ log["total_duration_ms"] | dur }}</dd></div>
  <div><dt>输入 tokens</dt><dd class="code">{{ log["input_tokens"] if log["input_tokens"] is not none else "—" }}</dd></div>
  <div><dt>输出 tokens</dt><dd class="code">{{ log["output_tokens"] if log["output_tokens"] is not none else "—" }}</dd></div>
  <div><dt>缓存读取</dt><dd class="code">{{ log["cache_read_tokens"] if log["cache_read_tokens"] is not none else "—" }}</dd></div>
  <div><dt>缓存写入</dt><dd class="code">{{ log["cache_creation_tokens"] if log["cache_creation_tokens"] is not none else "—" }}</dd></div>
</dl>
```

`request_body` 区块改为 `| pretty`，并在其后新增响应报文区块：

```html
{% if log["request_body"] %}
<h3>请求报文</h3>
<div class="card">
<pre class="raw" style="max-height:360px;overflow-y:auto">{{ log["request_body"] | pretty }}</pre>
</div>
{% endif %}

{% if log["response_body"] %}
<h3>响应报文</h3>
<div class="card">
<pre class="raw" style="max-height:360px;overflow-y:auto">{{ log["response_body"] | pretty }}</pre>
</div>
{% endif %}
```

尝试链的 `raw_error` 单元格改为：

```html
<pre class="raw">{{ a["raw_error"] | pretty }}</pre>
```

- [ ] **Step 7: 运行测试，确认通过**

Run: `uv run pytest tests/test_admin_logs_settings.py tests/test_admin_channels.py tests/test_admin_groups.py -v`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add app/admin/routes.py app/admin/templates/logs.html \
  app/admin/templates/dashboard.html app/admin/templates/log_detail.html \
  tests/test_admin_logs_settings.py
git commit -m "feat: 日志界面耗时改秒并格式化展示入参/出参与缓存 token"
```

---

### Task 8: 全量回归

**Files:**
- 无新增；修复回归

**Interfaces:**
- Consumes: 前 7 个任务的全部产出
- Produces: 全绿测试基线

- [ ] **Step 1: 跑全量测试**

Run: `uv run pytest tests/ -v`
Expected: 全部 PASS，无 failure/error。若有失败，逐条修复（不要放宽断言）。

- [ ] **Step 2: 手工冒烟（真实进程）**

```bash
LLMAPIG_DATA_DIR=/tmp/llmapig-smoke uv run python -m app.main
```

另开终端：

```bash
curl -s http://127.0.0.1:8317/health
```

浏览器打开 `http://127.0.0.1:8317/admin/logs`，确认：
- 无日志时列表为空态文案正常；
- 仪表盘与日志页不再出现 `ms` 后缀；
- 详情页「请求报文」为缩进 JSON。

结束后关闭进程并删除 `/tmp/llmapig-smoke`。

- [ ] **Step 3: 提交（如有修复）**

```bash
git add -A
git commit -m "test: 日志出参与缓存 token 改造回归修复"
```

---

## 自查

**Spec 覆盖：**

| Spec 章节 | 对应任务 |
|---|---|
| 一、数据模型迁移 v2→v3 | Task 3 |
| 一、`finish_log` 扩展 | Task 3 |
| 二、`app/usage.py` 两个入口 + 语义说明 | Task 1 |
| 三、非流式采集（原始 body 提取、返回客户端体存盘） | Task 4 |
| 三、流式直通旁路解析 | Task 6 |
| 三、流式转换（converter.usage、最终 usage 覆盖） | Task 5 + Task 6 |
| 三、重建器（text/thinking/tool_use/坏 JSON/截断） | Task 2 |
| 三、OpenAI 入口非流式采集 | Task 4（`test_openai_entry_non_stream_maps_cached_tokens`） |
| 三、OpenAI 入口流式保持现状 | Task 6（`recon is None` 分支，原测试覆盖） |
| 四、`dur` / `pretty` 过滤器 | Task 7 |
| 四、日志列表四列 token | Task 7 |
| 四、仪表盘改秒 | Task 7 |
| 四、详情页 8 项 meta + 请求/响应报文格式化 | Task 7 |
| 五、测试清单 | Task 1–7 各步 |
| 六、明确不做（不改客户端事件、不存原始 SSE、不做 OpenAI 流式） | 全局约束 + Task 6 的 `recon is None` 分支 |
