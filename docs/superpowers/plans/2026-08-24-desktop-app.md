# llm-apig 桌面端应用实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 llm-apig 网关打包为 Windows 桌面应用（pywebview 窗口 + 托盘 + Inno Setup 安装包），可分发给不懂技术的用户。

**Architecture:** `desktop/` 桌面壳负责单实例锁、拉起 uvicorn 线程（端口 8317 起自动重试）、pywebview 窗口、pystray 托盘、注册表自启；`app/` 网关本体仅两处小改（数据目录环境变量化、开发模式才开浏览器）。更新检查逻辑放 `app/update_check.py`（服务端能力，管理页横幅与托盘共用），构建脚本 `desktop/build.py` 一键完成 测试→PyInstaller→Inno Setup→冒烟。

**Tech Stack:** Python 3.13 + FastAPI/uvicorn（现有）、pywebview 5.x（WebView2）、pystray + Pillow、PyInstaller 6、Inno Setup 6。

**与 spec 的偏差（已确认合理）:** spec 中 `desktop/updater.py` 移为 `app/update_check.py` —— 更新横幅需要由 FastAPI 端点 `/admin/api/update` 提供给网页，逻辑放 app 层避免 app→desktop 反向依赖；托盘「检查更新」通过 localhost HTTP 调同一端点。

## Global Constraints

- 仅 Windows；现有 `tests/` 全部必须继续通过（每任务验证）
- 端口：8317 起被占用则 +1 重试，最多到 8321；实际端口写入 `%APPDATA%\llm-apig\runtime.json`
- 数据目录：环境变量 `LLMAPIG_DATA_DIR` 优先，桌面模式指向 `%APPDATA%\llm-apig\`；开发模式缺省仍是项目 `data/`
- 单实例：命名互斥锁 `llm-apig-singleton`；窗口标题固定 `llm-apig`（FindWindow 用）
- 自启：仅 HKCU `Software\Microsoft\Windows\CurrentVersion\Run`，键名 `llm-apig`，免 UAC；装到 `{localappdata}\Programs\llm-apig`
- 版本号唯一来源：`pyproject.toml` 的 `[project].version`，构建时打包进 exe
- 代码风格与现库一致：极简中文注释、模块级 `_transport = None` 测试注入点约定
- 提交信息：`feat:/fix:/docs:/test:` + 中文描述

---

### Task 1: 提交现有未提交改动，建立干净基线

**Files:**
- Modify: `.gitignore`
- Commit: `app/admin/routes.py` 及 10 个模板（工作区已有改动，非本计划产生）

**Interfaces:**
- Consumes: 无
- Produces: 干净工作树，后续任务每步提交不混入无关改动

- [ ] **Step 1: 确认现有测试通过**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（若有失败，停下报告用户，不要提交）

- [ ] **Step 2: .gitignore 增加构建产物与工具目录**

`.gitignore` 追加（文件当前内容：`.venv/`、`__pycache__/`、`data/`、`*.pyc`、`.idea/`、`gateway.db`）：

```
dist/
build/
.playwright-mcp/
```

- [ ] **Step 3: 提交**

```bash
git add .gitignore app/
git commit -m "style: 管理界面视觉打磨（设计令牌统一）"
```

---

### Task 2: app 数据目录环境变量化

**Files:**
- Modify: `app/db.py`
- Modify: `app/main.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: 现有 `db.DB_PATH` 模块属性（测试 monkeypatch 注入点，保持兼容）
- Produces: `db._db_path() -> Path`（DB_PATH 非 None 优先 → `LLMAPIG_DATA_DIR` 环境变量 → 项目 `data/`）；`app/main.py:main()` 仅在无 `LLMAPIG_DATA_DIR` 时打开浏览器

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_db.py`：

```python
def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", None)
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path))
    db.init_db()
    assert (tmp_path / "gateway.db").exists()


def test_db_path_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "env"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "explicit.db")
    db.init_db()
    assert (tmp_path / "explicit.db").exists()
    assert not (tmp_path / "env").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: 两个新测试 FAIL（当前 `connect()` 读模块级 `DB_PATH` 常量，None 会报错）

- [ ] **Step 3: 修改 `app/db.py`**

顶部 import 增加 `import os`；把第 4 行 `DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gateway.db"` 替换为：

```python
DB_PATH: Path | None = None  # 测试注入点；None 时按 _db_path() 解析


def _db_path() -> Path:
    if DB_PATH is not None:
        return DB_PATH
    env = os.environ.get("LLMAPIG_DATA_DIR")
    if env:
        return Path(env) / "gateway.db"
    return Path(__file__).resolve().parent.parent / "data" / "gateway.db"
```

