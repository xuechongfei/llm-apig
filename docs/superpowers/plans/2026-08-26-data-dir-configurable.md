# 数据目录可配置 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置页面增加数据目录配置项，允许用户修改数据存储路径，迁移现有数据并重启 daemon 生效。

**Architecture:** 引入固定位置的引导配置文件 `%APPDATA%\llm-apig\config.json`（存储自定义 `data_dir`），打破"DB 路径依赖 DB 自身"的循环。Rust 壳和 Python daemon 启动时均读取该文件确定数据目录；修改路径时通过 `.restart` 标记文件触发壳重启 daemon。

**Tech Stack:** Python (FastAPI, sqlite3, pathlib), Rust (tauri, serde_json), Jinja2 模板, vanilla JavaScript

## Global Constraints

- 所有路径操作使用 `pathlib.Path`（Python）和 `std::path::PathBuf`（Rust）
- `config.json` 始终位于 `%APPDATA%\llm-apig\`（默认位置，不随数据迁移）
- `.restart` 标记文件位于 `%APPDATA%\llm-apig\.restart`（与 config.json 同目录）
- 数据库迁移前先关闭当前连接，迁移后由新 daemon 实例打开
- 环境变量 `LLMAPIG_DATA_DIR` 优先级始终最高（壳注入，不变）
- 新代码遵循现有测试模式：`monkeypatch` + `tmp_path`（pytest）

---

### Task 1: 新增 `desktop/config.py` — 引导配置读写

**Files:**
- Create: `desktop/config.py`
- Create: `tests/desktop/test_config.py`

**Interfaces:**
- Produces:
  - `default_data_dir() -> Path` — 始终返回 `%APPDATA%/llm-apig`
  - `config_file() -> Path` — `default_data_dir() / "config.json"`（延迟求值，测试友好）
  - `load_config() -> dict` — 读取 config.json，文件不存在或 JSON 非法时返回 `{}`
  - `save_config(data: dict) -> None` — 写入 config.json（自动创建父目录）
  - `get_data_dir() -> str | None` — 读取 config 中的 `data_dir` 字段
  - `set_data_dir(path: str) -> None` — 写入 `data_dir` 到 config.json

- [ ] **Step 1: 编写测试**

```python
# tests/desktop/test_config.py
import json
from desktop import config


def test_default_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.default_data_dir() == tmp_path / "llm-apig"


def test_load_config_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # 确保 config.json 不存在
    assert config.load_config() == {}


def test_load_config_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": "D:\\MyData"}), encoding="utf-8")
    assert config.load_config() == {"data_dir": "D:\\MyData"}


def test_load_config_corrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("not json{{{", encoding="utf-8")
    assert config.load_config() == {}


def test_save_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config.save_config({"data_dir": "D:\\Test"})
    loaded = json.loads(
        (tmp_path / "llm-apig" / "config.json").read_text(encoding="utf-8"))
    assert loaded == {"data_dir": "D:\\Test"}


