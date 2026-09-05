# 模型组最大尝试次数配置化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `app/executor.py` 中写死的 `MAX_ATTEMPTS = 5` 改为存于 settings 表、可在管理端设置页修改的全局配置项 `max_attempts`，默认值 5。

**Architecture:** 复用现有 settings 体系（SQLite `settings` 键值表 + `get_setting`/`set_setting` + `/admin/settings` 白名单表单）。executor 每次请求时经 `_max_attempts(conn)` 实时读取，非法值回退 5。

**Tech Stack:** Python 3.12+ / FastAPI / SQLite / Jinja2 模板 / pytest + pytest-asyncio（`asyncio_mode = "auto"`，异步测试无需装饰器）。

**Spec:** `docs/superpowers/specs/2026-09-05-max-attempts-config-design.md`

## Global Constraints

- 测试命令统一为 `uv run pytest tests/ -v`（单文件：`uv run pytest tests/<file> -v`）。
- 提交信息结尾附 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 代码注释与 UI 文案用中文，与现有代码风格一致。
- 默认值必须为 5（与现状行为一致）；读取到非数字、空值或数值 < 1 时回退 5；不设上限钳制。
- 无 schema 变更、无迁移、无桌面端（Tauri）改动。

---

### Task 1: executor 从 settings 读取最大尝试次数

**Files:**
- Modify: `app/executor.py:14`（删常量）、`app/executor.py:98-106` 附近（加 `_max_attempts`）、`app/executor.py:147`（改切片）
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `app.db.get_setting(conn, key, default) -> str`（已存在）；`app.db.set_setting(conn, key, value)`（测试用来写入配置）
- Produces: `app.executor._max_attempts(conn) -> int`——返回 settings 表 `max_attempts` 键解析后的正整数；非数字、空值、< 1 均返回 5。`execute()` 的候选尝试上限由它决定。

- [ ] **Step 1: 写失败测试——配置限制故障转移次数**

在 `tests/test_executor.py` 末尾（`test_render_sse_formats` 之前）追加：

```python
async def test_max_attempts_setting_limits_failover(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    for ch in range(1, 4):
        _add_channel(conn, ch, f"c{ch}", "anthropic", ch)
    db.set_setting(conn, "max_attempts", "2")
    transport, calls = _mock([(500, {"e": "x"})] * 3)
    monkeypatch.setattr(ex, "_transport", transport)
    r = await execute(conn, entry_protocol="anthropic", group_name="g",
                      payload={"model": "g", "messages": []}, stream=False)
    assert r.status == 500
    assert len(calls) == 2  # 3 个候选只尝试了 2 个
    n = conn.execute(
        "SELECT COUNT(*) c FROM attempt WHERE skipped IS NULL"
    ).fetchone()["c"]
    assert n == 2
    conn.close()
```

- [ ] **Step 2: 写失败测试——非法值回退默认 5 次**

紧接上一条测试后追加（每个非法值用独立数据库目录，避免冷却状态互相污染）：

```python
async def test_max_attempts_invalid_values_fall_back(tmp_path, monkeypatch):
    for i, bad in enumerate(["abc", "0"]):
        conn = _conn(tmp_path / str(i), monkeypatch)
        for ch in range(1, 7):
            _add_channel(conn, ch, f"c{ch}", "anthropic", ch)
        db.set_setting(conn, "max_attempts", bad)
        transport, calls = _mock([(500, {"e": "x"})] * 6)
        monkeypatch.setattr(ex, "_transport", transport)
        r = await execute(conn, entry_protocol="anthropic", group_name="g",
                          payload={"model": "g", "messages": []}, stream=False)
        assert r.status == 500
        assert len(calls) == 5, f"max_attempts={bad!r} 应回退为 5 次"
        conn.close()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_executor.py -v -k max_attempts`
Expected: FAIL —— 两条用例均失败：`len(calls)` 为 3（第一条）和 6（第二条），因为当前代码仍是写死的切片逻辑尚不受 settings 控制（`max_attempts` 键无人读取）。

- [ ] **Step 4: 实现 `_max_attempts` 并接入 execute**

`app/executor.py` 第 14 行，删除 `MAX_ATTEMPTS = 5`。