`connect()` 中 `DB_PATH.parent.mkdir(...)` 与 `sqlite3.connect(DB_PATH)` 改用 `_db_path()`：

```python
def connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

- [ ] **Step 4: 修改 `app/main.py`**

`main()` 改为（`import os` 加到文件头）：

```python
def main():
    init_db()
    if not os.environ.get("LLMAPIG_DATA_DIR"):  # 桌面模式由壳开窗口
        threading.Timer(1.0,
                        lambda: webbrowser.open("http://127.0.0.1:8317/admin")).start()
    uvicorn.run(app, host="127.0.0.1", port=8317)
```

- [ ] **Step 5: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（现有 11 个测试文件都 monkeypatch `DB_PATH`，不受影响）

- [ ] **Step 6: 提交**

```bash
git add app/db.py app/main.py tests/test_db.py
git commit -m "feat: 数据目录支持 LLMAPIG_DATA_DIR 环境变量"
```

---

### Task 3: app/update_check.py 版本比较与更新信息

**Files:**
- Create: `app/update_check.py`
- Test: `tests/test_update_check.py`

**Interfaces:**
- Consumes: `app.db.connect/get_setting`（读 `update_url` 设置，空 = 禁用检查）
- Produces:
  - `version_gt(a: str, b: str) -> bool` 语义化版本比较（`v` 前缀容忍、非数字段按 0）
  - `current_version() -> str`（frozen 读 `sys._MEIPASS/pyproject.toml`，开发读项目根）
  - `async get_update_info() -> dict`：`{"update": None}` 或 `{"update": {"latest", "notes", "url"}}`，结果缓存 1 小时，网络/解析失败静默返回无更新
  - 模块级 `_transport = None` 注入点（沿用 executor 的约定）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_update_check.py`：

```python
import httpx

import app.db as db
import app.update_check as uc


def test_version_gt():
    assert uc.version_gt("1.0.1", "1.0.0")
    assert uc.version_gt("1.10.0", "1.9.0")
    assert uc.version_gt("v1.0.1", "1.0.0")
    assert not uc.version_gt("1.0.0", "1.0.0")
    assert not uc.version_gt("1.0", "1.0.0")
    assert not uc.version_gt("垃圾", "1.0.0")


def test_current_version_reads_pyproject():
    import tomllib
    from pathlib import Path
    with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert uc.current_version() == expected


async def test_update_info_no_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    uc._cache.clear()
    assert await uc.get_update_info() == {"update": None}


async def test_update_info_newer_version(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()
    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(lambda req: httpx.Response(200, json={
        "version": "99.0.0", "notes": "大更新", "url": "http://rel"})))
    uc._cache.clear()
    info = await uc.get_update_info()
    assert info["update"]["latest"] == "99.0.0"
    assert info["update"]["notes"] == "大更新"
    assert info["update"]["url"] == "http://rel"


async def test_update_info_same_version_means_none(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()
    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(lambda req: httpx.Response(200, json={
        "version": uc.current_version()})))
    uc._cache.clear()
    assert await uc.get_update_info() == {"update": None}


async def test_update_info_network_error_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(boom))
    uc._cache.clear()
    assert await uc.get_update_info() == {"update": None}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_update_check.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.update_check'`

- [ ] **Step 3: 实现 `app/update_check.py`**

```python
import sys
import time
import tomllib
from pathlib import Path

import httpx

_transport = None  # 测试注入点
CACHE_SECONDS = 3600
_cache: dict = {"at": 0.0, "result": None}


def version_gt(a: str, b: str) -> bool:
    def key(v: str) -> tuple:
        parts = []
        for p in str(v).strip().lstrip("vV").split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)
    return key(a) > key(b)


def current_version() -> str:
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "pyproject.toml"
    else:
        path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)["project"]["version"]


async def get_update_info() -> dict:
    now = time.time()
    if _cache["result"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["result"]
    from app.db import connect, get_setting
    conn = connect()
    try:
        url = get_setting(conn, "update_url", "")
    finally:
        conn.close()
    result: dict = {"update": None}
    if url:
        try:
            async with httpx.AsyncClient(transport=_transport, timeout=10) as client:
                resp = await client.get(url)
            data = resp.json()
            latest = str(data.get("version", ""))
            if latest and version_gt(latest, current_version()):
                result = {"update": {"latest": latest,
                                     "notes": str(data.get("notes", "")),
                                     "url": str(data.get("url", ""))}}
        except (httpx.HTTPError, ValueError):
            pass  # 检查失败静默，不影响使用
    _cache["result"] = result
    _cache["at"] = now
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_update_check.py -v`
Expected: 6 个 PASS