def test_get_data_dir_none(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.get_data_dir() is None


def test_get_data_dir_set(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": "E:\\Elsewhere"}), encoding="utf-8")
    assert config.get_data_dir() == "E:\\Elsewhere"


def test_set_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config.set_data_dir("F:\\NewPath")
    assert config.get_data_dir() == "F:\\NewPath"


def test_set_data_dir_overwrites(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": "old", "other": "keep"}), encoding="utf-8")
    config.set_data_dir("new")
    result = config.load_config()
    assert result["data_dir"] == "new"
    # 其他字段保留
    assert result.get("other") == "keep"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/desktop/test_config.py -v
```
Expected: 全部 FAIL（模块不存在）

- [ ] **Step 3: 实现 `desktop/config.py`**

```python
"""引导配置读写 —— 固定位置 %APPDATA%/llm-apig/config.json。

该文件始终位于默认数据目录，不随用户配置的 data_dir 迁移。
应用启动时先读此文件确定数据目录，再打开数据库。
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """始终返回默认数据目录 %APPDATA%/llm-apig（不随配置变化）。"""
    return Path(os.environ["APPDATA"]) / "llm-apig"


def config_file() -> Path:
    """返回 config.json 的完整路径（延迟求值，测试可通过 monkeypatch 覆盖 APPDATA）。"""
    return default_data_dir() / "config.json"


def load_config() -> dict:
    """读取 config.json，文件不存在或 JSON 非法时返回空 dict。"""
    cf = config_file()
    try:
        if not cf.exists():
            return {}
        with open(cf, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("config.json 读取失败: %s，回退到默认配置", e)
        return {}


def save_config(data: dict) -> None:
    """写入 config.json（自动创建父目录）。"""
    cf = config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    with open(cf, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_data_dir() -> str | None:
    """读取用户配置的自定义数据目录，无配置时返回 None。"""
    cfg = load_config()
    val = cfg.get("data_dir")
    return val if isinstance(val, str) and val else None


def set_data_dir(path: str) -> None:
    """写入自定义数据目录路径到 config.json（保留其他字段）。"""
    cfg = load_config()
    cfg["data_dir"] = path
    save_config(cfg)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/desktop/test_config.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add desktop/config.py tests/desktop/test_config.py
git commit -m "feat: 新增引导配置模块 desktop/config.py

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 修改 `desktop/paths.py` — 集成 config.json

**Files:**
- Modify: `desktop/paths.py`
- Modify: `tests/desktop/test_paths.py`

**Interfaces:**
- Consumes: `desktop.config.get_data_dir()` (Task 1), `desktop.config.default_data_dir()` (Task 1)
- Produces: `data_dir() -> Path` — 优先级：`LLMAPIG_DATA_DIR` 环境变量 > `config.json` 的 `data_dir` > `%APPDATA%/llm-apig`

- [ ] **Step 1: 更新测试**

```python
# tests/desktop/test_paths.py
import json
from desktop import paths


def test_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_data_dir_appdata_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMAPIG_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths.data_dir() == tmp_path / "llm-apig"


def test_log_dir_under_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path))
    assert paths.log_dir() == tmp_path / "logs"


def test_data_dir_from_config(monkeypatch, tmp_path):
    """config.json 中的 data_dir 生效（无环境变量时）。"""
    monkeypatch.delenv("LLMAPIG_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": str(tmp_path / "custom")}), encoding="utf-8")
    assert paths.data_dir() == tmp_path / "custom"


def test_data_dir_env_beats_config(monkeypatch, tmp_path):
    """环境变量优先于 config.json。"""
    env_dir = tmp_path / "env-dir"
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(env_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": str(tmp_path / "ignored")}), encoding="utf-8")
    assert paths.data_dir() == env_dir
```

- [ ] **Step 2: 运行测试确认新测试失败**

```bash
python -m pytest tests/desktop/test_paths.py::test_data_dir_from_config tests/desktop/test_paths.py::test_data_dir_env_beats_config -v
```
Expected: 2 FAIL（旧 3 个 PASS 保持不变）

- [ ] **Step 3: 修改 `desktop/paths.py`**

```python
import logging
import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("LLMAPIG_DATA_DIR")
    if env:
        return Path(env)
    from desktop.config import get_data_dir, default_data_dir
    custom = get_data_dir()
    if custom:
        return Path(custom)
    return default_data_dir()


def log_dir() -> Path:
    return data_dir() / "logs"


def setup_logging() -> None:
    log_dir().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_dir() / "app.log", encoding="utf-8")],
    )
```

- [ ] **Step 4: 运行全部测试确认通过**

```bash
python -m pytest tests/desktop/test_paths.py -v
```
Expected: 全部 5 个 PASS

- [ ] **Step 5: 提交**

```bash
git add desktop/paths.py tests/desktop/test_paths.py
git commit -m "feat: paths.py 集成 config.json 读取数据目录

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 修改 `src-tauri/src/daemon.rs` — Rust 侧 config.json 读取

**Files:**
- Modify: `src-tauri/src/daemon.rs`

**Interfaces:**
- Produces: `default_data_dir() -> PathBuf` — 始终返回 `%APPDATA%/llm-apig`
- Modifies: `data_dir() -> PathBuf` — 增加从 config.json 读取的逻辑
- Modifies: `spawn_crash_watcher` — 增加 `.restart` 标记检查

- [ ] **Step 1: 编译当前代码确认基线通过**

```bash
cd src-tauri && cargo check 2>&1
```
Expected: 编译通过（无错误）

- [ ] **Step 2: 修改 `data_dir()` 和相关函数**

在 `daemon.rs` 的 `data_dir()` 函数（第 96-103 行）之前添加 `default_data_dir()`，然后修改 `data_dir()`：

```rust
/// 默认数据目录（始终为 %APPDATA%\llm-apig，不随配置变化）。
fn default_data_dir() -> PathBuf {
    let appdata = std::env::var("APPDATA").unwrap_or_default();
    PathBuf::from(appdata).join("llm-apig")
}

/// 数据目录：环境变量优先（冒烟自检用），其次读 config.json，
/// 缺省 %APPDATA%\llm-apig。
pub(crate) fn data_dir() -> PathBuf {
    if let Ok(d) = std::env::var("LLMAPIG_DATA_DIR") {
        return PathBuf::from(d);
    }
    // 从默认位置的 config.json 读取自定义路径
    let config_path = default_data_dir().join("config.json");
    if let Ok(content) = fs::read_to_string(&config_path) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&content) {
            if let Some(dir) = v.get("data_dir").and_then(|d| d.as_str()) {
                let p = PathBuf::from(dir);
                if !p.as_os_str().is_empty() {
                    return p;
                }
            }
        }
    }
    default_data_dir()
}
```

- [ ] **Step 3: 修改 `spawn_crash_watcher` 增加 `.restart` 标记检查**

在 `spawn_crash_watcher` 函数中，将 `Ok(Some(status)) if status.code() == Some(0)` 分支改为先检查 `.restart` 标记。找到第 472-477 行附近的代码：

替换原代码块：
```rust
                match waited {
                    Ok(Some(status)) if status.code() == Some(0) => {
                        // 正常退出（如 POST /shutdown）：不重启，只退役监控
                        log_to_desktop("daemon 正常退出(code 0)，不再监控");
                        return;
                    }
                    Ok(Some(status)) => {
                        if !restart_budget {
                            log_to_desktop(&format!(
                                "daemon 再次崩溃({})，重启预算已用尽，停止自动重启",
                                exit_status_detail(&status)));
                            return;
                        }
                        // ... 重启逻辑
```

改为：
```rust
                match waited {
                    Ok(Some(status)) => {
                        // 检查 .restart 标记（用户主动请求重启）
                        let restart_marker = default_data_dir().join(".restart");
                        if restart_marker.exists() {
                            let _ = fs::remove_file(&restart_marker);
                            log_to_desktop("检测到 .restart 标记，主动重启 daemon");
                            // take 出旧 handle
                            let old = guard.take();
                            drop(old);
                            drop(guard);
                            match DaemonHandle::spawn(&handle) {
                                Ok(dh) => {
                                    let new_port = dh.port();
                                    match state.daemon.lock() {
                                        Ok(mut g) => *g = Some(dh),
                                        Err(_) => {
                                            log_to_desktop(
                                                "AppState 锁中毒，新 daemon 已停止，监控退役");
                                            return;
                                        }
                                    }
                                    if let Some(w) = handle.get_webview_window("main") {
                                        let _ = w.eval(&format!(
                                            "window.location.replace('http://127.0.0.1:{}/admin')",
                                            new_port));
                                    }
                                }
                                Err(e) => log_to_desktop(&format!(
                                    ".restart 重启失败: {}", e)),
                            }
                            return;
                        }
                        // 无 .restart 标记，走原有逻辑
                        if status.code() == Some(0) {
                            log_to_desktop("daemon 正常退出(code 0)，不再监控");
                            return;
                        }
                        if !restart_budget {
                            log_to_desktop(&format!(
                                "daemon 再次崩溃({})，重启预算已用尽，停止自动重启",
                                exit_status_detail(&status)));
                            return;
                        }
                        // ... 原有崩溃重启逻辑保持不变
```

注意：原有的崩溃重启逻辑（`log_to_desktop("daemon 崩溃...")` 及之后的代码）保持不动，只在它之前插入 `.restart` 检查块。

- [ ] **Step 4: 编译验证**

```bash
cd src-tauri && cargo check 2>&1
```
Expected: 编译通过

- [ ] **Step 5: 提交**

```bash
git add src-tauri/src/daemon.rs
git commit -m "feat: Rust 侧集成 config.json 读取 + .restart 标记重启

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 修改 `desktop/daemon.py` — 重启端点 + 清理残留标记

**Files:**
- Modify: `desktop/daemon.py`
- Create: `tests/desktop/test_restart.py`

**Interfaces:**
- Consumes: `desktop.config.default_data_dir()` (Task 1)
- Produces: `POST /admin/settings/restart` 端点，写入 `.restart` 标记后触发 daemon 退出

- [ ] **Step 1: 编写测试**

```python
# tests/desktop/test_restart.py
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db


def test_restart_endpoint_writes_marker(tmp_path, monkeypatch):
    """POST /admin/settings/restart 写入 .restart 标记并返回 ok。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "gateway.db")
    db.init_db()

    from app.main import app
    from desktop.daemon import attach_restart_handler

    # 模拟 uvicorn Server（仅需 should_exit 属性）
    class FakeServer:
        should_exit = False

    server = FakeServer()
    attach_restart_handler(app, server)

    client = TestClient(app)
    resp = client.post("/admin/settings/restart")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "重启" in data["message"]

    # 验证 .restart 标记文件已创建
    marker = tmp_path / "llm-apig" / ".restart"
    assert marker.exists()


def test_restart_triggers_server_exit(tmp_path, monkeypatch):
    """重启端点应设置 server.should_exit = True（由定时器延迟触发）。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "gateway.db")
    db.init_db()

    import time
    from app.main import app
    from desktop.daemon import attach_restart_handler

    class FakeServer:
        should_exit = False

    server = FakeServer()
    attach_restart_handler(app, server)

    client = TestClient(app)
    client.post("/admin/settings/restart")
    # 立即检查：should_exit 应为 False（0.2s 延迟）
    assert server.should_exit is False
    # 等待 0.3s 后应为 True
    time.sleep(0.3)
    assert server.should_exit is True
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/desktop/test_restart.py -v
```
Expected: 全部 FAIL（`attach_restart_handler` 不存在）

- [ ] **Step 3: 修改 `desktop/daemon.py`**

在 `attach_shutdown` 函数之后添加 `attach_restart_handler`：

```python
def attach_restart_handler(app, server: uvicorn.Server) -> None:
    """POST /admin/settings/restart：写入 .restart 标记后优雅退出。

    壳的崩溃监控线程检测到 .restart 标记后会重启 daemon（绕过重启预算）。
    """
    from desktop.config import default_data_dir

    @app.post("/admin/settings/restart")
    async def restart():
        marker = default_data_dir() / ".restart"
        marker.write_text("1", encoding="utf-8")
        threading.Timer(
            0.2, lambda: setattr(server, "should_exit", True)).start()
        return {"ok": True, "message": "正在重启..."}
```

在 `main()` 函数中，`attach_shutdown(app, token, server)` 之后、`server.run()` 之前添加：

```python
    attach_shutdown(app, token, server)
    attach_restart_handler(app, server)

    # 清理残留的 .restart 标记（上次重启可能未完成）
    from desktop.config import default_data_dir
    marker = default_data_dir() / ".restart"
    if marker.exists():
        marker.unlink()
        import logging
        logging.getLogger(__name__).info("已清理残留的 .restart 标记")

    server.run()  # 阻塞；should_exit 后返回
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/desktop/test_restart.py -v
```
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add desktop/daemon.py tests/desktop/test_restart.py
git commit -m "feat: daemon 增加重启端点 + 残留 .restart 清理

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 修改 `app/admin/routes.py` — 数据目录迁移接口

**Files:**
- Modify: `app/admin/routes.py`
- Modify: `tests/test_admin_logs_settings.py`

**Interfaces:**
- Consumes: `desktop.config.set_data_dir()` (Task 1), `desktop.paths.data_dir()` (Task 2)
- Produces: `POST /admin/settings/data-dir` 端点，settings 页面传递当前 `data_dir`

- [ ] **Step 1: 更新测试**

```python
# 在 tests/test_admin_logs_settings.py 末尾追加
import json
import os
import shutil
from pathlib import Path


async def test_data_dir_migration_success(tmp_path, monkeypatch):
    """正常迁移：gateway.db 被复制到新目录，config.json 写入，旧目录删除。"""
    old_dir = tmp_path / "old-data"
    old_dir.mkdir()
    new_dir = tmp_path / "new-data"

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(old_dir))
    monkeypatch.setattr(db, "DB_PATH", old_dir / "gateway.db")
    db.init_db()

    # 写入一些数据以便验证迁移
    conn = db.connect()
    conn.execute("INSERT INTO settings (key, value) VALUES ('test', 'migrated')")
    conn.commit()
    conn.close()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t"
    ) as c:
        resp = await c.post("/admin/settings/data-dir", data={
            "data_dir": str(new_dir)
        })
        assert resp.status_code == 200
        result = resp.json()
        assert result["ok"] is True
        assert str(new_dir) in result["data_dir"]

    # 验证 gateway.db 已迁移
    assert (new_dir / "gateway.db").exists()
    # 验证 config.json 已写入
    config = json.loads(
        (tmp_path / "llm-apig" / "config.json").read_text(encoding="utf-8"))
    assert config["data_dir"] == str(new_dir)
    # 验证旧目录已删除
    assert not old_dir.exists()


async def test_data_dir_migration_same_path(tmp_path, monkeypatch):
    """新旧路径相同时无操作返回成功。"""
    old_dir = tmp_path / "data"
    old_dir.mkdir()

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(old_dir))
    monkeypatch.setattr(db, "DB_PATH", old_dir / "gateway.db")
    db.init_db()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t"
    ) as c:
        resp = await c.post("/admin/settings/data-dir", data={
            "data_dir": str(old_dir)
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


async def test_data_dir_migration_invalid_path(tmp_path, monkeypatch):
    """空路径返回错误。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "gateway.db")
    db.init_db()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t"
    ) as c:
        resp = await c.post("/admin/settings/data-dir", data={
            "data_dir": ""
        })
        assert resp.status_code == 400
        assert resp.json()["ok"] is False


