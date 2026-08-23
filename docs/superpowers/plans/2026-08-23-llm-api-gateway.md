# LLM API 网关（llm-apig）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建本机部署的 LLM API 网关：Web 配置界面、渠道优先级、能力感知故障切换，兼容 Anthropic（优先）与 OpenAI 协议并支持双向转换。

**Architecture:** 单进程 FastAPI。路由层（Anthropic/OpenAI 入口）→ 渠道选择器（能力过滤→同协议优先→优先级→冷却）→ 协议转换层（纯函数 + 流式状态机）→ 执行器（httpx 转发 + 重试循环）。SQLite 存配置、运行时状态与请求日志。管理界面为 Jinja2 服务端渲染页面。

**Tech Stack:** Python 3.11+ / uv / FastAPI / uvicorn / httpx / Jinja2 / SQLite(stdlib sqlite3) / pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-08-23-llm-api-gateway-design.md`

## Global Constraints

- 包管理用 **uv**（`pyproject.toml`，`uv run` / `uv sync`），不要用 pip
- 只监听 `127.0.0.1`
- Anthropic 协议是一等公民：`/v1/messages` 完整支持流式、`tools`、`cache_control` 透传
- 重试只在「尚未向客户端发出任何字节」前进行；流式中途失败发协议内 error 事件，不重试
- 单请求最多尝试 `min(组内渠道数, 5)` 次
- 冷却默认值：余额不足 600s、限流 60s、鉴权失败 1800s、服务器错误 60s（settings 可覆盖）
- 未识别错误打标 `unclassified` 并保留原始错误体
- v1 不转换：`computer_use`、文件 block、citations、batch API；未知 block 同协议透传、跨协议报错
- 测试命令统一：`uv run pytest tests/ -v`
- 工作目录：`D:\AI\llm-apig`

---

### Task 1: 项目脚手架（uv + FastAPI + health 端点）

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`（空文件）
- Create: `app/main.py`
- Create: `start.bat`
- Create: `.gitignore`
- Test: `tests/__init__.py`（空）、`tests/test_health.py`

**Interfaces:**
- Produces: `app.main:app`（FastAPI 实例，后续所有 router 挂到它上面）；`uv run python -m app.main` 启动方式

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "llm-apig"
version = "0.1.0"
description = "Personal LLM API gateway with failover"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "httpx>=0.27",
    "jinja2>=3.1",
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: 写失败测试 `tests/test_health.py`**

```python
from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_health.py -v`
Expected: FAIL（`app.main` 不存在，ImportError）

- [ ] **Step 4: 实现 `app/main.py`**

```python
import threading
import webbrowser

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="llm-apig")


@app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8317/admin")).start()
    uvicorn.run(app, host="127.0.0.1", port=8317)


if __name__ == "__main__":
    main()
```

`app/__init__.py` 与 `tests/__init__.py` 为空文件。

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_health.py -v`
Expected: PASS（uv 会自动创建 venv 并装依赖）

- [ ] **Step 6: 写 start.bat 和 .gitignore，提交**

`start.bat`：
```bat
@echo off
cd /d %~dp0
uv run python -m app.main
```

`.gitignore`：
```
.venv/
__pycache__/
data/
*.pyc
```

```bash
git add -A && git commit -m "feat: 项目脚手架与 health 端点"
```

---

### Task 2: SQLite 存储层（db.py + schema + settings）

**Files:**
- Create: `app/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `connect() -> sqlite3.Connection`（row_factory=Row，WAL，外键开启；每次调用新建连接，用完关闭）
  - `init_db() -> None`（建表，幂等，`app.main` 启动时调用）
  - `get_setting(conn, key: str, default: str) -> str` / `set_setting(conn, key: str, value: str) -> None`
  - `DB_PATH`（`data/gateway.db`，测试中可用 `app.db.DB_PATH = tmp_path/...` 覆盖）
- 表结构见下（后续任务直接用这些表名/列名）

- [ ] **Step 1: 写失败测试 `tests/test_db.py`**