- [ ] **Step 5: 提交**

```bash
git add app/update_check.py tests/test_update_check.py
git commit -m "feat: 更新检查逻辑（版本比较 + jihulab version.json）"
```

---

### Task 4: 更新端点、设置项、界面横幅与动态端口显示

**Files:**
- Modify: `app/admin/routes.py`（新增端点 + `_SETTING_KEYS` 加 `update_url`）
- Modify: `app/admin/templates/settings.html`（新增「更新检查」卡片）
- Modify: `app/admin/templates/base.html`（横幅 + 侧栏端口动态化）
- Test: `tests/test_admin_logs_settings.py`

**Interfaces:**
- Consumes: `app.update_check.get_update_info`
- Produces: `GET /admin/api/update` → `{"update": None}` 或 `{"update": {"latest": str, "notes": str, "url": str}}`；设置项 key `update_url`（空 = 禁用）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_admin_logs_settings.py`（该文件已有 `import httpx`、`import app.db as db`、`from app.main import app` 及 monkeypatch DB_PATH 的 fixture 写法，新测试沿用同样开头）：

```python
import app.update_check as uc


async def test_update_api_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    uc._cache.clear()
    conn = db.connect()
    db.set_setting(conn, "update_url", "")
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/api/update")
    assert r.status_code == 200
    assert r.json() == {"update": None}


async def test_settings_page_shows_update_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://jihulab/x/version.json")
    conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/settings")
    assert r.status_code == 200
    assert "http://jihulab/x/version.json" in r.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_admin_logs_settings.py -v`
Expected: 新增两个 FAIL（404 / 页面无该字段）

- [ ] **Step 3: `app/admin/routes.py`**

文件头 import 区加 `from app.update_check import get_update_info`；`_SETTING_KEYS` 列表追加 `"update_url"`；文件末尾追加：

```python
@router.get("/api/update")
async def update_api():
    return await get_update_info()
```

- [ ] **Step 4: `settings.html` 加「更新检查」卡片**

在「访问鉴权」卡片（第 34-41 行）之后插入：

```html
<div class="card form-card" style="margin-bottom:22px">
  <h3 style="margin-top:0">更新检查</h3>
  <div class="field">
    <label>版本信息地址</label>
    <input name="update_url" value="{{ v['update_url'] }}"
           placeholder="留空则不检查更新">
    <span class="hint">填 version.json 的直链（如 GitLab raw 地址），启动时后台检查，有新版会在界面顶部提示</span>
  </div>
</div>
```

- [ ] **Step 5: `base.html` 横幅 + 动态端口**

`<main>` 标签（第 215 行）后插入横幅与脚本；侧栏 `<b>127.0.0.1:8317</b>`（第 212 行）改为动态端口：

```html
<main>
<div id="upd-banner" hidden
     style="display:flex;align-items:center;gap:10px;margin:0 0 18px;padding:10px 16px;
            background:var(--warn-bg);color:var(--warn);border-radius:var(--radius);
            font-size:13px">
  <span id="upd-text"></span>
  <a id="upd-link" href="#" style="flex:none">查看下载</a>
</div>
```

```html
<script>
(function(){
  fetch('/admin/api/update').then(function(r){return r.json()}).then(function(d){
    if(!d.update) return;
    document.getElementById('upd-text').textContent =
      '新版本 ' + d.update.latest + ' 可用' + (d.update.notes ? '：' + d.update.notes : '');
    var a = document.getElementById('upd-link');
    if(d.update.url){ a.href = d.update.url; a.target = '_blank'; } else { a.remove(); }
    document.getElementById('upd-banner').hidden = false;
  }).catch(function(){});
})();
</script>
```

脚本块放在 `</body>` 前。侧栏改为：

```html
<b>127.0.0.1:{{ request.url.port or 80 }}</b><br>本机网关 · <a href="/health" style="color:#7fb0dd">健康检查</a>
```

- [ ] **Step 6: 跑测试**

Run: `uv run pytest tests/test_admin_logs_settings.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 全量测试 + 提交**

Run: `uv run pytest tests/ -q` → 全部 PASS

```bash
git add app/admin/routes.py app/admin/templates/settings.html app/admin/templates/base.html tests/test_admin_logs_settings.py
git commit -m "feat: 管理页更新横幅与更新地址设置"
```