在 `_cooldown_seconds` 函数（约第 98-106 行）之后新增：

```python
def _max_attempts(conn) -> int:
    try:
        n = int(get_setting(conn, "max_attempts", "5"))
        return n if n >= 1 else 5
    except ValueError:
        return 5
```

`execute()` 中（约第 147 行）：

```python
    for cand in candidates[:MAX_ATTEMPTS]:
```

改为：

```python
    for cand in candidates[:_max_attempts(conn)]:
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_executor.py -v`
Expected: PASS —— 全部用例通过，包括既有用例（未配置时默认 5 次，行为不变）。

- [ ] **Step 6: 提交**

```bash
git add app/executor.py tests/test_executor.py
git commit -m "feat: 模型组最大尝试次数改为从 settings 读取

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 管理端设置页支持修改 max_attempts

**Files:**
- Modify: `app/admin/routes.py:366-368`（`_SETTING_KEYS` 加键）
- Modify: `app/admin/templates/settings.html:32` 之后（新增"失败重试"卡片）
- Test: `tests/test_admin_logs_settings.py:41-55`（扩展 `test_settings_page`）

**Interfaces:**
- Consumes: Task 1 的 `_max_attempts(conn)`（运行时读取 `max_attempts` 键，本任务不改其逻辑）
- Produces: settings 表 `max_attempts` 键可由 `/admin/settings` 表单写入与回显；`_SETTING_KEYS` 包含 `"max_attempts"`。

- [ ] **Step 1: 扩展测试——POST 携带 max_attempts 并断言回显**

`tests/test_admin_logs_settings.py` 的 `test_settings_page`（第 41-55 行）整体替换为：

```python
async def test_settings_page(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/settings", data={
            "cooldown_balance": "300", "cooldown_ratelimit": "45",
            "cooldown_auth": "1800", "cooldown_server": "60",
            "api_key": "k", "balance_patterns": "余额不足\nquota",
            "capability_patterns": "not support image",
            "max_attempts": "8"})
        assert r.status_code == 303
        conn = db.connect()
        assert db.get_setting(conn, "cooldown_balance", "") == "300"
        assert "quota" in db.get_setting(conn, "balance_patterns", "")
        assert db.get_setting(conn, "max_attempts", "") == "8"
        conn.close()
        r = await c.get("/admin/settings")
        assert r.status_code == 200 and 'name="max_attempts"' in r.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_admin_logs_settings.py::test_settings_page -v`
Expected: FAIL —— `get_setting(conn, "max_attempts", "")` 返回 `""`（白名单未含该键，POST 被忽略）。

- [ ] **Step 3: 实现——白名单加键 + 模板加卡片**

`app/admin/routes.py` 第 366-368 行：

```python
_SETTING_KEYS = ["cooldown_balance", "cooldown_ratelimit", "cooldown_auth",
                 "cooldown_server", "api_key", "balance_patterns",
                 "capability_patterns"]
```

改为：

```python
_SETTING_KEYS = ["cooldown_balance", "cooldown_ratelimit", "cooldown_auth",
                 "cooldown_server", "api_key", "balance_patterns",
                 "capability_patterns", "max_attempts"]
```

`app/admin/templates/settings.html`：在"冷却时间"卡片的闭合 `</div>`（第 32 行）之后、"访问鉴权"卡片（第 34 行）之前，插入：

```html
<div class="card form-card" style="margin-bottom:22px">
  <h3 style="margin-top:0">失败重试</h3>
  <div class="field">
    <label>最大尝试次数 <span class="hint">次/请求</span></label>
    <input name="max_attempts" type="number" min="1" value="{{ v['max_attempts'] or '5' }}">
    <span class="hint">每个请求最多尝试的渠道数</span>
  </div>
</div>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_admin_logs_settings.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归**

Run: `uv run pytest tests/ -v`
Expected: PASS —— 所有测试文件全部通过。

- [ ] **Step 6: 提交**

```bash
git add app/admin/routes.py app/admin/templates/settings.html tests/test_admin_logs_settings.py
git commit -m "feat: 设置页支持配置模型组最大尝试次数

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