```python
import app.db as db


def test_init_db_creates_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"channel", "model_group", "model_mapping", "request_log",
            "attempt", "settings", "channel_state"} <= tables
    db.init_db()  # 幂等，不报错
    conn.close()


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    assert db.get_setting(conn, "cooldown_balance", "600") == "600"
    db.set_setting(conn, "cooldown_balance", "300")
    assert db.get_setting(conn, "cooldown_balance", "600") == "300"
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL（`app.db` 不存在）

- [ ] **Step 3: 实现 `app/db.py`**

```python
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gateway.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol IN ('anthropic', 'openai')),
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS model_group (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS model_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES model_group(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    actual_model TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    supports_image INTEGER NOT NULL DEFAULT 0,
    supports_video INTEGER NOT NULL DEFAULT 0,
    UNIQUE (group_id, channel_id)
);
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    client_protocol TEXT NOT NULL,
    group_name TEXT NOT NULL,
    path TEXT NOT NULL,
    stream INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    total_duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL REFERENCES request_log(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    channel_id INTEGER,
    channel_name TEXT,
    actual_model TEXT,
    converted INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error_category TEXT,
    error_summary TEXT,
    raw_error TEXT,
    skipped TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id INTEGER PRIMARY KEY REFERENCES channel(id) ON DELETE CASCADE,
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
```

在 `app/main.py` 的 `main()` 开头加 `init_db()`：
```python
from app.db import init_db

def main():
    init_db()
    threading.Timer(...)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_db.py tests/test_health.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: SQLite 存储层与 schema"
```

---

### Task 3: 错误分类器（errors.py）

**Files:**
- Create: `app/errors.py`
- Test: `tests/test_errors.py`

**Interfaces:**
- Produces:
  - `ErrorCategory`（Enum，值为 str）：`INSUFFICIENT_BALANCE / CAPABILITY_UNSUPPORTED / RATE_LIMIT / AUTH / SERVER / CLIENT / UNCLASSIFIED`
  - `Verdict` dataclass：`category: ErrorCategory; retryable: bool; cooldown_seconds: int; summary: str`
  - `classify_error(status: int | None, body: str) -> Verdict`（status=None 表示网络错误/超时）
  - `BALANCE_PATTERNS / CAPABILITY_PATTERNS: list[str]`（特征库，小写匹配，后续设置页可维护）
- Consumes: 无

- [ ] **Step 1: 写失败测试 `tests/test_errors.py`**

```python
from app.errors import ErrorCategory, classify_error


def test_network_error_retryable():
    v = classify_error(None, "connect timeout")
    assert v.category == ErrorCategory.SERVER and v.retryable and v.cooldown_seconds == 60


def test_balance_402():
    v = classify_error(402, '{"error":{"message":"Insufficient Balance"}}')
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE
    assert v.retryable and v.cooldown_seconds == 600


def test_balance_in_403_body():
    v = classify_error(403, "User quota is not enough, 余额不足")
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE


def test_rate_limit():
    v = classify_error(429, "rate limit reached")
    assert v.category == ErrorCategory.RATE_LIMIT and v.cooldown_seconds == 60


def test_auth():
    v = classify_error(401, "invalid api key")
    assert v.category == ErrorCategory.AUTH and v.cooldown_seconds == 1800


def test_capability_unsupported():
    v = classify_error(400, "this model does not support image input")
    assert v.category == ErrorCategory.CAPABILITY_UNSUPPORTED
    assert v.retryable and v.cooldown_seconds == 0


def test_client_error_not_retryable():
    v = classify_error(400, "max_tokens must be positive")
    assert v.category == ErrorCategory.CLIENT and not v.retryable


def test_server_5xx():
    v = classify_error(502, "bad gateway")
    assert v.category == ErrorCategory.SERVER and v.retryable


def test_unclassified_not_retryable():
    v = classify_error(418, "I'm a teapot")
    assert v.category == ErrorCategory.UNCLASSIFIED and not v.retryable
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL（`app.errors` 不存在）

- [ ] **Step 3: 实现 `app/errors.py`**

```python
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    INSUFFICIENT_BALANCE = "insufficient_balance"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    SERVER = "server"
    CLIENT = "client"
    UNCLASSIFIED = "unclassified"


@dataclass
class Verdict:
    category: ErrorCategory
    retryable: bool
    cooldown_seconds: int
    summary: str


BALANCE_PATTERNS = [
    "insufficient balance", "insufficient_quota", "insufficient user quota",
    "quota exceeded", "exceeded your current quota", "balance is not enough",
    "余额不足", "欠费", "账户余额", "arrears",
]

CAPABILITY_PATTERNS = [
    "does not support image", "not support image", "image input is not supported",
    "does not support video", "not support video", "video input is not supported",
    "does not support vision", "multimodal input is not supported",
    "invalid image data", "unsupported content type",
]


def _matches(body_lower: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if p in body_lower:
            return p
    return None


def classify_error(status: int | None, body: str) -> Verdict:
    body_lower = (body or "").lower()

    if status is None:
        return Verdict(ErrorCategory.SERVER, True, 60, f"network error: {body[:200]}")

    hit = _matches(body_lower, BALANCE_PATTERNS)
    if status == 402 or hit:
        return Verdict(ErrorCategory.INSUFFICIENT_BALANCE, True, 600,
                       f"balance insufficient (matched: {hit or 'http 402'})")

    hit = _matches(body_lower, CAPABILITY_PATTERNS)
    if status == 400 and hit:
        return Verdict(ErrorCategory.CAPABILITY_UNSUPPORTED, True, 0,
                       f"capability unsupported (matched: {hit})")

    if status == 429:
        return Verdict(ErrorCategory.RATE_LIMIT, True, 60, "rate limited")
    if status in (401, 403):
        return Verdict(ErrorCategory.AUTH, True, 1800, f"auth failed: {body[:200]}")
    if status == 400 or status == 404 or status == 422:
        return Verdict(ErrorCategory.CLIENT, False, 0, f"client error: {body[:200]}")
    if status >= 500:
        return Verdict(ErrorCategory.SERVER, True, 60, f"upstream server error: {status}")

    return Verdict(ErrorCategory.UNCLASSIFIED, False, 0,
                   f"unclassified error (status={status}): {body[:200]}")
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_errors.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 错误分类器与特征库"
```

---

### Task 4: 模态检测 + 渠道选择器（selector.py）

**Files:**
- Create: `app/selector.py`
- Test: `tests/test_selector.py`

**Interfaces:**
- Produces:
  - `detect_modalities(payload: dict) -> set[str]`：递归扫描 messages 内容，返回 `{"image","video"}` 的子集；兼容 Anthropic blocks 与 OpenAI parts
  - `Candidate` dataclass：`mapping_id: int; channel_id: int; channel_name: str; protocol: str; base_url: str; api_key: str; actual_model: str; needs_conversion: bool; cooling: bool`
  - `Skipped` dataclass：`channel_id: int; channel_name: str; reason: str`
  - `select_candidates(conn, group_name: str, entry_protocol: str, modalities: set[str], now: float | None = None) -> tuple[list[Candidate], list[Skipped]]`
- Consumes: `app.db.connect()`、Task 2 的表结构

- [ ] **Step 1: 写失败测试 `tests/test_selector.py`**

```python
import app.db as db
from app.selector import detect_modalities, select_candidates


def _setup(conn):
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key) VALUES "
                 "(1,'claude-a','anthropic','http://a','k1'),"
                 "(2,'ds','openai','http://b','k2'),"
                 "(3,'claude-cool','anthropic','http://c','k3'),"
                 "(4,'disabled','anthropic','http://d','k4')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'claude-sonnet')")
    conn.execute("INSERT INTO model_mapping "
                 "(group_id,channel_id,actual_model,priority,supports_image) VALUES "
                 "(1,1,'m-a',10,1),(1,2,'deepseek-chat',5,0),(1,3,'m-c',1,1),(1,4,'m-d',1,1)")
    conn.execute("UPDATE channel SET enabled=0 WHERE id=4")
    conn.execute("INSERT INTO channel_state (channel_id,cooldown_until) VALUES (3, 9999999999.0)")
    conn.commit()


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    _setup(conn)
    return conn


def test_detect_modalities_anthropic():
    p = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
    ]}]}
    assert detect_modalities(p) == {"image"}


def test_detect_modalities_openai_video():
    p = {"messages": [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "http://x/v.mp4"}}]}]}
    assert detect_modalities(p) == {"video"}


def test_detect_modalities_plain_text():
    assert detect_modalities({"messages": [{"role": "user", "content": "hi"}]}) == set()


def test_select_filters_capability_and_sorts(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    # 带图片：ds(supports_image=0) 被跳过；同协议优先于优先级（claude-a p10 排在 ds 之前不出现，ds 被过滤）
    cands, skipped = select_candidates(conn, "claude-sonnet", "anthropic", {"image"}, now=1000.0)
    names = [c.channel_name for c in cands]
    assert names == ["claude-a", "claude-cool"]  # 冷却排最后，disabled 不出现
    assert [s.channel_name for s in skipped] == ["ds"]
    assert skipped[0].reason == "capability"
    assert cands[0].needs_conversion is False
    assert cands[1].cooling is True
    conn.close()


def test_select_protocol_match_beats_priority(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    conn.execute("UPDATE channel_state SET cooldown_until=0 WHERE channel_id=3")
    conn.commit()
    # 纯文本请求：三个都可选；anthropic 入口下同协议优先 → claude-cool(p1) 和 claude-a(p10) 都在 ds(p5) 前
    cands, skipped = select_candidates(conn, "claude-sonnet", "anthropic", set(), now=1000.0)
    assert [c.channel_name for c in cands] == ["claude-cool", "claude-a", "ds"]
    assert cands[2].needs_conversion is True
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_selector.py -v`
Expected: FAIL（`app.selector` 不存在）

- [ ] **Step 3: 实现 `app/selector.py`**

```python
import time
from dataclasses import dataclass

IMAGE_TYPES = {"image", "image_url", "input_image"}
VIDEO_TYPES = {"video", "video_url", "input_video"}


def detect_modalities(payload: dict) -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t in IMAGE_TYPES:
                found.add("image")
            elif t in VIDEO_TYPES:
                found.add("video")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload.get("messages", []))
    return found


@dataclass
class Candidate:
    mapping_id: int
    channel_id: int
    channel_name: str
    protocol: str
    base_url: str
    api_key: str
    actual_model: str
    needs_conversion: bool
    cooling: bool


@dataclass
class Skipped:
    channel_id: int
    channel_name: str
    reason: str  # "capability"


def select_candidates(conn, group_name: str, entry_protocol: str,
                      modalities: set[str], now: float | None = None
                      ) -> tuple[list[Candidate], list[Skipped]]:
    now = time.time() if now is None else now
    rows = conn.execute(
        """
        SELECT m.id AS mapping_id, m.actual_model, m.priority,
               m.supports_image, m.supports_video,
               c.id AS channel_id, c.name AS channel_name, c.protocol,
               c.base_url, c.api_key,
               COALESCE(s.cooldown_until, 0) AS cooldown_until
        FROM model_mapping m
        JOIN model_group g ON g.id = m.group_id
        JOIN channel c ON c.id = m.channel_id
        LEFT JOIN channel_state s ON s.channel_id = c.id
        WHERE g.name = ? AND c.enabled = 1
        """,
        (group_name,),
    ).fetchall()

    candidates: list[Candidate] = []
    skipped: list[Skipped] = []
    for r in rows:
        if ("image" in modalities and not r["supports_image"]) or (
            "video" in modalities and not r["supports_video"]
        ):
            skipped.append(Skipped(r["channel_id"], r["channel_name"], "capability"))
            continue
        cooling = r["cooldown_until"] > now
        candidates.append(Candidate(
            mapping_id=r["mapping_id"], channel_id=r["channel_id"],
            channel_name=r["channel_name"], protocol=r["protocol"],
            base_url=r["base_url"], api_key=r["api_key"],
            actual_model=r["actual_model"],
            needs_conversion=(r["protocol"] != entry_protocol),
            cooling=cooling,
        ))

    candidates.sort(key=lambda c: c.priority)
    candidates.sort(key=lambda c: (c.cooling, c.needs_conversion))
    return candidates, skipped
```

（说明：利用 Python sort 稳定性，先 priority 再 (cooling, needs_conversion)，得到「冷却最后 → 同协议优先 → 优先级」。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_selector.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 模态检测与渠道选择器"
```

---

### Task 5: 非流式转换 Anthropic → OpenAI（convert/a2o.py）

**Files:**
- Create: `app/convert/__init__.py`（空）
- Create: `app/convert/a2o.py`
- Test: `tests/convert/test_a2o.py`（含 `tests/convert/__init__.py` 空文件）

**Interfaces:**
- Produces:
  - `request(payload: dict) -> dict`：Anthropic messages 请求 → OpenAI chat.completions 请求；payload 中 `model` 保持原样（执行器会改写为 actual_model）
  - `response(oa: dict, model: str) -> dict`：OpenAI 响应 → Anthropic message 响应，model 填统一模型名
  - `ConversionError(Exception)`：遇到不支持跨协议转换的 block 时抛出
- Consumes: 无

- [ ] **Step 1: 写失败测试 `tests/convert/test_a2o.py`**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/convert/test_a2o.py -v`
Expected: FAIL（`app.convert.a2o` 不存在）

- [ ] **Step 3: 实现 `app/convert/a2o.py`**

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/convert/test_a2o.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Anthropic→OpenAI 非流式转换"
```

---

### Task 6: 非流式转换 OpenAI → Anthropic（convert/o2a.py）

**Files:**
- Create: `app/convert/o2a.py`
- Test: `tests/convert/test_o2a.py`

**Interfaces:**
- Produces:
  - `request(payload: dict) -> dict`：OpenAI chat.completions 请求 → Anthropic messages 请求；无 max_tokens 时默认 4096（Anthropic 必填）
  - `response(a: dict, model: str) -> dict`：Anthropic message → OpenAI chat.completion
  - 复用 `app.convert.a2o.ConversionError`
- Consumes: Task 5 的 `ConversionError`

- [ ] **Step 1: 写失败测试 `tests/convert/test_o2a.py`**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/convert/test_o2a.py -v`
Expected: FAIL（`app.convert.o2a` 不存在）

- [ ] **Step 3: 实现 `app/convert/o2a.py`**

```python
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
        blocks: list[dict] = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            blocks.extend(_parts_to_blocks(content))
        for tc in m.get("tool_calls") or []:
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/convert/test_o2a.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: OpenAI→Anthropic 非流式转换"
```

---

### Task 7: 流式转换 OpenAI → Anthropic 事件流（convert/stream_a2o.py）

场景：客户端说 Anthropic 协议，上游是 OpenAI 渠道。把 OpenAI chunk 增量转成 Anthropic SSE 事件。**状态机**。

**Files:**
- Create: `app/convert/stream_a2o.py`
- Test: `tests/convert/test_stream_a2o.py`

**Interfaces:**
- Produces:
  - `class A2OStreamConverter(model: str)`：
    - `feed(chunk: dict) -> list[dict]`：输入一个 OpenAI chunk，输出 0..n 个 Anthropic 事件 dict（事件名 = dict 的 `"type"` 字段）
    - `finish() -> list[dict]`：上游流结束时调用，补发 `message_delta` + `message_stop`
- Consumes: `app.convert.a2o._FINISH_MAP`

- [ ] **Step 1: 写失败测试 `tests/convert/test_stream_a2o.py`**

```python
from app.convert.stream_a2o import A2OStreamConverter


def _text_flow():
    c = A2OStreamConverter("claude-sonnet")
    events = []
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"role": "assistant", "content": None},
                      "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"content": "Hel"}, "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"content": "lo"}, "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {},
                      "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    events += c.finish()
    return events


def test_text_stream_event_sequence():
    types = [e["type"] for e in _text_flow()]
    assert types == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]


def test_text_stream_content():
    events = _text_flow()
    deltas = [e["delta"]["text"] for e in events
              if e["type"] == "content_block_delta"]
    assert deltas == ["Hel", "lo"]
    msg = events[0]["message"]
    assert msg["model"] == "claude-sonnet" and msg["content"] == []
    md = [e for e in events if e["type"] == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"] == {"output_tokens": 2}


def test_tool_call_stream():
    c = A2OStreamConverter("m")
    events = []
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "get", "arguments": ""}}]},
        "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]},
        "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]},
        "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {},
                      "finish_reason": "tool_calls"}]})
    events += c.finish()
    types = [e["type"] for e in events]
    assert types == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]
    start = events[1]
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "get"
    json_deltas = [e["delta"]["partial_json"] for e in events
                   if e["type"] == "content_block_delta"]
    assert "".join(json_deltas) == '{"q":1}'
    md = [e for e in events if e["type"] == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use"


def test_text_then_tool_gets_two_blocks():
    c = A2OStreamConverter("m")
    events = []
    events += c.feed({"id": "c1", "choices": [{"index": 0,
                      "delta": {"content": "ok"}, "finish_reason": None}]})
    events += c.feed({"id": "c1", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": "call_1",
                        "function": {"name": "f", "arguments": "{}"}}]},
        "finish_reason": None}]})
    events += c.finish()
    starts = [e for e in events if e["type"] == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1]
    stops = [e for e in events if e["type"] == "content_block_stop"]
    assert [s["index"] for s in stops] == [0, 1]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/convert/test_stream_a2o.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `app/convert/stream_a2o.py`**

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/convert/test_stream_a2o.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: OpenAI→Anthropic 流式转换状态机"
```

---

### Task 8: 流式转换 Anthropic → OpenAI 事件流（convert/stream_o2a.py）

场景：客户端说 OpenAI 协议，上游是 Anthropic 渠道。把 Anthropic SSE 事件转成 OpenAI chunk。

**Files:**
- Create: `app/convert/stream_o2a.py`
- Test: `tests/convert/test_stream_o2a.py`

**Interfaces:**
- Produces:
  - `class O2AStreamConverter(model: str)`：
    - `feed(event: dict) -> list[dict]`：输入一个 Anthropic 事件，输出 0..n 个 OpenAI chunk dict
    - `finish() -> list[dict]`：通常为 []（`[DONE]` 由执行器统一追加）
- Consumes: `app.convert.o2a._STOP_MAP`

- [ ] **Step 1: 写失败测试 `tests/convert/test_stream_o2a.py`**

```python
from app.convert.stream_o2a import O2AStreamConverter


def _flow():
    c = O2AStreamConverter("ds")
    chunks = []
    chunks += c.feed({"type": "message_start", "message": {
        "id": "msg_1", "model": "claude-x",
        "usage": {"input_tokens": 9, "output_tokens": 0}}})
    chunks += c.feed({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}})
    chunks += c.feed({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": "Hi"}})
    chunks += c.feed({"type": "content_block_stop", "index": 0})
    chunks += c.feed({"type": "message_delta",
                      "delta": {"stop_reason": "end_turn"},
                      "usage": {"output_tokens": 4}})
    chunks += c.feed({"type": "message_stop"})
    chunks += c.finish()
    return chunks


def test_text_flow():
    chunks = _flow()
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[0]["model"] == "ds"
    assert chunks[1]["choices"][0]["delta"] == {"content": "Hi"}
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"] == {"prompt_tokens": 9, "completion_tokens": 4,
                             "total_tokens": 13}


def test_tool_flow():
    c = O2AStreamConverter("ds")
    chunks = []
    chunks += c.feed({"type": "message_start", "message": {
        "id": "msg_1", "usage": {"input_tokens": 1, "output_tokens": 0}}})
    chunks += c.feed({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "tool_use", "id": "t1",
                                        "name": "get", "input": {}}})
    chunks += c.feed({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "input_json_delta",
                                "partial_json": '{"q":'}})
    chunks += c.feed({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "input_json_delta",
                                "partial_json": '1}'}})
    chunks += c.feed({"type": "content_block_stop", "index": 0})
    chunks += c.feed({"type": "message_delta",
                      "delta": {"stop_reason": "tool_use"},
                      "usage": {"output_tokens": 2}})
    first_tc = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    assert first_tc["id"] == "t1"
    assert first_tc["function"] == {"name": "get", "arguments": ""}
    arg_chunks = [ch["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
                  for ch in chunks[2:4]]
    assert "".join(arg_chunks) == '{"q":1}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_ping_ignored():
    c = O2AStreamConverter("ds")
    assert c.feed({"type": "ping"}) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/convert/test_stream_o2a.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `app/convert/stream_o2a.py`**

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/convert/test_stream_o2a.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Anthropic→OpenAI 流式转换状态机"
```

---

### Task 9: 请求日志与冷却状态（logging_.py）

**Files:**
- Create: `app/logging_.py`
- Test: `tests/test_logging.py`

**Interfaces:**
- Produces:
  - `create_log(conn, *, client_protocol, group_name, path, stream: bool) -> int`
  - `finish_log(conn, log_id: int, *, status: str, total_ms: int, input_tokens: int | None, output_tokens: int | None) -> None`
  - `add_attempt(conn, log_id: int, *, channel_id=None, channel_name=None, actual_model=None, converted=False, http_status=None, error_category=None, error_summary=None, raw_error=None, skipped=None) -> None`
  - `set_cooldown(conn, channel_id: int, seconds: int, error: str) -> None`（seconds=0 时只记 last_error 不冷却）
  - `clear_cooldown(conn, channel_id: int) -> None`（渠道管理页手动恢复用）
- Consumes: Task 2 的 db

- [ ] **Step 1: 写失败测试 `tests/test_logging.py`**

```python
import time

import app.db as db
from app.logging_ import (add_attempt, clear_cooldown, create_log,
                          finish_log, set_cooldown)


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url) "
                 "VALUES (1,'c','anthropic','http://a')")
    conn.commit()
    return conn


def test_log_lifecycle(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    log_id = create_log(conn, client_protocol="anthropic",
                        group_name="g", path="/v1/messages", stream=True)
    add_attempt(conn, log_id, channel_id=1, channel_name="c",
                actual_model="m", http_status=402,
                error_category="insufficient_balance",
                error_summary="no money", raw_error='{"e":1}')
    add_attempt(conn, log_id, channel_id=1, channel_name="c",
                actual_model="m", http_status=200)
    finish_log(conn, log_id, status="success", total_ms=120,
               input_tokens=3, output_tokens=5)
    log = conn.execute("SELECT * FROM request_log WHERE id=?", (log_id,)).fetchone()
    assert log["status"] == "success" and log["stream"] == 1
    assert log["output_tokens"] == 5
    attempts = conn.execute(
        "SELECT * FROM attempt WHERE log_id=? ORDER BY id", (log_id,)).fetchall()
    assert len(attempts) == 2
    assert attempts[0]["error_category"] == "insufficient_balance"
    assert attempts[0]["raw_error"] == '{"e":1}'
    conn.close()


def test_cooldown(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    set_cooldown(conn, 1, 60, "boom")
    row = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert row["cooldown_until"] > time.time() + 50
    assert row["last_error"] == "boom"
    clear_cooldown(conn, 1)
    row = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert row["cooldown_until"] == 0
    conn.close()


def test_cooldown_zero_only_records_error(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    set_cooldown(conn, 1, 0, "note")
    row = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert row["cooldown_until"] == 0 and row["last_error"] == "note"
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_logging.py -v`
Expected: FAIL（`app.logging_` 不存在）

- [ ] **Step 3: 实现 `app/logging_.py`**

```python
import time


def create_log(conn, *, client_protocol, group_name, path, stream: bool) -> int:
    cur = conn.execute(
        "INSERT INTO request_log (created_at, client_protocol, group_name, path, stream)"
        " VALUES (?,?,?,?,?)",
        (time.time(), client_protocol, group_name, path, 1 if stream else 0),
    )
    conn.commit()
    return cur.lastrowid


def finish_log(conn, log_id: int, *, status: str, total_ms: int,
               input_tokens: int | None, output_tokens: int | None) -> None:
    conn.execute(
        "UPDATE request_log SET status=?, total_duration_ms=?,"
        " input_tokens=?, output_tokens=? WHERE id=?",
        (status, total_ms, input_tokens, output_tokens, log_id),
    )
    conn.commit()


def add_attempt(conn, log_id: int, *, channel_id=None, channel_name=None,
                actual_model=None, converted=False, http_status=None,
                error_category=None, error_summary=None, raw_error=None,
                skipped=None) -> None:
    conn.execute(
        "INSERT INTO attempt (log_id, created_at, channel_id, channel_name,"
        " actual_model, converted, http_status, error_category, error_summary,"
        " raw_error, skipped) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (log_id, time.time(), channel_id, channel_name, actual_model,
         1 if converted else 0, http_status, error_category, error_summary,
         raw_error, skipped),
    )
    conn.commit()


def set_cooldown(conn, channel_id: int, seconds: int, error: str) -> None:
    until = time.time() + seconds if seconds > 0 else 0
    conn.execute(
        "INSERT INTO channel_state (channel_id, cooldown_until, last_error)"
        " VALUES (?,?,?) ON CONFLICT(channel_id) DO UPDATE SET"
        " cooldown_until=excluded.cooldown_until, last_error=excluded.last_error",
        (channel_id, until, error[:500]),
    )
    conn.commit()


def clear_cooldown(conn, channel_id: int) -> None:
    conn.execute(
        "INSERT INTO channel_state (channel_id, cooldown_until, last_error)"
        " VALUES (?,0,NULL) ON CONFLICT(channel_id) DO UPDATE SET"
        " cooldown_until=0, last_error=NULL",
        (channel_id,),
    )
    conn.commit()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_logging.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 请求日志与冷却状态"
```

---

### Task 10: 转发执行器（executor.py）——重试循环 + 流式 + 协议转换接线

核心模块。把选择器、转换层、错误分类、日志串起来。

**Files:**
- Create: `app/executor.py`
- Test: `tests/test_executor.py`（用 `httpx.MockTransport` 模拟上游）

**Interfaces:**
- Produces:
  - `GatewayResult` dataclass：`status: int; body: dict | None; events: AsyncIterator[str] | None`（非流式填 body，流式填 events——已是**入口协议**的 SSE 文本行）
  - `async execute(conn, *, entry_protocol: str, group_name: str, payload: dict, stream: bool) -> GatewayResult`
  - `render_sse(events: list[dict], protocol: str) -> list[str]`：事件 dict → SSE 文本（anthropic: `event:`+`data:`；openai: `data:`）
- Consumes: Task 3 `classify_error`、Task 4 `select_candidates/detect_modalities`、Task 5-8 转换器、Task 9 日志函数、`app.db.get_setting`

**关键行为**（测试逐一验证）：
1. 无候选渠道 → 502 + 错误体（入口协议格式）
2. 非流式成功：直接返回（协议需转换时先转请求、再转响应）
3. 402 → 冷却该渠道 + 自动重试下一渠道，客户端无感知
4. 400 普通错误 → 不重试，原样返回
5. 全部失败 → 返回最后错误，日志含全部尝试
6. 流式：上游返回错误状态（尚未读 body）→ 重试下一渠道；2xx 后开始产出事件
7. 冷却时长从 settings 读：`cooldown_balance/ratelimit/auth/server`

- [ ] **Step 1: 写失败测试 `tests/test_executor.py`**

```python
import json

import httpx
import pytest

import app.db as db
import app.executor as ex
from app.executor import execute, render_sse


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db.connect()


def _add_channel(conn, id, name, protocol, priority, model="m"):
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (?,?,?,?,?)", (id, name, protocol, "http://up", "k"))
    conn.execute("INSERT OR IGNORE INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority,supports_image)"
                 " VALUES (1,?,?,?,1)", (id, model, priority))
    conn.commit()


def _mock(hits):
    """hits: list of (status, body_dict)。返回 transport 和调用记录。"""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        status, payload = hits[len(calls) - 1]
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), calls


async def test_success_anthropic_passthrough(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    transport, calls = _mock([(200, {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 2}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 200 and r.body["content"][0]["text"] == "hi"
    assert calls[0]["model"] == "m"  # 改写为 actual_model
    assert calls[0]["messages"] == []
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["status"] == "success" and log["output_tokens"] == 2
    conn.close()


async def test_balance_failover_to_next_channel(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "broke", "anthropic", 1)
    _add_channel(conn, 2, "rich", "anthropic", 2)
    transport, calls = _mock([
        (402, {"error": {"message": "Insufficient Balance"}}),
        (200, {"id": "m", "type": "message", "role": "assistant",
               "content": [{"type": "text", "text": "ok"}],
               "stop_reason": "end_turn",
               "usage": {"input_tokens": 1, "output_tokens": 1}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 200
    assert len(calls) == 2
    st = conn.execute("SELECT * FROM channel_state WHERE channel_id=1").fetchone()
    assert st["cooldown_until"] > 0  # 402 的渠道被冷却
    attempts = conn.execute("SELECT * FROM attempt ORDER BY id").fetchall()
    assert len(attempts) == 2
    assert attempts[0]["error_category"] == "insufficient_balance"
    conn.close()


async def test_client_error_not_retried(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    _add_channel(conn, 2, "c2", "anthropic", 2)
    transport, calls = _mock([(400, {"error": {"message": "bad param"}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 400 and len(calls) == 1
    conn.close()


async def test_all_fail_returns_last_error(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    _add_channel(conn, 2, "c2", "anthropic", 2)
    transport, _ = _mock([(500, {"e": "x"}), (503, {"e": "y"})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 503
    assert conn.execute("SELECT * FROM request_log").fetchone()["status"] == "failed"
    conn.close()


async def test_no_candidates(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 502
    assert r.body["error"]["type"] == "api_error"
    conn.close()


async def test_cross_protocol_conversion(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "ds", "openai", 1, model="deepseek-chat")
    transport, calls = _mock([(200, {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}})])
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "max_tokens": 10,
                               "system": "s",
                               "messages": [{"role": "user", "content": "hi"}]},
                      stream=False)
    assert r.status == 200
    assert r.body["type"] == "message"  # 响应已转回 Anthropic 格式
    assert calls[0]["messages"][0] == {"role": "system", "content": "s"}
    assert conn.execute("SELECT * FROM attempt").fetchone()["converted"] == 1
    conn.close()


async def test_stream_error_status_retried(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _add_channel(conn, 1, "c1", "anthropic", 1)
    _add_channel(conn, 2, "c2", "anthropic", 2)
    sse = (b'event: message_start\ndata: {"type":"message_start","message":'
           b'{"id":"m","usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
           b'event: content_block_delta\ndata: {"type":"content_block_delta",'
           b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
           b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": [], "stream": True},
                      stream=True)
    assert r.status == 200 and r.events is not None
    text = "".join([e async for e in r.events])
    assert "text_delta" in text and len(calls) == 2
    conn.close()


def test_render_sse_formats():
    ev = [{"type": "message_stop"}]
    assert render_sse(ev, "anthropic") == [
        'event: message_stop\ndata: {"type": "message_stop"}\n\n']
    assert render_sse(ev, "openai")[0].startswith("data: ")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_executor.py -v`
Expected: FAIL（`app.executor` 不存在）

- [ ] **Step 3: 实现 `app/executor.py`**

```python
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.convert import a2o, o2a, stream_a2o, stream_o2a
from app.db import get_setting
from app.errors import classify_error
from app.logging_ import add_attempt, create_log, finish_log, set_cooldown
from app.selector import detect_modalities, select_candidates

MAX_ATTEMPTS = 5
_transport: httpx.MockTransport | None = None  # 测试注入点

_COOLDOWN_SETTING_KEYS = {
    "insufficient_balance": ("cooldown_balance", 600),
    "rate_limit": ("cooldown_ratelimit", 60),
    "auth": ("cooldown_auth", 1800),
    "server": ("cooldown_server", 60),
}


@dataclass
class GatewayResult:
    status: int
    body: dict | None = None
    events: AsyncIterator[str] | None = None


def render_sse(events: list[dict], protocol: str) -> list[str]:
    out = []
    for e in events:
        data = json.dumps(e, ensure_ascii=False)
        if protocol == "anthropic":
            out.append(f"event: {e['type']}\ndata: {data}\n\n")
        else:
            out.append(f"data: {data}\n\n")
    return out


def _error_body(protocol: str, message: str, status: int) -> dict:
    if protocol == "anthropic":
        return {"type": "error",
                "error": {"type": "api_error", "message": message}}
    return {"error": {"message": message, "type": "api_error", "code": status}}


def _target(cand) -> tuple[str, dict]:
    base = cand.base_url.rstrip("/")
    if cand.protocol == "anthropic":
        return f"{base}/v1/messages", {
            "x-api-key": cand.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return f"{base}/v1/chat/completions", {
        "authorization": f"Bearer {cand.api_key}",
        "content-type": "application/json",
    }


def _cooldown_seconds(conn, category: str, default: int) -> int:
    item = _COOLDOWN_SETTING_KEYS.get(category)
    if not item:
        return default
    key, fallback = item
    try:
        return int(get_setting(conn, key, str(fallback)))
    except ValueError:
        return fallback


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=httpx.Timeout(120.0, connect=15.0))


async def execute(conn, *, entry_protocol: str, group_name: str,
                  payload: dict, stream: bool) -> GatewayResult:
    started = time.time()
    modalities = detect_modalities(payload)
    candidates, skipped = select_candidates(
        conn, group_name, entry_protocol, modalities)
    log_id = create_log(conn, client_protocol=entry_protocol,
                        group_name=group_name,
                        path="/v1/messages" if entry_protocol == "anthropic"
                        else "/v1/chat/completions",
                        stream=stream)
    for s in skipped:
        add_attempt(conn, log_id, channel_id=s.channel_id,
                    channel_name=s.channel_name, skipped=s.reason)

    if not candidates:
        finish_log(conn, log_id, status="failed",
                   total_ms=_elapsed(started), input_tokens=None, output_tokens=None)
        return GatewayResult(502, body=_error_body(
            entry_protocol, f"模型组 {group_name!r} 没有可用渠道", 502))

    last_status, last_body = 502, {}
    for cand in candidates[:MAX_ATTEMPTS]:
        url, headers = _target(cand)
        upstream_payload = dict(payload)
        upstream_payload["model"] = cand.actual_model
        upstream_payload["stream"] = stream
        if cand.needs_conversion:
            try:
                if entry_protocol == "anthropic":
                    upstream_payload = a2o.request(upstream_payload)
                else:
                    upstream_payload = o2a.request(upstream_payload)
            except a2o.ConversionError as e:
                add_attempt(conn, log_id, channel_id=cand.channel_id,
                            channel_name=cand.channel_name,
                            actual_model=cand.actual_model, converted=True,
                            error_category="conversion",
                            error_summary=str(e))
                continue

        if stream:
            result, consumed = await _try_stream(
                conn, log_id, cand, entry_protocol, url, headers,
                upstream_payload, started, group_name)
            if result is not None:
                return result
            last_status, last_body = consumed
            continue

        result, consumed = await _try_non_stream(
            conn, log_id, cand, entry_protocol, url, headers,
            upstream_payload, started, group_name)
        if result is not None:
            return result
        last_status, last_body = consumed

    finish_log(conn, log_id, status="failed", total_ms=_elapsed(started),
               input_tokens=None, output_tokens=None)
    body = last_body if isinstance(last_body, dict) and last_body else _error_body(
        entry_protocol, "所有渠道均失败", last_status)
    return GatewayResult(last_status, body=body)


def _elapsed(started: float) -> int:
    return int((time.time() - started) * 1000)


async def _try_non_stream(conn, log_id, cand, entry_protocol, url, headers,
                          upstream_payload, started, group_name):
    """返回 (GatewayResult | None, (status, body))。None 表示失败已记录可重试。"""
    try:
        async with _client() as client:
            resp = await client.post(url, json=upstream_payload, headers=headers)
    except httpx.HTTPError as e:
        _record_failure(conn, log_id, cand, classify_error(None, str(e)), None, str(e))
        return None, (502, {})

    if resp.status_code < 400:
        body = resp.json()
        if cand.needs_conversion:
            body = (o2a.response(body, group_name) if entry_protocol == "openai"
                    else a2o.response(body, group_name))
        add_attempt(conn, log_id, channel_id=cand.channel_id,
                    channel_name=cand.channel_name,
                    actual_model=cand.actual_model,
                    converted=cand.needs_conversion, http_status=resp.status_code)
        usage = body.get("usage") or {}
        finish_log(conn, log_id, status="success", total_ms=_elapsed(started),
                   input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
                   output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"))
        return GatewayResult(resp.status_code, body=body), (resp.status_code, body)

    body_text = resp.text
    verdict = classify_error(resp.status_code, body_text)
    _record_failure(conn, log_id, cand, verdict, resp.status_code, body_text)
    if not verdict.retryable:
        finish_log(conn, log_id, status="failed", total_ms=_elapsed(started),
                   input_tokens=None, output_tokens=None)
        try:
            parsed = resp.json()
        except ValueError:
            parsed = _error_body(entry_protocol, body_text[:500], resp.status_code)
        return GatewayResult(resp.status_code, body=parsed), (resp.status_code, {})
    return None, (resp.status_code, _safe_json(body_text))


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _record_failure(conn, log_id, cand, verdict, http_status, raw: str) -> None:
    add_attempt(conn, log_id, channel_id=cand.channel_id,
                channel_name=cand.channel_name, actual_model=cand.actual_model,
                converted=cand.needs_conversion, http_status=http_status,
                error_category=verdict.category.value,
                error_summary=verdict.summary, raw_error=(raw or "")[:2000])
    if verdict.cooldown_seconds:
        secs = _cooldown_seconds(conn, verdict.category.value,
                                 verdict.cooldown_seconds)
        set_cooldown(conn, cand.channel_id, secs, verdict.summary)


async def _try_stream(conn, log_id, cand, entry_protocol, url, headers,
                      upstream_payload, started, group_name):
    """流式尝试。状态码错误（未读 body）可安全重试；2xx 后产出事件生成器。"""
    try:
        client = _client()
        req = client.build_request("POST", url, json=upstream_payload,
                                   headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        _record_failure(conn, log_id, cand, classify_error(None, str(e)), None, str(e))
        return None, (502, {})

    if resp.status_code >= 400:
        raw = (await resp.aread()).decode("utf-8", "replace")
        await resp.aclose()
        await client.aclose()
        verdict = classify_error(resp.status_code, raw)
        _record_failure(conn, log_id, cand, verdict, resp.status_code, raw)
        if not verdict.retryable:
            finish_log(conn, log_id, status="failed",
                       total_ms=_elapsed(started), input_tokens=None,
                       output_tokens=None)
            return GatewayResult(resp.status_code,
                                 body=_safe_json(raw) or _error_body(
                                     entry_protocol, raw[:500], resp.status_code)), \
                (resp.status_code, {})
        return None, (resp.status_code, _safe_json(raw))

    add_attempt(conn, log_id, channel_id=cand.channel_id,
                channel_name=cand.channel_name, actual_model=cand.actual_model,
                converted=cand.needs_conversion, http_status=200)

    async def events() -> AsyncIterator[str]:
        failed_mid = None
        try:
            if not cand.needs_conversion:
                async for line in resp.aiter_text():
                    yield line
            else:
                converter = (stream_o2a.O2AStreamConverter(group_name)
                             if cand.protocol == "anthropic"
                             else stream_a2o.A2OStreamConverter(group_name))
                buf = ""
                async for chunk_bytes in resp.aiter_text():
                    buf += chunk_bytes
                    while "\n\n" in buf:
                        raw_event, buf = buf.split("\n\n", 1)
                        data = _sse_data(raw_event)
                        if data is None:
                            continue
                        if data.strip() == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        for out in render_sse(converter.feed(obj), entry_protocol):
                            yield out
                for out in render_sse(converter.finish(), entry_protocol):
                    yield out
                if entry_protocol == "openai":
                    yield "data: [DONE]\n\n"
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
            status = "success" if failed_mid is None else "failed"
            finish_log(conn, log_id, status=status,
                       total_ms=_elapsed(started),
                       input_tokens=None, output_tokens=None)
            if failed_mid is not None:
                verdict = classify_error(None, failed_mid)
                _record_failure(conn, log_id, cand, verdict, None, failed_mid)

    return GatewayResult(200, events=events()), (200, {})


def _sse_data(raw_event: str) -> str | None:
    """从一个 SSE 事件块中提取 data 字段（可能多行）。"""
    lines = [ln[5:] for ln in raw_event.splitlines() if ln.startswith("data:")]
    if not lines:
        return None
    return "\n".join(lines)
```

注意：流式成功路径中 token usage 从流中提取较复杂，v1 记 NULL（attempt 已记渠道）；v2 可增强。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_executor.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 转发执行器（重试循环/冷却/协议转换接线/流式）"
```

---

### Task 11: Anthropic 路由（router_anthropic.py）+ 可选访问令牌

**Files:**
- Create: `app/router_anthropic.py`
- Modify: `app/main.py`（挂 router + startup 里 init_db）
- Test: `tests/test_router_anthropic.py`

**Interfaces:**
- Produces:
  - `POST /v1/messages`、`POST /v1/messages/count_tokens`
  - 鉴权依赖 `verify_token(request)`：settings `api_key` 非空时校验 `x-api-key` 或 `Authorization: Bearer`，否则放行
- Consumes: Task 10 `execute`、Task 2 db、Task 4 选择器

- [ ] **Step 1: 写失败测试 `tests/test_router_anthropic.py`**

```python
import httpx
import pytest

import app.db as db
import app.executor as ex
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'c1','anthropic','http://up','k')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority)"
                 " VALUES (1,1,'real-model',1)")
    conn.commit()
    conn.close()