---

### Task 5: desktop/ 包骨架 — paths.py 与 singleinstance.py

**Files:**
- Create: `desktop/__init__.py`（空文件）
- Create: `desktop/paths.py`
- Create: `desktop/singleinstance.py`
- Create: `tests/desktop/__init__.py`（空文件）
- Test: `tests/desktop/test_paths.py`、`tests/desktop/test_singleinstance.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `paths.data_dir() -> Path`（`LLMAPIG_DATA_DIR` → `%APPDATA%\llm-apig`）、`paths.log_dir() -> Path`（data_dir/logs）、`paths.setup_logging() -> None`（FileHandler 追加写 app.log）
  - `singleinstance.SingleInstance(name).acquire() -> bool / release()`、`singleinstance.activate_existing_window(title) -> bool`

- [ ] **Step 1: 写失败测试**

`tests/desktop/test_paths.py`：

```python
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
```

`tests/desktop/test_singleinstance.py`：

```python
import os

from desktop import singleinstance


def _uniq():
    return f"llm-apig-test-{os.getpid()}-{id(object())}"


def test_second_acquire_fails():
    name = _uniq()
    a = singleinstance.SingleInstance(name)
    b = singleinstance.SingleInstance(name)
    assert a.acquire()
    assert not b.acquire()
    a.release()
    c = singleinstance.SingleInstance(name)
    assert c.acquire()
    c.release()


def test_activate_missing_window_returns_false():
    assert singleinstance.activate_existing_window("绝不存在的窗口标题-xyz") is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/desktop -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'desktop'`

- [ ] **Step 3: 实现 `desktop/paths.py`**

```python
import logging
import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("LLMAPIG_DATA_DIR")
    if env:
        return Path(env)
    return Path(os.environ["APPDATA"]) / "llm-apig"


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

`desktop/singleinstance.py`：

```python
import ctypes

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "llm-apig-singleton"
WINDOW_TITLE = "llm-apig"


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = None

    def acquire(self) -> bool:
        self._handle = self._k32.CreateMutexW(None, False, self.name)
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def activate_existing_window(title: str = WINDOW_TITLE) -> bool:
    u32 = ctypes.WinDLL("user32")
    hwnd = u32.FindWindowW(None, title)
    if not hwnd:
        return False
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/desktop -v`
Expected: 4 个 PASS

- [ ] **Step 5: 提交**

```bash
git add desktop/ tests/desktop/
git commit -m "feat: 桌面壳路径解析与单实例锁"
```

---

### Task 6: desktop/server.py — uvicorn 线程封装

**Files:**
- Create: `desktop/server.py`
- Test: `tests/desktop/test_server.py`

**Interfaces:**
- Consumes: `app.main.app`（FastAPI 实例，含 `/health`）
- Produces: `GatewayServer(app, host="127.0.0.1", ports=(8317,8318,8319,8320,8321))`；`.start() -> int`（返回实际端口，全部被占/超时抛 `ServerError`）；`.stop() -> None`（`should_exit` + join 5s）

- [ ] **Step 1: 写失败测试**

`tests/desktop/test_server.py`：

```python
import socket
import time

import httpx
import pytest

import app.db as db
from app.main import app
from desktop.server import GatewayServer, ServerError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _init(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


def test_start_health_stop(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    port = _free_port()
    srv = GatewayServer(app, ports=(port,))
    assert srv.start() == port
    r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
    assert r.json() == {"status": "ok"}
    srv.stop()
    time.sleep(0.5)
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)


def test_port_fallback(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    busy = _free_port()
    hold = socket.socket()
    hold.bind(("127.0.0.1", busy))
    hold.listen(1)
    try:
        free = _free_port()
        srv = GatewayServer(app, ports=(busy, free))
        assert srv.start() == free
        srv.stop()
    finally:
        hold.close()


def test_all_ports_busy_raises(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    busy = _free_port()
    hold = socket.socket()
    hold.bind(("127.0.0.1", busy))
    hold.listen(1)
    try:
        with pytest.raises(ServerError):
            GatewayServer(app, ports=(busy,)).start()
    finally:
        hold.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/desktop/test_server.py -v`
Expected: FAIL，`ModuleNotFoundError`（desktop.server 不存在）

- [ ] **Step 3: 实现 `desktop/server.py`**