async def test_data_dir_migration_unwritable(tmp_path, monkeypatch):
    """目标路径不可写时返回错误。"""
    old_dir = tmp_path / "data"
    old_dir.mkdir()

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(old_dir))
    monkeypatch.setattr(db, "DB_PATH", old_dir / "gateway.db")
    db.init_db()

    # 创建一个只读目录（Windows 下用文件冒充目录）
    bad_path = tmp_path / "bad"
    bad_path.write_text("block")  # 文件存在但不是目录，无法 mkdir

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t"
    ) as c:
        resp = await c.post("/admin/settings/data-dir", data={
            "data_dir": str(bad_path / "sub")
        })
        assert resp.status_code == 400
        assert resp.json()["ok"] is False


async def test_settings_page_includes_data_dir(tmp_path, monkeypatch):
    """设置页面应显示当前数据目录路径。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "gateway.db")
    db.init_db()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t"
    ) as c:
        resp = await c.get("/admin/settings")
        assert resp.status_code == 200
        assert str(tmp_path / "data") in resp.text
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_admin_logs_settings.py -v -k "data_dir"
```
Expected: 全部 FAIL（端点不存在）

- [ ] **Step 3: 修改 `app/admin/routes.py`**

在 `settings_page` 函数中增加 `data_dir` 到模板上下文：

```python
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.db import get_setting
    from app.errors import BALANCE_PATTERNS, CAPABILITY_PATTERNS
    from desktop.paths import data_dir
    conn = connect()
    try:
        values = {k: get_setting(conn, k, "") for k in _SETTING_KEYS}
    finally:
        conn.close()
    if not values["balance_patterns"]:
        values["balance_patterns"] = "\n".join(BALANCE_PATTERNS)
    if not values["capability_patterns"]:
        values["capability_patterns"] = "\n".join(CAPABILITY_PATTERNS)
    return templates.TemplateResponse(request, "settings.html", {
        "v": values,
        "current_data_dir": str(data_dir()),
    })
```

在文件末尾添加数据目录迁移端点：

```python
@router.post("/settings/data-dir")
async def settings_data_dir(request: Request):
    import os
    import shutil
    from pathlib import Path

    from desktop.config import set_data_dir

    form = await request.form()
    new_dir = form.get("data_dir", "").strip()

    if not new_dir:
        return JSONResponse({"ok": False, "detail": "路径不能为空"}, 400)

    new_path = Path(new_dir).resolve()
    old_path = Path(os.environ.get("LLMAPIG_DATA_DIR", "") or
                    str(Path(os.environ["APPDATA"]) / "llm-apig"))

    if new_path == old_path:
        return JSONResponse({"ok": True, "data_dir": str(new_path)})

    # 校验：可创建且可写
    try:
        new_path.mkdir(parents=True, exist_ok=True)
        test_file = new_path / ".write_test"
        test_file.touch()
        test_file.unlink()
    except (OSError, PermissionError) as e:
        return JSONResponse(
            {"ok": False, "detail": f"路径不可写: {e}"}, 400)

    # 校验：磁盘空间
    db_file = old_path / "gateway.db"
    if db_file.exists():
        db_size = db_file.stat().st_size
        usage = shutil.disk_usage(new_path)
        if usage.free < db_size * 1.5:
            return JSONResponse(
                {"ok": False, "detail": "目标磁盘空间不足"}, 400)

    # 迁移
    try:
        if db_file.exists():
            shutil.copy2(db_file, new_path / "gateway.db")

        set_data_dir(str(new_path))

        shutil.rmtree(old_path, ignore_errors=True)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "detail": f"迁移失败: {e}"}, 500)

    return JSONResponse({"ok": True, "data_dir": str(new_path)})
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest tests/test_admin_logs_settings.py -v -k "data_dir"
```
Expected: 全部 PASS

- [ ] **Step 5: 确保已有测试不受影响**

```bash
python -m pytest tests/test_admin_logs_settings.py -v -k "not data_dir"
```
Expected: 原有测试全部 PASS

- [ ] **Step 6: 提交**

```bash
git add app/admin/routes.py tests/test_admin_logs_settings.py
git commit -m "feat: 新增数据目录迁移接口 POST /admin/settings/data-dir

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 修改 `app/admin/templates/settings.html` — 前端 UI

**Files:**
- Modify: `app/admin/templates/settings.html`

**Interfaces:**
- Consumes: `{{ current_data_dir }}` (Task 5), `POST /admin/settings/data-dir` (Task 5), `POST /admin/settings/restart` (Task 4)

- [ ] **Step 1: 修改模板**

将 `settings.html` 的 `{% block content %}` 内容替换为：

```jinja2
{% extends "base.html" %}
{% block nav_settings %}class="on"{% endblock %}
{% block content %}
<div class="page-head">
  <div>
    <h2>设置</h2>
    <p>失败冷却、访问鉴权、错误特征库与数据目录</p>
  </div>
</div>

<form method="post">
<div class="card form-card" style="margin-bottom:22px">
  <h3 style="margin-top:0">冷却时间</h3>
  <div class="stack" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div class="field">
      <label>余额不足 <span class="hint">秒</span></label>
      <input name="cooldown_balance" type="number" value="{{ v['cooldown_balance'] or '600' }}">
    </div>
    <div class="field">
      <label>限流 <span class="hint">秒</span></label>
      <input name="cooldown_ratelimit" type="number" value="{{ v['cooldown_ratelimit'] or '60' }}">
    </div>
    <div class="field">
      <label>鉴权失败 <span class="hint">秒</span></label>
      <input name="cooldown_auth" type="number" value="{{ v['cooldown_auth'] or '1800' }}">
    </div>
    <div class="field">
      <label>服务器错误 <span class="hint">秒</span></label>
      <input name="cooldown_server" type="number" value="{{ v['cooldown_server'] or '60' }}">
    </div>
  </div>
</div>

<div class="card form-card" style="margin-bottom:22px">
  <h3 style="margin-top:0">访问鉴权</h3>
  <div class="field">
    <label>网关访问令牌</label>
    <input name="api_key" value="{{ v['api_key'] }}" placeholder="留空则不校验">
    <span class="hint">配置后，客户端以该令牌作为 api key 访问网关</span>
  </div>
</div>

<div class="card form-card" style="margin-bottom:22px">
  <h3 style="margin-top:0">错误特征库</h3>
  <div class="field" style="margin-bottom:18px">
    <label>余额不足特征</label>
    <textarea name="balance_patterns" rows="6">{{ v["balance_patterns"] }}</textarea>
    <span class="hint">每行一条，小写匹配响应内容</span>
  </div>
  <div class="field">
    <label>能力不支持特征</label>
    <textarea name="capability_patterns" rows="6">{{ v["capability_patterns"] }}</textarea>
    <span class="hint">每行一条，小写匹配响应内容</span>
  </div>
</div>

<div class="form-actions">
  <button class="btn btn-primary" type="submit">保存设置</button>
</div>
</form>

<div class="card form-card" style="margin-bottom:22px">
  <h3 style="margin-top:0">数据目录</h3>
  <div class="field">
    <label>当前数据目录</label>
    <input id="current-dir" value="{{ current_data_dir }}" readonly
           style="background:#f7f9fa;color:var(--ink-soft);font-family:var(--mono);font-size:12px">
  </div>
  <div class="field" style="margin-top:14px">
    <label>新数据目录</label>
    <input id="new-dir" placeholder="如 D:\MyData\llm-apig"
           style="font-family:var(--mono);font-size:12px">
    <span class="hint">修改后现有数据将自动迁移到新目录，旧目录将被删除，需重启生效</span>
  </div>
  <div id="migrate-msg" style="margin:10px 0;font-size:13px;display:none"></div>
  <div class="form-actions" style="margin-top:14px">
    <button class="btn btn-primary" id="btn-migrate" type="button">修改并迁移</button>
    <button class="btn btn-primary" id="btn-restart" type="button"
            style="display:none">重启生效</button>
  </div>
</div>

<script>
(function(){
  var btnMigrate = document.getElementById('btn-migrate');
  var btnRestart = document.getElementById('btn-restart');
  var msg = document.getElementById('migrate-msg');
  var newDir = document.getElementById('new-dir');
  var currentDir = document.getElementById('current-dir');

  function showMsg(text, ok){
    msg.textContent = text;
    msg.style.display = 'block';
    msg.style.color = ok ? 'var(--ok)' : 'var(--bad)';
    msg.style.background = ok ? 'var(--ok-bg)' : 'var(--bad-bg)';
    msg.style.padding = '8px 12px';
    msg.style.borderRadius = '8px';
  }

  btnMigrate.addEventListener('click', function(){
    var val = newDir.value.trim();
    if (!val) { showMsg('请输入新路径', false); return; }
    btnMigrate.disabled = true;
    btnMigrate.textContent = '正在迁移…';
    msg.style.display = 'none';

    var fd = new FormData();
    fd.append('data_dir', val);
    fetch('/admin/settings/data-dir', {method:'POST', body:fd})
      .then(function(r){ return r.json(); })
      .then(function(d){
        btnMigrate.disabled = false;
        btnMigrate.textContent = '修改并迁移';
        if (d.ok) {
          showMsg('数据已迁移到 ' + d.data_dir + '，需要重启生效', true);
          currentDir.value = d.data_dir;
          newDir.value = '';
          btnRestart.style.display = 'inline-flex';
        } else {
          showMsg(d.detail || '迁移失败', false);
        }
      })
      .catch(function(e){
        btnMigrate.disabled = false;
        btnMigrate.textContent = '修改并迁移';
        showMsg('网络错误: ' + e.message, false);
      });
  });

  btnRestart.addEventListener('click', function(){
    btnRestart.disabled = true;
    btnRestart.textContent = '正在重启…';
    fetch('/admin/settings/restart', {method:'POST'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        showMsg(d.message || '正在重启…', true);
        // daemon 即将退出，轮询等待页面恢复
        var attempts = 0;
        function poll(){
          attempts++;
          fetch('/health')
            .then(function(r){
              if (r.ok) {
                // 壳会重定向 webview，但兜底手动刷新
                window.location.reload();
              } else if (attempts < 30) {
                setTimeout(poll, 1000);
              }
            })
            .catch(function(){
              if (attempts < 30) setTimeout(poll, 1000);
            });
        }
        setTimeout(poll, 1500);
      })
      .catch(function(e){
        // daemon 退出时 fetch 可能失败，这是预期行为
        showMsg('正在重启…', true);
        var attempts = 0;
        function poll(){
          attempts++;
          fetch('/health')
            .then(function(r){
              if (r.ok) window.location.reload();
              else if (attempts < 30) setTimeout(poll, 1000);
            })
            .catch(function(){
              if (attempts < 30) setTimeout(poll, 1000);
            });
        }
        setTimeout(poll, 1500);
      });
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 2: 验证页面渲染**

```bash
python -m pytest tests/test_admin_logs_settings.py::test_settings_page_includes_data_dir -v
```
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add app/admin/templates/settings.html
git commit -m "feat: 设置页面增加数据目录配置卡片

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 集成测试 — 端到端验证

**Files:**
- Create: `tests/desktop/test_data_dir_e2e.py`

**Interfaces:**
- Consumes: 所有前置 Task

- [ ] **Step 1: 编写端到端测试**

```python
# tests/desktop/test_data_dir_e2e.py
"""数据目录配置端到端测试：config → 迁移 → 重启。"""
import json
import os
import time
from pathlib import Path

import app.db as db
from app.main import app
from fastapi.testclient import TestClient


def setup_app(tmp_path, monkeypatch):
    """初始化测试环境：设置环境变量和 DB。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(data_dir))
    monkeypatch.setattr(db, "DB_PATH", data_dir / "gateway.db")
    db.init_db()
    return data_dir