def _mock_ok(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))


async def test_messages_endpoint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _mock_ok(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages", json={
            "model": "g", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["content"][0]["text"] == "hello"


async def test_count_tokens_endpoint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def handler(request):
        return httpx.Response(200, json={"input_tokens": 42})
    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages/count_tokens", json={
            "model": "g", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["input_tokens"] == 42


async def test_token_auth(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _mock_ok(monkeypatch)
    conn = db.connect()
    db.set_setting(conn, "api_key", "secret")
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages", json={"model": "g", "messages": []})
        assert r.status_code == 401
        r = await c.post("/v1/messages",
                         headers={"x-api-key": "secret"},
                         json={"model": "g", "messages": []})
        assert r.status_code != 401
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_router_anthropic.py -v`
Expected: FAIL（`app.router_anthropic` 不存在）

- [ ] **Step 3: 实现 `app/router_anthropic.py`**

```python
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import app.executor as ex
from app.db import connect, get_setting
from app.selector import select_candidates

router = APIRouter()


async def verify_token(request: Request):
    conn = connect()
    try:
        token = get_setting(conn, "api_key", "")
    finally:
        conn.close()
    if not token:
        return
    provided = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        provided = provided or auth[7:]
    if provided != token:
        raise HTTPException(status_code=401, detail={
            "type": "error", "error": {"type": "authentication_error",
                                       "message": "invalid api key"}})


@router.post("/v1/messages", dependencies=[Depends(verify_token)])
async def messages(request: Request):
    payload = await request.json()
    conn = connect()
    try:
        result = await ex.execute(
            conn, entry_protocol="anthropic",
            group_name=payload.get("model", ""),
            payload=payload, stream=bool(payload.get("stream")))
    finally:
        conn.close()
    if result.events is not None:
        return StreamingResponse(result.events,
                                 media_type="text/event-stream",
                                 status_code=result.status)
    return JSONResponse(result.body, status_code=result.status)


@router.post("/v1/messages/count_tokens", dependencies=[Depends(verify_token)])
async def count_tokens(request: Request):
    payload = await request.json()
    conn = connect()
    try:
        candidates, _ = select_candidates(
            conn, payload.get("model", ""), "anthropic", set())
        cand = next((c for c in candidates if c.protocol == "anthropic"), None)
        if cand is None:
            # OpenAI 渠道不支持 count_tokens：近似估算
            text = str(payload.get("messages", ""))
            return JSONResponse({"input_tokens": max(1, len(text) // 4)},
                                headers={"x-estimated": "true"})
        url = cand.base_url.rstrip("/") + "/v1/messages/count_tokens"
        headers = {"x-api-key": cand.api_key,
                   "anthropic-version": "2023-06-01"}
        async with httpx.AsyncClient(
                transport=ex._transport, timeout=30) as client:
            resp = await client.post(url, json={**payload, "model": cand.actual_model},
                                     headers=headers)
        return JSONResponse(resp.json() if resp.status_code < 400 else
                            {"input_tokens": 1}, status_code=resp.status_code)
    finally:
        conn.close()
```

在 `app/main.py` 中挂载：
```python
from app.router_anthropic import router as anthropic_router

app.include_router(anthropic_router)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_router_anthropic.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Anthropic 协议路由与访问令牌"
```

---

### Task 12: OpenAI 路由（router_openai.py）

**Files:**
- Create: `app/router_openai.py`
- Modify: `app/main.py`（挂载）
- Test: `tests/test_router_openai.py`

**Interfaces:**
- Produces: `POST /v1/chat/completions`、`GET /v1/models`（列出所有模型组的统一模型名，OpenAI models 格式）
- Consumes: Task 10 `execute`、Task 11 `verify_token`

- [ ] **Step 1: 写失败测试 `tests/test_router_openai.py`**

```python
import httpx

import app.db as db
import app.executor as ex
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'ds','openai','http://up','k')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'my-chat')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority)"
                 " VALUES (1,1,'deepseek-chat',1)")
    conn.commit()
    conn.close()