```python
import socket
import threading
import time

import httpx
import uvicorn

HEALTH_TIMEOUT = 10.0


class ServerError(Exception):
    pass


class GatewayServer:
    def __init__(self, app, host: str = "127.0.0.1",
                 ports: tuple = (8317, 8318, 8319, 8320, 8321)):
        self.app = app
        self.host = host
        self.ports = ports
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        for port in self.ports:
            with socket.socket() as s:
                try:
                    s.bind((self.host, port))
                except OSError:
                    continue
            return self._run(port)
        raise ServerError(f"端口 {self.ports[0]}-{self.ports[-1]} 均被占用")

    def _run(self, port: int) -> int:
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host=self.host, port=port, log_config=None))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + HEALTH_TIMEOUT
        while time.time() < deadline:
            if not self._thread.is_alive():
                raise ServerError("服务线程异常退出")
            try:
                r = httpx.get(f"http://{self.host}:{port}/health", timeout=1)
                if r.status_code == 200:
                    return port
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise ServerError("健康检查超时")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/desktop/test_server.py -v`
Expected: 3 个 PASS

- [ ] **Step 5: 提交**

```bash
git add desktop/server.py tests/desktop/test_server.py
git commit -m "feat: 网关线程封装（端口探测 + 健康等待 + 优雅停止）"
```

---

### Task 7: desktop/autostart.py — HKCU Run 键读写

**Files:**
- Create: `desktop/autostart.py`
- Test: `tests/desktop/test_autostart.py`

**Interfaces:**
- Consumes: 无（`sys.executable` 作为自启命令）
- Produces: `is_supported() -> bool`（仅 frozen 后为 True）；`is_enabled(key_path) -> bool`；`enable(key_path)`；`disable(key_path)`；默认 `key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"`，键名 `llm-apig`（参数化供测试注入子键）

- [ ] **Step 1: 写失败测试**

`tests/desktop/test_autostart.py`：

```python
import sys
import winreg

from desktop import autostart

TEST_KEY = r"Software\llm-apig-test"


def _cleanup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software") as parent:
            winreg.DeleteKey(parent, "llm-apig-test")
    except OSError:
        pass


def test_roundtrip():
    _cleanup()
    try:
        assert not autostart.is_enabled(TEST_KEY)
        autostart.enable(TEST_KEY)
        assert autostart.is_enabled(TEST_KEY)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as k:
            value, _ = winreg.QueryValueEx(k, "llm-apig")
        assert value == sys.executable
        autostart.disable(TEST_KEY)
        assert not autostart.is_enabled(TEST_KEY)
    finally:
        _cleanup()


def test_disable_when_absent_is_noop():
    _cleanup()
    autostart.disable(TEST_KEY)  # 不抛错
    assert not autostart.is_enabled(TEST_KEY)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/desktop/test_autostart.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 `desktop/autostart.py`**

```python
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "llm-apig"


def is_supported() -> bool:
    return getattr(sys, "frozen", False)  # 开发模式无固定 exe 路径


def is_enabled(key_path: str = RUN_KEY) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            value, _ = winreg.QueryValueEx(k, APP_NAME)
        return value == sys.executable
    except OSError:
        return False


def enable(key_path: str = RUN_KEY) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, sys.executable)


def disable(key_path: str = RUN_KEY) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/desktop/test_autostart.py -v`
Expected: 2 个 PASS

- [ ] **Step 5: 提交**

```bash
git add desktop/autostart.py tests/desktop/test_autostart.py
git commit -m "feat: 开机自启注册表读写"
```

---

### Task 8: 托盘、桌面壳入口与依赖

**Files:**
- Create: `desktop/tray.py`
- Create: `desktop/main.py`
- Modify: `pyproject.toml`（dependencies 加 `pywebview>=5`、`pystray>=0.19`、`Pillow>=10`）
- Test: `tests/desktop/test_tray.py`（仅图标生成；GUI 部分人工验收）

**Interfaces:**
- Consumes: Task 5/6/7 全部产物；`app.main.app`；`app.update_check.current_version`
- Produces: `tray.make_icon_image() -> PIL.Image.Image`（64×64 RGBA）；`tray.create_tray(on_open, on_check_update, on_quit) -> pystray.Icon`（菜单：打开主界面(默认/双击) / 开机自启(勾选态) / 检查更新 / 退出）；`desktop/main.py` 可执行入口（`uv run python -m desktop.main`）

- [ ] **Step 1: 加依赖并同步**

`pyproject.toml` 的 `dependencies` 追加三行：

```toml
    "pywebview>=5",
    "pystray>=0.19",
    "Pillow>=10",
```

Run: `uv sync`
Expected: 安装成功，无冲突

- [ ] **Step 2: 写失败测试**

`tests/desktop/test_tray.py`：

```python
from desktop.tray import make_icon_image