def test_full_migration_flow(tmp_path, monkeypatch):
    """完整迁移流程：发起迁移 → 验证 DB 在新位置 → 验证 config.json。"""
    old_dir = setup_app(tmp_path, monkeypatch)

    # 写入一些设置数据
    conn = db.connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('test_key', 'test_value')")
    conn.commit()
    conn.close()

    new_dir = tmp_path / "new-data"
    client = TestClient(app)

    # Step 1: 迁移
    resp = client.post("/admin/settings/data-dir", data={
        "data_dir": str(new_dir)
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Step 2: 验证 gateway.db 在新位置
    assert (new_dir / "gateway.db").exists()

    # Step 3: 验证数据完整性
    import sqlite3
    verify_conn = sqlite3.connect(str(new_dir / "gateway.db"))
    row = verify_conn.execute(
        "SELECT value FROM settings WHERE key='test_key'").fetchone()
    assert row[0] == "test_value"
    verify_conn.close()

    # Step 4: 验证 config.json
    config = json.loads(
        (tmp_path / "llm-apig" / "config.json").read_text(encoding="utf-8"))
    assert config["data_dir"] == str(new_dir)

    # Step 5: 验证旧目录已删除
    assert not old_dir.exists()


def test_restart_flow(tmp_path, monkeypatch):
    """重启流程：写入 .restart 标记 → daemon 退出。"""
    data_dir = setup_app(tmp_path, monkeypatch)

    from desktop.daemon import attach_restart_handler

    class FakeServer:
        should_exit = False

    server = FakeServer()
    attach_restart_handler(app, server)

    client = TestClient(app)
    resp = client.post("/admin/settings/restart")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 验证 .restart 标记
    marker = tmp_path / "llm-apig" / ".restart"
    assert marker.exists()

    # 等待定时器触发
    time.sleep(0.3)
    assert server.should_exit is True


def test_config_roundtrip(tmp_path, monkeypatch):
    """config.json 读写闭环：写 → 读 → 验证。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from desktop import config

    # 初始：无配置
    assert config.get_data_dir() is None

    # 写入
    config.set_data_dir(str(tmp_path / "roundtrip"))
    assert config.get_data_dir() == str(tmp_path / "roundtrip")

    # 验证文件内容
    raw = json.loads(
        (tmp_path / "llm-apig" / "config.json").read_text(encoding="utf-8"))
    assert raw["data_dir"] == str(tmp_path / "roundtrip")


def test_data_dir_priority(tmp_path, monkeypatch):
    """环境变量 > config.json > 默认值。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))

    # 默认值
    monkeypatch.delenv("LLMAPIG_DATA_DIR", raising=False)
    from desktop.paths import data_dir
    assert data_dir() == tmp_path / "llm-apig"

    # config.json 覆盖默认值
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": str(tmp_path / "custom")}), encoding="utf-8")
    # 需要重新导入因为模块级缓存
    import importlib
    import desktop.paths
    importlib.reload(desktop.paths)
    assert desktop.paths.data_dir() == tmp_path / "custom"

    # 环境变量覆盖 config.json
    env_dir = tmp_path / "env-override"
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(env_dir))
    importlib.reload(desktop.paths)
    assert desktop.paths.data_dir() == env_dir
```

- [ ] **Step 2: 运行集成测试**

```bash
python -m pytest tests/desktop/test_data_dir_e2e.py -v
```
Expected: 全部 PASS

- [ ] **Step 3: 运行全量测试确认无回归**

```bash
python -m pytest tests/ -v
```
Expected: 全部 PASS

- [ ] **Step 4: 提交**

```bash
git add tests/desktop/test_data_dir_e2e.py
git commit -m "test: 数据目录配置端到端集成测试

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```