async def test_chat_completions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def handler(request):
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "yo"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}})
    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/chat/completions", json={
            "model": "my-chat",
            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "yo"


async def test_models_list(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["my-chat"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_router_openai.py -v`
Expected: FAIL（`app.router_openai` 不存在）

- [ ] **Step 3: 实现 `app/router_openai.py`**

```python
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

import app.executor as ex
from app.db import connect
from app.router_anthropic import verify_token

router = APIRouter()


@router.post("/v1/chat/completions", dependencies=[Depends(verify_token)])
async def chat_completions(request: Request):
    payload = await request.json()
    conn = connect()
    try:
        result = await ex.execute(
            conn, entry_protocol="openai",
            group_name=payload.get("model", ""),
            payload=payload, stream=bool(payload.get("stream")))
    finally:
        conn.close()
    if result.events is not None:
        return StreamingResponse(result.events,
                                 media_type="text/event-stream",
                                 status_code=result.status)
    return JSONResponse(result.body, status_code=result.status)


@router.get("/v1/models", dependencies=[Depends(verify_token)])
async def list_models():
    conn = connect()
    try:
        rows = conn.execute("SELECT name FROM model_group ORDER BY name").fetchall()
    finally:
        conn.close()
    return {"object": "list", "data": [
        {"id": r["name"], "object": "model", "created": int(time.time()),
         "owned_by": "llm-apig"} for r in rows]}
```

在 `app/main.py` 挂载：
```python
from app.router_openai import router as openai_router

app.include_router(openai_router)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_router_openai.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: OpenAI 协议路由"
```

---

### Task 13: 管理界面（一）——脚手架 + 仪表盘 + 渠道管理

表单 POST 需要 `python-multipart`。

**Files:**
- Modify: `pyproject.toml`（dependencies 加 `"python-multipart>=0.0.9"`），然后 `uv sync`
- Create: `app/admin/__init__.py`（空）
- Create: `app/admin/routes.py`
- Create: `app/admin/templates/base.html`
- Create: `app/admin/templates/dashboard.html`
- Create: `app/admin/templates/channels.html`
- Create: `app/admin/templates/channel_form.html`
- Modify: `app/main.py`（挂载 admin router）
- Test: `tests/test_admin_channels.py`

**Interfaces:**
- Produces:
  - `GET /admin`（仪表盘）、`GET/POST /admin/channels`（列表/新建）、`GET/POST /admin/channels/{id}/edit`、`POST /admin/channels/{id}/delete`、`POST /admin/channels/{id}/toggle`、`POST /admin/channels/{id}/clear-cooldown`
  - `POST /admin/channels/{id}/test` → JSON `{ok: bool, detail: str}`（连通性测试，用该渠道任一映射的 actual_model 发 max_tokens=1 请求）
  - `templates = Jinja2Templates(...)`（`app.admin.routes.templates`，后续任务复用）
- Consumes: Task 2 db、Task 9 `clear_cooldown`

- [ ] **Step 1: 加依赖**

在 `pyproject.toml` dependencies 追加 `"python-multipart>=0.0.9"`，运行 `uv sync`。

- [ ] **Step 2: 写失败测试 `tests/test_admin_channels.py`**

```python
import httpx

import app.db as db
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


async def test_dashboard_ok(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin")
    assert r.status_code == 200 and "渠道" in r.text


async def test_channel_crud(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/channels", data={
            "name": "官方", "protocol": "anthropic",
            "base_url": "https://api.anthropic.com", "api_key": "sk-x"})
        assert r.status_code == 303
        conn = db.connect()
        row = conn.execute("SELECT * FROM channel WHERE name='官方'").fetchone()
        assert row["protocol"] == "anthropic" and row["enabled"] == 1
        cid = row["id"]
        r = await c.post(f"/admin/channels/{cid}/toggle")
        conn2 = db.connect()
        assert conn2.execute("SELECT enabled FROM channel WHERE id=?",
                             (cid,)).fetchone()["enabled"] == 0
        r = await c.post(f"/admin/channels/{cid}/delete")
        assert db.connect().execute(
            "SELECT COUNT(*) c FROM channel").fetchone()["c"] == 0
        conn.close(); conn2.close()


async def test_channel_test_endpoint(tmp_path, monkeypatch):
    import app.admin.routes as admin
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key)"
                 " VALUES (1,'c','anthropic','http://up','k')")
    conn.commit(); conn.close()

    def handler(request):
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "h"}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(admin, "_transport", httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/admin/channels/1/test")
    assert r.json()["ok"] is True
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_admin_channels.py -v`
Expected: FAIL（`app.admin` 不存在）

- [ ] **Step 4: 实现**

`app/admin/routes.py`：
```python
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import connect
from app.logging_ import clear_cooldown

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"))
_transport = None  # 测试注入点


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    import time
    conn = connect()
    try:
        channels = conn.execute(
            "SELECT c.*, COALESCE(s.cooldown_until,0) cooldown_until, s.last_error"
            " FROM channel c LEFT JOIN channel_state s ON s.channel_id=c.id"
            " ORDER BY c.id").fetchall()
        logs = conn.execute(
            "SELECT * FROM request_log ORDER BY id DESC LIMIT 20").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "channels": channels, "logs": logs,
        "now": time.time()})


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    conn = connect()
    try:
        channels = conn.execute(
            "SELECT c.*, COALESCE(s.cooldown_until,0) cooldown_until, s.last_error"
            " FROM channel c LEFT JOIN channel_state s ON s.channel_id=c.id"
            " ORDER BY c.id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("channels.html",
                                      {"request": request, "channels": channels})