def test_make_icon_image():
    img = make_icon_image()
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
```

- [ ] **Step 3: 实现 `desktop/tray.py`**

```python
import ctypes

import pystray
from PIL import Image, ImageDraw

from desktop import autostart


def make_icon_image() -> Image.Image:
    """与网页 favicon 同款：深蓝圆角块 + 三条横线"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(15, 43, 70, 255))
    d.rectangle([14, 22, 50, 27], fill=(255, 255, 255, 235))
    d.rectangle([14, 31, 38, 36], fill=(127, 176, 221, 235))
    d.rectangle([14, 40, 44, 45], fill=(255, 255, 255, 235))
    return img


def create_tray(on_open, on_check_update, on_quit) -> pystray.Icon:
    def _autostart_toggle(icon, item):
        if not autostart.is_supported():
            ctypes.windll.user32.MessageBoxW(
                0, "开发模式下不可设置开机自启", "llm-apig", 0x40)
            return
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()

    menu = pystray.Menu(
        pystray.MenuItem("打开主界面", lambda icon, item: on_open(),
                         default=True),
        pystray.MenuItem("开机自启", _autostart_toggle,
                         checked=lambda item: autostart.is_enabled()),
        pystray.MenuItem("检查更新", lambda icon, item: on_check_update()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda icon, item: on_quit()),
    )
    return pystray.Icon("llm-apig", make_icon_image(), "llm-apig API 网关",
                        menu)
```

- [ ] **Step 4: 实现 `desktop/main.py`**

```python
import ctypes
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 开发模式兜底

from desktop import autostart, paths, server as dserver, singleinstance

log = logging.getLogger("llm-apig")


def show_error(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, text, "llm-apig", 0x10)


def main() -> None:
    lock = singleinstance.SingleInstance()
    if not lock.acquire():
        singleinstance.activate_existing_window()
        return
    try:
        data = paths.data_dir()
        data.mkdir(parents=True, exist_ok=True)
        os.environ["LLMAPIG_DATA_DIR"] = str(data)
        paths.setup_logging()
        from app.db import init_db
        from app.main import app
        from app.update_check import current_version
        init_db()
        gw = dserver.GatewayServer(app)
        port = gw.start()
        (data / "runtime.json").write_text(
            json.dumps({"port": port, "version": current_version()}),
            encoding="utf-8")
        log.info("网关就绪 端口=%s 版本=%s", port, current_version())
    except Exception as e:
        log.exception("启动失败")
        show_error(f"启动失败：{e}\n\n日志：{paths.log_dir() / 'app.log'}")
        lock.release()
        return

    import httpx
    import webview

    window = webview.create_window(
        "llm-apig", f"http://127.0.0.1:{port}/admin", width=1200, height=800)

    def on_closing():
        window.hide()  # 缩到托盘，服务继续
        return False

    window.events.closing += on_closing

    def on_open():
        window.show()

    def on_check_update():
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/admin/api/update",
                          timeout=15)
            info = r.json().get("update")
        except httpx.HTTPError:
            info = None
            ctypes.windll.user32.MessageBoxW(
                0, "检查更新失败，请稍后再试", "llm-apig", 0x30)
            return
        if info:
            ctypes.windll.user32.MessageBoxW(
                0, f"新版本 {info['latest']} 可用。\n{info['notes']}\n\n"
                   f"请到发布页下载：{info['url']}", "llm-apig 更新", 0x40)
        else:
            ctypes.windll.user32.MessageBoxW(
                0, "已是最新版本", "llm-apig", 0x40)

    def on_quit():
        window.destroy()

    icon = None
    try:
        from desktop import tray
        icon = tray.create_tray(on_open, on_check_update, on_quit)
        icon.run_detached()
    except Exception:
        log.exception("托盘初始化失败（不影响使用）")

    webview.start()  # 阻塞直到窗口销毁（退出）

    if icon:
        icon.stop()
    gw.stop()
    lock.release()
    log.info("已退出")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 跑测试（含全量）**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 6: 人工验收（GUI，无法自动化）**

Run: `uv run python -m desktop.main`

逐项核对：
1. 窗口打开显示管理界面（侧栏端口与实际一致）
2. 关闭按钮 → 窗口隐藏、托盘图标存在、`curl http://127.0.0.1:8317/health` 仍通
3. 双击托盘 → 窗口恢复
4. 托盘「检查更新」→ 弹「已是最新版本」（update_url 为空时）
5. 托盘「开机自启」→ 弹「开发模式下不可设置」
6. 再次运行 `uv run python -m desktop.main` → 已有窗口前置，新进程退出
7. 托盘「退出」→ 进程结束，端口释放
8. `%APPDATA%\llm-apig\` 下出现 gateway.db、runtime.json、logs\app.log

- [ ] **Step 7: 提交**

```bash
git add desktop/main.py desktop/tray.py tests/desktop/test_tray.py pyproject.toml uv.lock
git commit -m "feat: 桌面壳入口（窗口/托盘/单实例/优雅退出）"
```

---

### Task 9: 构建脚本与安装包

**Files:**
- Create: `desktop/build.py`
- Create: `llm-apig.iss`
- Create: `version.json`
- Modify: `pyproject.toml`（dev 组加 `pyinstaller>=6`）

**Interfaces:**
- Consumes: Task 8 产物（`desktop.main`、`tray.make_icon_image`）；`app.update_check.current_version`
- Produces: `dist/llm-apig/`（PyInstaller onedir）、`dist/llm-apig-setup-<版本>.exe`、更新后的 `version.json`

- [ ] **Step 1: dev 依赖加 pyinstaller**

`pyproject.toml` 的 dev 组追加 `"pyinstaller>=6"`，然后 Run: `uv sync`（成功安装）

- [ ] **Step 2: 创建 `version.json`（仓库根）**

```json
{
  "version": "0.1.0",
  "notes": "",
  "url": ""
}
```

- [ ] **Step 3: 创建 `llm-apig.iss`**

```ini
; llm-apig 安装包脚本 —— 由 desktop/build.py 调用（ISCC /DMyAppVersion=x.y.z）
; 手动编译：先用 /D 传版本号，见 desktop/build.py

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

#define MyAppName "llm-apig"
#define MyAppExeName "llm-apig.exe"

[Setup]
AppId={{8C1F2B7A-6E5D-4C9A-9B3E-4D2F1A0C5E77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=llm-apig
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=llm-apig-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName} API 网关

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
Source: "dist\llm-apig\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[UninstallDelete]
; 清理自启注册项（若用户开启过）
Type: none

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Run', 'llm-apig');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('是否保留用户数据（渠道配置、日志）？' + #13#10 +
              '选择「是」保留，以后重装可继续使用；选择「否」彻底删除。',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      DelTree(ExpandConstant('{userappdata}') + '\llm-apig', True, True, True);
    end;
  end;
end;
```

- [ ] **Step 4: 实现 `desktop/build.py`**

```python
"""一键构建：pytest → PyInstaller → Inno Setup → version.json → 冒烟"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