@router.get("/channels/new", response_class=HTMLResponse)
async def channel_new(request: Request):
    return templates.TemplateResponse("channel_form.html",
                                      {"request": request, "ch": None})


@router.post("/channels")
async def channel_create(name: str = Form(...), protocol: str = Form(...),
                         base_url: str = Form(...), api_key: str = Form("")):
    conn = connect()
    try:
        conn.execute("INSERT INTO channel (name,protocol,base_url,api_key)"
                     " VALUES (?,?,?,?)", (name, protocol, base_url, api_key))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.get("/channels/{cid}/edit", response_class=HTMLResponse)
async def channel_edit(request: Request, cid: int):
    conn = connect()
    try:
        ch = conn.execute("SELECT * FROM channel WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse("channel_form.html",
                                      {"request": request, "ch": ch})


@router.post("/channels/{cid}/edit")
async def channel_update(cid: int, name: str = Form(...),
                         protocol: str = Form(...), base_url: str = Form(...),
                         api_key: str = Form("")):
    conn = connect()
    try:
        if api_key:
            conn.execute("UPDATE channel SET name=?,protocol=?,base_url=?,api_key=?"
                         " WHERE id=?", (name, protocol, base_url, api_key, cid))
        else:  # 留空表示不修改密钥
            conn.execute("UPDATE channel SET name=?,protocol=?,base_url=?"
                         " WHERE id=?", (name, protocol, base_url, cid))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/toggle")
async def channel_toggle(cid: int):
    conn = connect()
    try:
        conn.execute("UPDATE channel SET enabled = 1 - enabled WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/delete")
async def channel_delete(cid: int):
    conn = connect()
    try:
        conn.execute("DELETE FROM channel WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/clear-cooldown")
async def channel_clear_cooldown(cid: int):
    conn = connect()
    try:
        clear_cooldown(conn, cid)
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/test")
async def channel_test(cid: int):
    conn = connect()
    try:
        ch = conn.execute("SELECT * FROM channel WHERE id=?", (cid,)).fetchone()
        model_row = conn.execute(
            "SELECT actual_model FROM model_mapping WHERE channel_id=?"
            " ORDER BY priority LIMIT 1", (cid,)).fetchone()
    finally:
        conn.close()
    if ch is None:
        return JSONResponse({"ok": False, "detail": "渠道不存在"}, 404)
    model = model_row["actual_model"] if model_row else (
        "claude-haiku-4-5" if ch["protocol"] == "anthropic" else "gpt-4o-mini")
    base = ch["base_url"].rstrip("/")
    if ch["protocol"] == "anthropic":
        url, headers = f"{base}/v1/messages", {
            "x-api-key": ch["api_key"], "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": 1,
                   "messages": [{"role": "user", "content": "hi"}]}
    else:
        url, headers = f"{base}/v1/chat/completions", {
            "authorization": f"Bearer {ch['api_key']}"}
        payload = {"model": model, "max_tokens": 1,
                   "messages": [{"role": "user", "content": "hi"}]}
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"网络错误: {e}"}
    ok = resp.status_code < 400
    return {"ok": ok, "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}
```

`app/admin/templates/base.html`：
```html
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>llm-apig</title>
<style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px;color:#222}
nav a{margin-right:16px}
table{border-collapse:collapse;width:100%;margin-top:12px}
th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px}
.badge{padding:2px 8px;border-radius:10px;font-size:12px}
.ok{background:#d3f9d8}.bad{background:#ffe3e3}.cool{background:#fff3bf}
button{cursor:pointer}input,select{padding:5px;margin:4px 0;width:100%;box-sizing:border-box}
form.inline{display:inline}
</style>
</head>
<body>
<nav>
<b>llm-apig</b>
<a href="/admin">仪表盘</a>
<a href="/admin/channels">渠道</a>
<a href="/admin/groups">模型组</a>
<a href="/admin/logs">日志</a>
<a href="/admin/settings">设置</a>
</nav>
<hr>
{% block content %}{% endblock %}
</body>
</html>
```

`app/admin/templates/dashboard.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>渠道状态</h2>
<table>
<tr><th>名称</th><th>协议</th><th>状态</th><th>最近错误</th></tr>
{% for c in channels %}
<tr>
<td>{{ c["name"] }}{{ "" if c["enabled"] else "（已停用）" }}</td>
<td>{{ c["protocol"] }}</td>
<td>
{% if not c["enabled"] %}<span class="badge bad">停用</span>
{% elif c["cooldown_until"] > now %}<span class="badge cool">冷却中（剩余 {{ (c["cooldown_until"] - now) | int }}s）</span>
{% else %}<span class="badge ok">正常</span>{% endif %}
</td>
<td>{{ c["last_error"] or "" }}</td>
</tr>
{% endfor %}
</table>
<h2>最近请求</h2>
<table>
<tr><th>时间</th><th>协议</th><th>模型</th><th>状态</th><th>耗时</th><th></th></tr>
{% for l in logs %}
<tr>
<td>{{ l["created_at"] | round(0) | int }}</td><td>{{ l["client_protocol"] }}</td>
<td>{{ l["group_name"] }}</td>
<td><span class="badge {{ 'ok' if l["status"]=='success' else 'bad' }}">{{ l["status"] }}</span></td>
<td>{{ l["total_duration_ms"] }}ms</td>
<td><a href="/admin/logs/{{ l["id"] }}">详情</a></td>
</tr>
{% endfor %}
</table>
{% endblock %}
```

`app/admin/templates/channels.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>渠道 <a href="/admin/channels/new">+ 新建</a></h2>
<table>
<tr><th>ID</th><th>名称</th><th>协议</th><th>Base URL</th><th>密钥</th><th>操作</th></tr>
{% for c in channels %}
<tr>
<td>{{ c["id"] }}</td><td>{{ c["name"] }}</td><td>{{ c["protocol"] }}</td>
<td>{{ c["base_url"] }}</td>
<td>{{ "******" if c["api_key"] else "（空）" }}</td>
<td>
<a href="/admin/channels/{{ c["id"] }}/edit">编辑</a>
<form class="inline" method="post" action="/admin/channels/{{ c["id"] }}/toggle">
<button>{{ "停用" if c["enabled"] else "启用" }}</button></form>
<form class="inline" method="post" action="/admin/channels/{{ c["id"] }}/clear-cooldown">
<button>清除冷却</button></form>
<button onclick="testChannel({{ c["id"] }}, this)">测试</button>
<form class="inline" method="post" action="/admin/channels/{{ c["id"] }}/delete"
 onsubmit="return confirm('确认删除？')"><button>删除</button></form>
</td>
</tr>
{% endfor %}
</table>
<script>
async function testChannel(id, btn){
  btn.disabled = true; btn.textContent = "测试中…";
  try{
    const r = await fetch(`/admin/channels/${id}/test`, {method:"POST"});
    const j = await r.json();
    alert((j.ok ? "✅ 连通正常\n" : "❌ 失败\n") + j.detail);
  } finally { btn.disabled = false; btn.textContent = "测试"; }
}
</script>
{% endblock %}
```

`app/admin/templates/channel_form.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>{{ "编辑渠道" if ch else "新建渠道" }}</h2>
<form method="post" style="max-width:480px"
 action="{{ '/admin/channels/' ~ ch['id'] ~ '/edit' if ch else '/admin/channels' }}">
<label>名称</label>
<input name="name" required value="{{ ch['name'] if ch else '' }}">
<label>协议</label>
<select name="protocol">
<option value="anthropic" {{ "selected" if ch and ch["protocol"]=="anthropic" }}>anthropic</option>
<option value="openai" {{ "selected" if ch and ch["protocol"]=="openai" }}>openai</option>
</select>
<label>Base URL</label>
<input name="base_url" required placeholder="https://api.anthropic.com"
 value="{{ ch['base_url'] if ch else '' }}">
<label>API Key{{ "（留空不修改）" if ch else "" }}</label>
<input name="api_key" type="password" value="">
<button>保存</button>
</form>
{% endblock %}
```

在 `app/main.py` 挂载：
```python
from app.admin.routes import router as admin_router

app.include_router(admin_router)
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run pytest tests/test_admin_channels.py -v`
Expected: 3 passed

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "feat: 管理界面（仪表盘 + 渠道 CRUD + 连通性测试）"
```

---

### Task 14: 管理界面（二）——模型组与映射

**Files:**
- Modify: `app/admin/routes.py`（追加路由）
- Create: `app/admin/templates/groups.html`
- Create: `app/admin/templates/group_detail.html`
- Test: `tests/test_admin_groups.py`

**Interfaces:**
- Produces:
  - `GET/POST /admin/groups`、`POST /admin/groups/{gid}/delete`
  - `GET /admin/groups/{gid}`（映射编辑页）、`POST /admin/groups/{gid}/mappings`（新增/更新映射，以 channel_id 去重）、`POST /admin/mappings/{mid}/delete`、`POST /admin/mappings/{mid}/move?dir=up|down`（调优先级）
- Consumes: Task 13 `templates`

- [ ] **Step 1: 写失败测试 `tests/test_admin_groups.py`**

```python
import httpx

import app.db as db
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url) VALUES"
                 " (1,'a','anthropic','http://a'),(2,'b','openai','http://b')")
    conn.commit(); conn.close()


async def test_group_create_and_mapping(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/groups", data={"name": "claude-sonnet"})
        assert r.status_code == 303
        gid = db.connect().execute(
            "SELECT id FROM model_group").fetchone()["id"]
        r = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "claude-sonnet-4-5",
            "priority": "10", "supports_image": "on"})
        assert r.status_code == 303
        conn = db.connect()
        m = conn.execute("SELECT * FROM model_mapping").fetchone()
        assert m["supports_image"] == 1 and m["priority"] == 10
        conn.close()


async def test_mapping_move_priority(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping (id,group_id,channel_id,actual_model,priority)"
                 " VALUES (1,1,1,'a',10),(2,1,2,'b',20)")
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        await c.post("/admin/mappings/2/move?dir=up")
    conn = db.connect()
    rows = conn.execute("SELECT id,priority FROM model_mapping ORDER BY priority").fetchall()
    assert (rows[0]["id"], rows[1]["id"]) == (2, 1)
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_admin_groups.py -v`
Expected: FAIL（路由 404）

- [ ] **Step 3: 实现（追加到 `app/admin/routes.py`）**

```python
@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request):
    conn = connect()
    try:
        groups = conn.execute(
            "SELECT g.*, COUNT(m.id) AS n FROM model_group g"
            " LEFT JOIN model_mapping m ON m.group_id=g.id"
            " GROUP BY g.id ORDER BY g.id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("groups.html",
                                      {"request": request, "groups": groups})


@router.post("/groups")
async def group_create(name: str = Form(...)):
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO model_group (name) VALUES (?)", (name,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/groups/{gid}/delete")
async def group_delete(gid: int):
    conn = connect()
    try:
        conn.execute("DELETE FROM model_group WHERE id=?", (gid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/groups", status_code=303)


@router.get("/groups/{gid}", response_class=HTMLResponse)
async def group_detail(request: Request, gid: int):
    conn = connect()
    try:
        group = conn.execute("SELECT * FROM model_group WHERE id=?",
                             (gid,)).fetchone()
        mappings = conn.execute(
            "SELECT m.*, c.name AS channel_name, c.protocol"
            " FROM model_mapping m JOIN channel c ON c.id=m.channel_id"
            " WHERE m.group_id=? ORDER BY m.priority", (gid,)).fetchall()
        channels = conn.execute("SELECT * FROM channel ORDER BY id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("group_detail.html", {
        "request": request, "group": group, "mappings": mappings,
        "channels": channels})


@router.post("/groups/{gid}/mappings")
async def mapping_upsert(gid: int, channel_id: int = Form(...),
                         actual_model: str = Form(...),
                         priority: int = Form(100),
                         supports_image: str | None = Form(None),
                         supports_video: str | None = Form(None)):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO model_mapping (group_id,channel_id,actual_model,priority,"
            "supports_image,supports_video) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(group_id,channel_id) DO UPDATE SET"
            " actual_model=excluded.actual_model, priority=excluded.priority,"
            " supports_image=excluded.supports_image,"
            " supports_video=excluded.supports_video",
            (gid, channel_id, actual_model, priority,
             1 if supports_image else 0, 1 if supports_video else 0))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{gid}", status_code=303)


@router.post("/mappings/{mid}/delete")
async def mapping_delete(mid: int):
    conn = connect()
    try:
        row = conn.execute("SELECT group_id FROM model_mapping WHERE id=?",
                           (mid,)).fetchone()
        conn.execute("DELETE FROM model_mapping WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{row['group_id']}", status_code=303)


@router.post("/mappings/{mid}/move")
async def mapping_move(mid: int, dir: str = "up"):
    conn = connect()
    try:
        cur = conn.execute("SELECT * FROM model_mapping WHERE id=?",
                           (mid,)).fetchone()
        op = "<" if dir == "up" else ">"
        order = "DESC" if dir == "up" else "ASC"
        neighbor = conn.execute(
            f"SELECT * FROM model_mapping WHERE group_id=? AND priority {op} ?"
            f" ORDER BY priority {order} LIMIT 1",
            (cur["group_id"], cur["priority"])).fetchone()
        if neighbor is None:  # 同优先级相邻的情况：按 id 找
            neighbor = conn.execute(
                f"SELECT * FROM model_mapping WHERE group_id=? AND priority=?"
                f" AND id {'<' if dir == 'up' else '>'} ? ORDER BY id {order}"
                " LIMIT 1",
                (cur["group_id"], cur["priority"], mid)).fetchone()
        if neighbor is not None:
            conn.execute("UPDATE model_mapping SET priority=? WHERE id=?",
                         (neighbor["priority"], mid))
            conn.execute("UPDATE model_mapping SET priority=? WHERE id=?",
                         (cur["priority"], neighbor["id"]))
            conn.commit()
        gid = cur["group_id"]
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{gid}", status_code=303)
```

`app/admin/templates/groups.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>模型组</h2>
<form method="post" action="/admin/groups">
<input name="name" required placeholder="统一模型名，如 claude-sonnet" style="width:300px">
<button>新建</button>
</form>
<table>
<tr><th>名称（客户端填写的模型名）</th><th>渠道数</th><th>操作</th></tr>
{% for g in groups %}
<tr>
<td><a href="/admin/groups/{{ g["id"] }}">{{ g["name"] }}</a></td>
<td>{{ g["n"] }}</td>
<td>
<form class="inline" method="post" action="/admin/groups/{{ g["id"] }}/delete"
 onsubmit="return confirm('确认删除？')"><button>删除</button></form>
</td>
</tr>
{% endfor %}
</table>
{% endblock %}
```

`app/admin/templates/group_detail.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>模型组：{{ group["name"] }}</h2>
<table>
<tr><th>渠道</th><th>协议</th><th>真实模型名</th><th>优先级</th>
<th>图片</th><th>视频</th><th>操作</th></tr>
{% for m in mappings %}
<tr>
<td>{{ m["channel_name"] }}</td><td>{{ m["protocol"] }}</td>
<td>{{ m["actual_model"] }}</td><td>{{ m["priority"] }}</td>
<td>{{ "✓" if m["supports_image"] }}</td>
<td>{{ "✓" if m["supports_video"] }}</td>
<td>
<form class="inline" method="post" action="/admin/mappings/{{ m["id"] }}/move?dir=up"><button>↑</button></form>
<form class="inline" method="post" action="/admin/mappings/{{ m["id"] }}/move?dir=down"><button>↓</button></form>
<form class="inline" method="post" action="/admin/mappings/{{ m["id"] }}/delete"><button>删除</button></form>
</td>
</tr>
{% endfor %}
</table>
<h3>添加/更新映射</h3>
<form method="post" action="/admin/groups/{{ group["id"] }}/mappings" style="max-width:480px">
<label>渠道</label>
<select name="channel_id">
{% for c in channels %}
<option value="{{ c["id"] }}">{{ c["name"] }}（{{ c["protocol"] }}）</option>
{% endfor %}
</select>
<label>真实模型名</label>
<input name="actual_model" required placeholder="如 claude-sonnet-4-5 / deepseek-chat">
<label>优先级（越小越优先）</label>
<input name="priority" type="number" value="100">
<label><input type="checkbox" name="supports_image" style="width:auto"> 支持图片输入</label>
<label><input type="checkbox" name="supports_video" style="width:auto"> 支持视频输入</label>
<button>保存</button>
</form>
{% endblock %}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_admin_groups.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 模型组与映射管理界面"
```

---

### Task 15: 管理界面（三）——日志页 + 设置页（含特征库编辑）

特征库可编辑：`classify_error` 增加可选 patterns 参数，executor 从 settings 读取覆盖。

**Files:**
- Modify: `app/errors.py`（`classify_error` 加可选参数 `balance_patterns`, `capability_patterns`）
- Modify: `app/executor.py`（`_record_failure` 前把 settings 里的 patterns 传入 —— 在 `execute` 开头读取一次，经参数传给 `_try_*`；或直接在各 classify 调用点读）
- Modify: `app/admin/routes.py`（追加路由）
- Create: `app/admin/templates/logs.html`、`log_detail.html`、`settings.html`
- Test: `tests/test_admin_logs_settings.py`

**Interfaces:**
- Produces:
  - `GET /admin/logs`（过滤参数 `status`、`q` 模型名）、`GET /admin/logs/{log_id}`
  - `GET/POST /admin/settings`
  - settings 键：`cooldown_balance` / `cooldown_ratelimit` / `cooldown_auth` / `cooldown_server` / `api_key` / `balance_patterns` / `capability_patterns`（patterns 为换行分隔文本）
  - `classify_error(status, body, balance_patterns=None, capability_patterns=None)`（None 时用模块默认）
- Consumes: Task 3 errors、Task 13 templates

- [ ] **Step 1: 写失败测试 `tests/test_admin_logs_settings.py`**

```python
import httpx

import app.db as db
from app.errors import ErrorCategory, classify_error
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


def test_classify_with_custom_patterns():
    v = classify_error(400, "xxx 账户已冻结 xxx",
                       balance_patterns=["账户已冻结"])
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE
    # 默认 patterns 不命中 → 仍是 client error
    assert classify_error(400, "xxx 账户已冻结 xxx").category == ErrorCategory.CLIENT


async def test_logs_pages(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO request_log (created_at,client_protocol,group_name,"
                 "path,stream,status) VALUES (1.0,'anthropic','g','/v1/messages',0,"
                 "'failed')")
    conn.commit()
    log_id = conn.execute("SELECT id FROM request_log").fetchone()["id"]
    conn.execute("INSERT INTO attempt (log_id,created_at,channel_name,http_status,"
                 "error_summary) VALUES (?,1.0,'c1',402,'no money')", (log_id,))
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/logs?status=failed")
        assert r.status_code == 200 and "402" not in r.text  # 列表不含 attempt
        assert "'g'" in r.text or ">g<" in r.text
        r = await c.get(f"/admin/logs/{log_id}")
        assert r.status_code == 200 and "no money" in r.text


async def test_settings_page(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/settings", data={
            "cooldown_balance": "300", "cooldown_ratelimit": "45",
            "cooldown_auth": "1800", "cooldown_server": "60",
            "api_key": "k", "balance_patterns": "余额不足\nquota",
            "capability_patterns": "not support image"})
        assert r.status_code == 303
        conn = db.connect()
        assert db.get_setting(conn, "cooldown_balance", "") == "300"
        assert "quota" in db.get_setting(conn, "balance_patterns", "")
        conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_admin_logs_settings.py -v`
Expected: FAIL（新签名/路由不存在）

- [ ] **Step 3: 实现**

`app/errors.py` 中把 `classify_error` 改为（其余不变）：
```python
def classify_error(status: int | None, body: str,
                   balance_patterns: list[str] | None = None,
                   capability_patterns: list[str] | None = None) -> Verdict:
    balance_patterns = balance_patterns or BALANCE_PATTERNS
    capability_patterns = capability_patterns or CAPABILITY_PATTERNS
    body_lower = (body or "").lower()
    # ... 原有逻辑，把 BALANCE_PATTERNS 替换为 balance_patterns，
    # CAPABILITY_PATTERNS 替换为 capability_patterns
```

`app/executor.py`：新增辅助并在 `execute` 开头读取 patterns，传到 `_try_non_stream` / `_try_stream` 的 classify 调用点：
```python
def _patterns(conn) -> tuple[list[str] | None, list[str] | None]:
    def load(key: str) -> list[str] | None:
        raw = get_setting(conn, key, "")
        lines = [ln.strip().lower() for ln in raw.splitlines() if ln.strip()]
        return lines or None
    return load("balance_patterns"), load("capability_patterns")
```
`execute` 开头：`bp, cp = _patterns(conn)`；`_try_non_stream/_try_stream` 增加 `patterns` 参数，内部 `classify_error(status, text, bp, cp)` 与网络错误分支同理（网络分支无 body，patterns 不影响，可直接调原式）。

`app/admin/routes.py` 追加：
```python
@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, status: str = "", q: str = ""):
    sql = "SELECT * FROM request_log WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"; args.append(status)
    if q:
        sql += " AND group_name LIKE ?"; args.append(f"%{q}%")
    sql += " ORDER BY id DESC LIMIT 200"
    conn = connect()
    try:
        logs = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("logs.html",
                                      {"request": request, "logs": logs,
                                       "status": status, "q": q})


@router.get("/logs/{log_id}", response_class=HTMLResponse)
async def log_detail(request: Request, log_id: int):
    conn = connect()
    try:
        log = conn.execute("SELECT * FROM request_log WHERE id=?",
                           (log_id,)).fetchone()
        attempts = conn.execute(
            "SELECT * FROM attempt WHERE log_id=? ORDER BY id",
            (log_id,)).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse("log_detail.html", {
        "request": request, "log": log, "attempts": attempts})


_SETTING_KEYS = ["cooldown_balance", "cooldown_ratelimit", "cooldown_auth",
                 "cooldown_server", "api_key", "balance_patterns",
                 "capability_patterns"]


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.db import get_setting
    from app.errors import BALANCE_PATTERNS, CAPABILITY_PATTERNS
    conn = connect()
    try:
        values = {k: get_setting(conn, k, "") for k in _SETTING_KEYS}
    finally:
        conn.close()
    if not values["balance_patterns"]:
        values["balance_patterns"] = "\n".join(BALANCE_PATTERNS)
    if not values["capability_patterns"]:
        values["capability_patterns"] = "\n".join(CAPABILITY_PATTERNS)
    return templates.TemplateResponse("settings.html",
                                      {"request": request, "v": values})


@router.post("/settings")
async def settings_save(request: Request):
    from app.db import set_setting
    form = await request.form()
    conn = connect()
    try:
        for k in _SETTING_KEYS:
            set_setting(conn, k, str(form.get(k, "")))
    finally:
        conn.close()
    return RedirectResponse("/admin/settings", status_code=303)
```

`app/admin/templates/logs.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>请求日志</h2>
<form method="get">
<input name="q" placeholder="模型名过滤" value="{{ q }}" style="width:200px">
<select name="status" style="width:140px">
<option value="">全部状态</option>
<option value="success" {{ "selected" if status=="success" }}>success</option>
<option value="failed" {{ "selected" if status=="failed" }}>failed</option>
</select>
<button>过滤</button>
</form>
<table>
<tr><th>ID</th><th>时间</th><th>协议</th><th>模型</th><th>流式</th>
<th>状态</th><th>耗时</th><th>tokens(in/out)</th><th></th></tr>
{% for l in logs %}
<tr>
<td>{{ l["id"] }}</td><td>{{ l["created_at"] | round(0) | int }}</td>
<td>{{ l["client_protocol"] }}</td><td>{{ l["group_name"] }}</td>
<td>{{ "是" if l["stream"] }}</td>
<td><span class="badge {{ 'ok' if l["status"]=='success' else 'bad' }}">{{ l["status"] }}</span></td>
<td>{{ l["total_duration_ms"] }}ms</td>
<td>{{ l["input_tokens"] }}/{{ l["output_tokens"] }}</td>
<td><a href="/admin/logs/{{ l["id"] }}">详情</a></td>
</tr>
{% endfor %}
</table>
{% endblock %}
```

`app/admin/templates/log_detail.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>请求 #{{ log["id"] }}（{{ log["status"] }}）</h2>
<p>协议: {{ log["client_protocol"] }} ｜ 模型: {{ log["group_name"] }} ｜
路径: {{ log["path"] }} ｜ 耗时: {{ log["total_duration_ms"] }}ms</p>
<h3>尝试链</h3>
<table>
<tr><th>#</th><th>渠道</th><th>真实模型</th><th>转换</th><th>HTTP</th>
<th>错误类别</th><th>摘要</th></tr>
{% for a in attempts %}
<tr>
<td>{{ loop.index }}</td>
<td>{{ a["channel_name"] or "-" }}{{ "（跳过: " ~ a["skipped"] ~ "）" if a["skipped"] }}</td>
<td>{{ a["actual_model"] or "-" }}</td>
<td>{{ "是" if a["converted"] else "否" }}</td>
<td>{{ a["http_status"] or "-" }}</td>
<td>{{ a["error_category"] or "-" }}</td>
<td>{{ a["error_summary"] or "-" }}</td>
</tr>
{% if a["raw_error"] %}
<tr><td></td><td colspan="6"><pre style="white-space:pre-wrap;font-size:12px">{{ a["raw_error"] }}</pre></td></tr>
{% endif %}
{% endfor %}
</table>
{% endblock %}
```

`app/admin/templates/settings.html`：
```html
{% extends "base.html" %}
{% block content %}
<h2>设置</h2>
<form method="post" style="max-width:560px">
<label>余额不足冷却秒数</label><input name="cooldown_balance" value="{{ v['cooldown_balance'] or '600' }}">
<label>限流冷却秒数</label><input name="cooldown_ratelimit" value="{{ v['cooldown_ratelimit'] or '60' }}">
<label>鉴权失败冷却秒数</label><input name="cooldown_auth" value="{{ v['cooldown_auth'] or '1800' }}">
<label>服务器错误冷却秒数</label><input name="cooldown_server" value="{{ v['cooldown_server'] or '60' }}">
<label>网关访问令牌（留空则不校验；客户端用它作 api key）</label>
<input name="api_key" value="{{ v['api_key'] }}">
<label>余额不足特征（每行一条，小写匹配）</label>
<textarea name="balance_patterns" rows="6" style="width:100%">{{ v["balance_patterns"] }}</textarea>
<label>能力不支持特征（每行一条，小写匹配）</label>
<textarea name="capability_patterns" rows="6" style="width:100%">{{ v["capability_patterns"] }}</textarea>
<button>保存</button>
</form>
{% endblock %}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_admin_logs_settings.py tests/test_errors.py tests/test_executor.py -v`
Expected: 全部 PASS（确认 errors 默认值行为不变、executor 测试不回归）

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 日志页与设置页（特征库可编辑）"
```

---

### Task 16: 端到端验证 + README + 全量回归

**Files:**
- Create: `tests/test_e2e.py`
- Create: `README.md`

**Interfaces:**
- Consumes: 全部前序任务

- [ ] **Step 1: 写 e2e 测试 `tests/test_e2e.py`（经 HTTP 全链路：Anthropic 入口 → OpenAI 渠道，且首渠道 402 触发切换）**

```python
import json

import httpx

import app.db as db
import app.executor as ex
from app.main import app


async def test_e2e_anthropic_client_failover_to_openai_channel(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key) VALUES"
                 " (1,'broke-claude','anthropic','http://a','k1'),"
                 " (2,'deepseek','openai','http://b','k2')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'my-claude')")
    conn.execute("INSERT INTO model_mapping"
                 " (group_id,channel_id,actual_model,priority) VALUES"
                 " (1,1,'claude-sonnet-4-5',1),(1,2,'deepseek-chat',2)")
    conn.commit(); conn.close()

    calls = []

    def upstream(request):
        calls.append(str(request.url))
        if "http://a/" in str(request.url):
            return httpx.Response(402, json={
                "error": {"message": "Insufficient Balance"}})
        # OpenAI 渠道：验证收到的是转换后的 OpenAI 格式
        body = json.loads(request.content)
        assert body["model"] == "deepseek-chat"
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={
            "id": "c1", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "你好"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    monkeypatch.setattr(ex, "_transport", httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/v1/messages", json={
            "model": "my-claude", "max_tokens": 100, "system": "你是助手",
            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"  # 客户端拿到 Anthropic 格式
    assert body["content"][0] == {"type": "text", "text": "你好"}
    assert len(calls) == 2  # 第一次 402，第二次成功
    conn = db.connect()
    log = conn.execute("SELECT * FROM request_log").fetchone()
    assert log["status"] == "success"
    attempts = conn.execute("SELECT * FROM attempt ORDER BY id").fetchall()
    assert attempts[0]["error_category"] == "insufficient_balance"
    assert attempts[1]["converted"] == 1
    conn.close()
```

- [ ] **Step 2: 运行确认通过**

Run: `uv run pytest tests/test_e2e.py -v`
Expected: 1 passed

- [ ] **Step 3: 写 `README.md`**

```markdown
# llm-apig — 个人 LLM API 网关

本机部署的大模型 API 网关：多渠道优先级、余额耗尽/限流/能力不匹配时自动切换，
兼容 Anthropic（优先）与 OpenAI 协议，支持双向协议转换。

## 快速开始

1. 安装 [uv](https://docs.astral.sh/uv/)
2. 双击 `start.bat`（或 `uv run python -m app.main`）
3. 浏览器自动打开管理界面 http://127.0.0.1:8317/admin

## 使用

1. **渠道**：添加云产商接入点（协议/base_url/api_key），可用「测试」按钮验证连通性
2. **模型组**：建统一模型名（如 `my-claude`），把渠道加进组里，设真实模型名、
   优先级（越小越优先）、图片/视频能力标记
3. **客户端指向网关**：
   - Anthropic 协议：`ANTHROPIC_BASE_URL=http://127.0.0.1:8317`，模型填统一模型名
   - OpenAI 协议：`base_url=http://127.0.0.1:8317/v1`
   - 若在「设置」里配置了访问令牌，客户端 api key 填该令牌；否则任意

## 切换规则

能力过滤（图片/视频）→ 同协议优先 → 优先级 → 冷却排最后。
余额不足(402/特征匹配)冷却 10 分钟、限流 60s、鉴权失败 30 分钟、5xx 60s，
均可在设置页调整。流式响应一旦开始则不再重试（避免重复内容）。

## 开发

`uv run pytest tests/ -v`
```

- [ ] **Step 4: 全量回归**

Run: `uv run pytest tests/ -v`
Expected: 全部 PASS（约 40 个测试）

手动冒烟（可选）：`uv run python -m app.main`，浏览器打开 `/admin`，建渠道+模型组，curl 验证：
```
curl -X POST http://127.0.0.1:8317/v1/messages -H "content-type: application/json" -d "{\"model\":\"<组名>\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "test+docs: 端到端测试与 README"
```

---

## 自检记录

- Spec 覆盖：配置界面(T13-15) ✓ 优先级(T4/T14) ✓ 能力切换(T4/T10) ✓ 双协议+转换(T5-8/T11/T12) ✓ uv(T1) ✓ 日志(T9/T15) ✓ 冷却(T9/T10) ✓ count_tokens(T11) ✓ 特征库可维护(T15) ✓ v2 Agent 预留（unclassified + raw_error，T3/T9）✓
- 已知限制（与 spec 一致）：流式成功路径的 usage 记 NULL；`thinking`/`cache_control` 仅同协议透传