ISCC_CANDIDATES = [
    os.environ.get("ISCC"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def run(cmd: list, **kw) -> None:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT, **kw)


def current_version() -> str:
    sys.path.insert(0, str(ROOT))
    from app.update_check import current_version as cv
    return cv()


def pyinstaller(version: str) -> None:
    ico = ROOT / "build" / "llm-apig.ico"
    ico.parent.mkdir(exist_ok=True)
    from desktop.tray import make_icon_image
    make_icon_image().save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    run([
        "uv", "run", "pyinstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", "llm-apig",
        "--icon", ico,
        "--paths", ".",
        "--add-data", "app/admin/templates;app/admin/templates",
        "--add-data", "pyproject.toml;.",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.platforms.winforms",
        "desktop/main.py",
    ])
    print(f"PyInstaller 完成: {DIST / 'llm-apig' / 'llm-apig.exe'}")


def update_version_json(version: str) -> None:
    path = ROOT / "version.json"
    old: dict = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data = {"version": version,
            "notes": old.get("notes", "") if old.get("version") == version else "",
            "url": old.get("url", "") if old.get("version") == version else ""}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"version.json 已更新: {version}（发布前请填写 notes/url）")


def inno(version: str) -> bool:
    iscc = next((p for p in ISCC_CANDIDATES if p and Path(p).exists()), None)
    if not iscc:
        print("⚠ 未找到 Inno Setup（ISCC.exe），跳过安装包。")
        print("  安装：winget install -e --id JRSoftware.InnoSetup 后重跑")
        return False
    run([iscc, f"/DMyAppVersion={version}", "llm-apig.iss"])
    print(f"安装包完成: {DIST / f'llm-apig-setup-{version}.exe'}")
    return True


def smoke() -> None:
    """启动打包产物 → 等 runtime.json → 健康检查 → 结束进程"""
    exe = DIST / "llm-apig" / "llm-apig.exe"
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "LLMAPIG_DATA_DIR": td}
        proc = subprocess.Popen([str(exe)], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        rj = Path(td) / "runtime.json"
        deadline = time.time() + 30
        port = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise SystemExit("冒烟失败：进程提前退出（是否已有实例在运行？先退出再构建）")
            if rj.exists():
                port = json.loads(rj.read_text(encoding="utf-8"))["port"]
                break
            time.sleep(0.5)
        if port is None:
            proc.kill()
            raise SystemExit("冒烟失败：30s 内未写出 runtime.json")
        import httpx
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r.json() == {"status": "ok"}, "冒烟失败：健康检查未通过"
        proc.terminate()
        proc.wait(timeout=10)
    print(f"冒烟通过（端口 {port}）")


def main() -> None:
    version = current_version()
    print(f"=== 构建 llm-apig v{version} ===")
    run(["uv", "run", "pytest", "tests/", "-q"])
    pyinstaller(version)
    update_version_json(version)
    inno(version)
    smoke()
    print("=== 构建全部完成 ===")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 执行构建（本机无 Inno Setup，安装包步骤会跳过）**

Run: `uv run python desktop/build.py`
Expected: 依次输出 pytest 通过 → PyInstaller 完成 → version.json 更新 → 「未找到 Inno Setup，跳过」→ 冒烟通过。若 PyInstaller 阶段报缺模块，按报错追加 `--hidden-import` 后重跑。

- [ ] **Step 6: 安装 Inno Setup 后完整构建（用户机器一次性操作）**

Run: `winget install -e --id JRSoftware.InnoSetup`
然后重跑：`uv run python desktop/build.py`
Expected: `dist/llm-apig-setup-0.1.0.exe` 生成

- [ ] **Step 7: 人工验收安装包**

双击 `dist/llm-apig-setup-0.1.0.exe`：
1. 安装向导中文、无 UAC 弹窗、装到 `%LOCALAPPDATA%\Programs\llm-apig`
2. 桌面/开始菜单快捷方式出现，双击打开窗口
3. 托盘「开机自启」可勾选，勾选后 `reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v llm-apig` 有值，取消勾选后值消失
4. 卸载：询问是否保留用户数据；选否后 `%APPDATA%\llm-apig` 被删；自启注册项被清理

- [ ] **Step 8: 提交**

```bash
git add desktop/build.py llm-apig.iss version.json pyproject.toml uv.lock .gitignore
git commit -m "build: 一键构建（PyInstaller + Inno Setup + 冒烟自检）"
```

---

### Task 10: README 分发文档与全量回归

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`（确认 `dist/`、`build/` 已在 Task 1 加入）

**Interfaces:**
- Consumes: Task 9 构建流程
- Produces: 面向分发者的发布文档；面向最终用户的安装说明

- [ ] **Step 1: README 追加分发章节**

在现有 README 末尾追加：

```markdown
## 桌面版构建与分发（Windows）

### 构建

1. 安装依赖与 Inno Setup（一次性）：
   `winget install -e --id JRSoftware.InnoSetup`
2. 一键构建：`uv run python desktop/build.py`
   （自动执行：测试 → PyInstaller → 安装包 → 冒烟自检，产物在 `dist/`）
3. 发布前编辑 `version.json` 填写 `notes` 与 `url`（发布页地址），
   把 setup.exe 上传到 GitLab Release，并提交 version.json

### 最终用户安装说明（随安装包附带）

1. 双击 `llm-apig-setup-x.x.x.exe` 安装
2. 首次运行若出现蓝色「Windows 已保护你的电脑」提示：
   点「更多信息」→「仍要运行」（无签名软件的正常提示）
3. 关闭窗口后程序驻留系统托盘，网关继续服务；托盘右键可退出
4. 客户端配置同上（地址 `http://127.0.0.1:8317`，若端口被占程序自动换用
   8318-8321，实际端口见管理界面左下角）

### 更新检查

设置页「版本信息地址」填 version.json 直链（如 GitLab raw 地址）后，
程序每小时后台检查一次，有新版在界面顶部横幅提示。
```

- [ ] **Step 2: 全量回归**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: 桌面版构建与分发说明"
```

---

## 验收标准（整体）

- `uv run pytest tests/ -q` 全绿（现有 + 新增 desktop/update_check 测试）
- `uv run python desktop/build.py` 一条命令产出可安装 setup.exe
- 安装后：双击图标出窗口、关窗驻留托盘、服务不中断、自启可开关、单实例、卸载可选保留数据
- 现有开发流程（`start.bat`、`uv run pytest`）不受影响
