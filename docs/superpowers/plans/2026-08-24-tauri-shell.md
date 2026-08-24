# Tauri 壳迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 llm-apig 的 pywebview 桌面壳换成 Tauri 2 (Rust) 壳 + PyInstaller daemon sidecar，并内置 Tauri updater 自动更新。

**Architecture:** Rust 壳（src-tauri/）负责窗口/托盘/单实例/自启/更新，spawn 独立的 `llm-apig-daemon.exe`（PyInstaller 打包的 FastAPI 网关），用 KILL_ON_JOB_CLOSE Job Object 防孤儿，POST /shutdown + token 优雅停机。admin 前端零框架改动，仅横幅 JS 改为 invoke Tauri updater。

**Tech Stack:** Python 3.13 + FastAPI/uvicorn（现有）、PyInstaller 6（daemon onefile）、Rust + Tauri 2（tauri-plugin-updater/single-instance/autostart/dialog）、NSIS（Tauri bundler）。

**Spec:** `docs/superpowers/specs/2026-08-24-tauri-shell-design.md`

## Global Constraints

- 仅 Windows；用户机器零 Python 依赖（daemon 由 PyInstaller 打包）
- 数据目录环境变量名：`LLMAPIG_DATA_DIR`（壳注入，daemon 缺失即退出码 2）
- 端口环境变量：`LLMAPIG_PORT`，缺省 8317，壳侧被占 +1 重试
- runtime.json 字段：`{"port", "version", "token", "pid"}`，先写文件再起 uvicorn
- 安装包：Tauri bundler NSIS，installMode currentUser（免 UAC，`%LOCALAPPDATA%\Programs\llm-apig`）
- 托盘菜单四项（严格对等）：打开主界面 / 开机自启(勾选) / 检查更新 / 退出
- 签名私钥走环境变量 `TAURI_PRIVATE_KEY`（tauri-cli 标准名），私钥文件不入库
- 所有面向用户的文案用中文（与现有一致）
- 无 npm 前端工程：splash 用静态 html，admin 页通过 `window.__TAURI__.core.invoke` 调壳命令（withGlobalTauri: true）
- 旧 pywebview 壳（desktop/main.py、tray.py、singleinstance.py、autostart.py、server.py）与 version.json 更新链在 Task 7 统一删除；此前的 Task 保持旧壳可运行

---

### Task 1: Python daemon 入口（desktop/daemon.py）

**Files:**
- Create: `desktop/daemon.py`
- Test: `tests/desktop/test_daemon.py`

**Interfaces:**
- Consumes: `app.main.app`（FastAPI 实例）、`app.db.init_db`、`desktop.paths.setup_logging/data_dir`、`app.update_check.current_version`
- Produces:
  - `pick_port(env: str | None) -> int`
  - `attach_shutdown(app: FastAPI, token: str, server: uvicorn.Server) -> None`（注册 POST /shutdown）
  - `runtime_payload(port: int, token: str) -> dict`（写 runtime.json 的内容）
  - 进程入口 `python -m desktop.daemon`，env `LLMAPIG_DATA_DIR`/`LLMAPIG_PORT`

- [ ] **Step 1: 写失败测试**

```python
# tests/desktop/test_daemon.py
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from desktop import daemon


def test_pick_port_env_valid():
    assert daemon.pick_port("9000") == 9000


def test_pick_port_env_invalid_falls_back():
    assert daemon.pick_port("not-a-port") == 8317
    assert daemon.pick_port(None) == 8317
    assert daemon.pick_port("") == 8317


def test_runtime_payload_shape():
    p = daemon.runtime_payload(8317, "tok")
    assert p["port"] == 8317
    assert p["token"] == "tok"
    assert p["pid"] == os.getpid()
    assert isinstance(p["version"], str) and p["version"]


class _StubServer:
    def __init__(self):
        self.should_exit = False


def test_shutdown_wrong_token_403():
    app = FastAPI()
    daemon.attach_shutdown(app, "secret", _StubServer())
    r = TestClient(app).post("/shutdown",
                             headers={"X-Shutdown-Token": "wrong"})
    assert r.status_code == 403


def test_shutdown_ok_sets_flag():
    srv = _StubServer()
    app = FastAPI()
    daemon.attach_shutdown(app, "secret", srv)
    r = TestClient(app).post("/shutdown",
                             headers={"X-Shutdown-Token": "secret"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    deadline = time.time() + 2
    while not srv.should_exit and time.time() < deadline:
        time.sleep(0.05)
    assert srv.should_exit


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_daemon_process_e2e(tmp_path):
    """子进程完整生命周期：起→runtime.json→health→shutdown→退出"""
    port = _free_port()
    env = {**os.environ, "LLMAPIG_DATA_DIR": str(tmp_path),
           "LLMAPIG_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "desktop.daemon"], env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        rj = tmp_path / "runtime.json"
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stderr.read().decode(errors="replace")
                pytest.fail(f"daemon 提前退出: {out}")
            if rj.exists():
                break
            time.sleep(0.3)
        assert rj.exists(), "30s 未写出 runtime.json"
        meta = json.loads(rj.read_text(encoding="utf-8"))
        assert meta["port"] == port
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r.json() == {"status": "ok"}
        r = httpx.post(f"http://127.0.0.1:{port}/shutdown",
                       headers={"X-Shutdown-Token": meta["token"]},
                       timeout=5)
        assert r.status_code == 200
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_daemon_requires_data_dir(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "LLMAPIG_DATA_DIR"}
    proc = subprocess.run(
        [sys.executable, "-m", "desktop.daemon"], env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True, timeout=30)
    assert proc.returncode == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/desktop/test_daemon.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'desktop.daemon'`

- [ ] **Step 3: 实现 daemon.py**

```python
"""llm-apig daemon 入口：独立 sidecar 进程，由 Tauri 壳拉起。

环境变量（均由壳注入）：
  LLMAPIG_DATA_DIR  数据目录（必需，缺失退出码 2）
  LLMAPIG_PORT      监听端口（缺省 8317）
"""

import json
import os
import secrets
import sys
import threading

import uvicorn


def pick_port(env: str | None) -> int:
    if env:
        try:
            p = int(env)
        except ValueError:
            p = 0
        if 0 < p < 65536:
            return p
    return 8317


def runtime_payload(port: int, token: str) -> dict:
    from app.update_check import current_version
    return {"port": port, "version": current_version(),
            "token": token, "pid": os.getpid()}


def attach_shutdown(app, token: str, server: uvicorn.Server) -> None:
    """POST /shutdown：token 校验通过后置 should_exit 优雅停机。

    仅绑定 127.0.0.1（uvicorn 配置保证），token 防 admin 页被恶意网页
    CSRF 后借 fetch 打该端点。
    """
    from fastapi import HTTPException, Request

    @app.post("/shutdown")
    async def shutdown(request: Request):
        got = request.headers.get("X-Shutdown-Token", "")
        if not secrets.compare_digest(got, token):
            raise HTTPException(status_code=403)
        threading.Timer(
            0.2, lambda: setattr(server, "should_exit", True)).start()
        return {"status": "ok"}


def main() -> int:
    data_env = os.environ.get("LLMAPIG_DATA_DIR")
    if not data_env:
        print("LLMAPIG_DATA_DIR 未设置（daemon 必须由桌面壳拉起）",
              file=sys.stderr)
        return 2

    from desktop import paths
    paths.setup_logging()
    from app.db import init_db
    from app.main import app
    init_db()

    port = pick_port(os.environ.get("LLMAPIG_PORT"))
    token = secrets.token_urlsafe(32)
    (paths.data_dir() / "runtime.json").write_text(
        json.dumps(runtime_payload(port, token)), encoding="utf-8")

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None))
    attach_shutdown(app, token, server)
    server.run()  # 阻塞；should_exit 后返回
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

（模块顶部 `from fastapi import ...` 与 `attach_shutdown` 内部的重复导入按上面保留一份即可——实现时只在函数内导入，顶部不需要。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/desktop/test_daemon.py -v`
Expected: 8 PASS（e2e 用例会起真实子进程，约 5-15s）

- [ ] **Step 5: 全量回归**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（旧壳测试未动，仍通过）

- [ ] **Step 6: Commit**

```bash
git add desktop/daemon.py tests/desktop/test_daemon.py
git commit -m "feat: daemon sidecar 入口（端口选择/runtime.json/shutdown token）"
```

---

### Task 2: src-tauri 脚手架 + splash 页 + 图标

**Files:**
- Create: `src-tauri/Cargo.toml`、`src-tauri/build.rs`、`src-tauri/tauri.conf.json`、`src-tauri/capabilities/default.json`、`src-tauri/src/main.rs`、`src-tauri/src/lib.rs`（本任务最小版）
- Create: `desktop-ui/index.html`（splash）
- Create: `desktop/make_icons.py`（一次性图标生成脚本）+ 生成物 `src-tauri/icons/*`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: 无（首个 Rust 任务）
- Produces:
  - 可编译的 Tauri 2 工程骨架，窗口 label `main`，标题 `llm-apig`，1200x800
  - `desktop-ui/index.html` 暴露 `window.__shellError(title, detail)` 全局函数（Task 3 的 Rust 通过 eval 调它显示错误）
  - 前置条件：`cargo install tauri-cli --version "^2"`（一次性，编译约 10 分钟）

- [ ] **Step 1: 安装 tauri-cli（若未装）**

Run: `cargo tauri --version`
若报找不到命令：`cargo install tauri-cli --version "^2" --locked`
Expected: 输出 `tauri-cli 2.x.y`

- [ ] **Step 2: 图标生成脚本 + 生成图标**

```python
# desktop/make_icons.py
"""从托盘同款图形生成 src-tauri/icons/（改图标时手动跑一次）。

产物：icon.ico(16/32/48/256) + 32x32.png + 128x128.png + 128x128@2x.png
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src-tauri" / "icons"


def make_icon_image() -> Image.Image:
    """与网页 favicon 同款：深蓝圆角块 + 三条横线"""
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16, 16, 496, 496], radius=112, fill=(15, 43, 70, 255))
    d.rectangle([112, 176, 400, 216], fill=(255, 255, 255, 235))
    d.rectangle([112, 248, 304, 288], fill=(127, 176, 221, 235))
    d.rectangle([112, 320, 352, 360], fill=(255, 255, 255, 235))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = make_icon_image()
    img.save(OUT / "icon.ico",
             sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    img.resize((32, 32), Image.LANCZOS).save(OUT / "32x32.png")
    img.resize((128, 128), Image.LANCZOS).save(OUT / "128x128.png")
    img.resize((256, 256), Image.LANCZOS).save(OUT / "128x128@2x.png")
    print(f"图标已生成: {OUT}")


if __name__ == "__main__":
    main()
```

Run: `uv run python desktop/make_icons.py`
Expected: 打印 `图标已生成: ...\src-tauri\icons`

- [ ] **Step 3: Cargo.toml / build.rs / main.rs**

```toml
# src-tauri/Cargo.toml
[package]
name = "llm-apig-shell"
version = "0.1.0"
description = "llm-apig 桌面壳"
edition = "2021"

[lib]
name = "llm_apig_shell_lib"
crate-type = ["lib", "cdylib", "staticlib"]

[build-dependencies]
tauri-build = { version = "2", features = [] }

[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-single-instance = "2"
tauri-plugin-autostart = "2"
tauri-plugin-dialog = "2"
tauri-plugin-updater = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
ureq = { version = "2", features = ["json"] }

[target.'cfg(windows)'.dependencies]
windows-sys = { version = "0.59", features = [
    "Win32_System_JobObjects", "Win32_System_Threading",
    "Win32_Foundation", "Win32_Security",
] }
```

```rust
// src-tauri/build.rs
fn main() {
    tauri_build::build()
}
```

```rust
// src-tauri/src/main.rs
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    llm_apig_shell_lib::run()
}
```

```rust
// src-tauri/src/lib.rs — 本任务最小版（窗口 + splash），Task 3/4/5 逐步充实
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running llm-apig");
}
```

- [ ] **Step 4: tauri.conf.json + capabilities**

```json
// src-tauri/tauri.conf.json
{
  "$schema": "https://raw.githubusercontent.com/tauri-apps/tauri/dev/crates/tauri-config-schema/schema.json",
  "productName": "llm-apig",
  "identifier": "com.llm-apig.desktop",
  "version": "0.1.0",
  "build": {
    "frontendDist": "../desktop-ui",
    "devUrl": "http://127.0.0.1:8317/admin"
  },
  "app": {
    "withGlobalTauri": true,
    "windows": [
      {
        "title": "llm-apig",
        "width": 1200,
        "height": 800,
        "center": true
      }
    ]
  },
  "bundle": {
    "active": true,
    "targets": ["nsis"],
    "resources": ["resources/llm-apig-daemon.exe"],
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.ico"
    ],
    "windows": {
      "webviewInstallMode": { "type": "downloadBootstrapper" },
      "nsis": {
        "installMode": "currentUser",
        "languages": ["SimpChinese"]
      }
    }
  }
}
```

```json
// src-tauri/capabilities/default.json
{
  "identifier": "default",
  "windows": ["main"],
  "remote": { "urls": ["http://127.0.0.1:*", "http://localhost:*"] },
  "permissions": [
    "core:default",
    "updater:default",
    "core:event:default",
    "core:webview:default",
    "core:window:default"
  ]
}
```

注意：`remote.urls` 授权 daemon 域（127.0.0.1 任意端口）上的页面调用
`window.__TAURI__.core.invoke`——admin 页跳转过去后仍是 remote 页面，必须
在此授权。`updater:default` 覆盖 updater 插件命令；`core:webview/window`
覆盖 `restart_app` 里用到的 app 重启能力。若运行时 invoke 报
permission denied，按报错补对应插件权限到这个数组。

- [ ] **Step 5: splash 页**

```html
<!-- desktop-ui/index.html -->
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>llm-apig</title>
<style>
  body { margin: 0; height: 100vh; display: flex; align-items: center;
         justify-content: center; font-family: "Microsoft YaHei", sans-serif;
         background: #0f2b46; color: #fff; }
  .box { text-align: center; }
  .spin { width: 36px; height: 36px; margin: 0 auto 18px;
          border: 4px solid rgba(255,255,255,.2);
          border-top-color: #7fb0dd; border-radius: 50%;
          animation: r 1s linear infinite; }
  @keyframes r { to { transform: rotate(360deg); } }
  #err { display: none; max-width: 640px; text-align: left;
         background: #5b1f1f; border-radius: 8px; padding: 16px 20px;
         white-space: pre-wrap; font-size: 13px; line-height: 1.6; }
</style>
</head>
<body>
<div class="box">
  <div class="spin" id="spin"></div>
  <div id="msg">正在启动 llm-apig 网关…</div>
  <div id="err"></div>
</div>
<script>
// 壳在 daemon 启动失败时 eval 调用此函数（Task 3）
window.__shellError = function (title, detail) {
  document.getElementById('spin').style.display = 'none';
  document.getElementById('msg').textContent = title;
  var e = document.getElementById('err');
  e.style.display = 'block';
  e.textContent = detail;
};
</script>
</body>
</html>
```

- [ ] **Step 6: .gitignore 追加**

在 `.gitignore` 末尾追加：

```
src-tauri/target/
src-tauri/resources/
src-tauri/gen/
.tauri/
```

（`gen/` 是 tauri-build 生成的 schema；`resources/` 由构建脚本拷贝 daemon exe，不入库。）

- [ ] **Step 7: 编译验证**

Run: `cd src-tauri && cargo build`
Expected: 编译成功（首次拉依赖较久）。若报 icons 缺失，确认 Step 2 产物在 `src-tauri/icons/`。

- [ ] **Step 8: Commit**

```bash
git add src-tauri desktop-ui desktop/make_icons.py .gitignore
git commit -m "feat: Tauri 2 壳脚手架（窗口/splash/图标/NSIS 配置）"
```

---

### Task 3: daemon.rs 生命周期 + 壳接线

**Files:**
- Create: `src-tauri/src/daemon.rs`
- Modify: `src-tauri/src/lib.rs`（替换 Task 2 最小版）

**Interfaces:**
- Consumes: `desktop/daemon.py` 的进程协议（env `LLMAPIG_DATA_DIR`/`LLMAPIG_PORT`，`runtime.json` 的 `{port, version, token, pid}`，`POST /shutdown` 头 `X-Shutdown-Token`，`GET /health`）；`desktop-ui/index.html` 的 `window.__shellError(title, detail)`
- Produces:
  - `pub struct DaemonHandle { child: Child, port: u16, pub log: Option<fs::File> }` + `impl DaemonHandle { pub fn spawn() -> Result<Self, String>; pub fn port(&self) -> u16; pub fn graceful_stop(&self) }`
  - `pub(crate) fn append_log(file: &Option<fs::File>, level: &str, msg: &str)`
  - lib.rs `AppState { daemon: Mutex<Option<DaemonHandle>> }`

- [ ] **Step 1: daemon.rs**

从 tether-desktop `src-tauri/src/daemon.rs` 适配（保留 Job Object/端口探测/find_binary/日志转发/健康检查的结构），差异点：去除 pair-secret 捕获与 TETHER_* 环境变量；健康检查通过后读 runtime.json 拿 token 存进 handle；新增 `graceful_stop()`。完整目标代码：

```rust
//! daemon 生命周期管理：spawn / Job Object 防孤儿 / 健康检查 / 日志转发 / 优雅停机。

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::Duration;

const HEALTH_TIMEOUT_S: u32 = 30;
const HEALTH_POLL_MS: u64 = 300;

#[cfg(windows)]
struct JobHandle(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for JobHandle {}

#[cfg(windows)]
impl Drop for JobHandle {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

/// 把子进程挂进 KILL_ON_JOB_CLOSE Job：壳以任何方式终止（崩溃/强杀）→
/// OS 销毁 Job → daemon 整棵进程树被杀，杜绝孤儿 daemon 占端口。
#[cfg(windows)]
fn assign_kill_on_close(child: &Child) -> Result<JobHandle, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(format!("CreateJobObjectW: {}", std::io::Error::last_os_error()));
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            let e = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("SetInformationJobObject: {}", e));
        }
        if AssignProcessToJobObject(job, child.as_raw_handle()) == 0 {
            let e = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("AssignProcessToJobObject: {}", e));
        }
        Ok(JobHandle(job))
    }
}

/// TCP connect 探测端口是否被监听（不需要 HTTP 成功）。
fn port_in_use(port: u16) -> bool {
    std::net::TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(300),
    )
    .is_ok()
}

/// 8317 起找第一个空闲端口（最多 +19）。被占大概率是上次残留的孤儿
/// daemon —— 换端口而不是杀进程，不误杀。
fn pick_free_port(requested: u16) -> u16 {
    for port in requested..requested.saturating_add(20) {
        if !port_in_use(port) {
            return port;
        }
    }
    requested
}

/// 数据目录：环境变量优先（冒烟自检用），缺省 %APPDATA%\llm-apig。
fn data_dir() -> PathBuf {
    if let Ok(d) = std::env::var("LLMAPIG_DATA_DIR") {
        return PathBuf::from(d);
    }
    let appdata = std::env::var("APPDATA").unwrap_or_default();
    PathBuf::from(appdata).join("llm-apig")
}

pub struct DaemonHandle {
    child: Child,
    port: u16,
    shutdown_token: String,
    /// desktop.log 共享句柄
    pub log: Option<fs::File>,
    #[cfg(windows)]
    _job: Option<JobHandle>,
}

fn ensure_log_dir(dir: &std::path::Path) -> PathBuf {
    let d = dir.join("logs");
    let _ = fs::create_dir_all(&d);
    d
}

fn open_log(log_dir: &std::path::Path, name: &str) -> Option<fs::File> {
    fs::File::create(log_dir.join(name)).ok()
}

pub(crate) fn append_log(file: &Option<fs::File>, level: &str, msg: &str) {
    if let Some(mut f) = file.as_ref() {
        let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
        let _ = writeln!(f, "[{}] [{}] {}", ts, level, msg);
    }
}

/// 递归搜索目录找 daemon exe（MSI/NSIS resource 落点不稳定）。
fn find_daemon_exe(dir: &std::path::Path, depth: u32) -> Option<PathBuf> {
    if depth > 4 {
        return None;
    }
    let p = dir.join("llm-apig-daemon.exe");
    if p.exists() {
        return Some(p);
    }
    if let Ok(entries) = fs::read_dir(dir) {
        for e in entries.flatten() {
            if e.path().is_dir() {
                if let Some(found) = find_daemon_exe(&e.path(), depth + 1) {
                    return Some(found);
                }
            }
        }
    }
    None
}

impl DaemonHandle {
    pub fn spawn(app: &tauri::AppHandle) -> Result<Self, String> {
        let requested: u16 = std::env::var("LLMAPIG_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(8317);

        let exe_dir = std::env::current_exe()
            .unwrap_or_default()
            .parent()
            .map(PathBuf::from)
            .unwrap_or_default();
        let project_root = exe_dir
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .map(PathBuf::from)
            .unwrap_or_default();

        let data = data_dir();
        let _ = fs::create_dir_all(&data);
        let log_dir = ensure_log_dir(&data);
        let desktop_log = open_log(&log_dir, "desktop.log");
        append_log(&desktop_log, "INFO", &format!("exe_dir: {}", exe_dir.display()));

        let port = pick_free_port(requested);
        if port != requested {
            append_log(&desktop_log, "WARN", &format!(
                "端口 {} 被占用（疑似残留 daemon），改用 {}", requested, port));
        }

        let daemon_exe = find_daemon_exe(&exe_dir, 0);
        let mut cmd = if let Some(ref exe) = daemon_exe {
            Command::new(exe)
        } else {
            // dev 兜底：打包 daemon 不在时回落源码运行
            let python = std::env::var("LLMAPIG_PYTHON")
                .unwrap_or_else(|_| "python".to_string());
            let mut c = Command::new(&python);
            c.args(["-m", "desktop.daemon"]).current_dir(&project_root);
            c
        };
        cmd.env("LLMAPIG_DATA_DIR", &data)
            .env("LLMAPIG_PORT", port.to_string())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        // daemon 无窗口：隐藏控制台，避免 Windows Terminal 弹黑窗
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        append_log(&desktop_log, "INFO", &format!(
            "spawning daemon (bundled={}, port={})",
            daemon_exe.is_some(), port));

        let mut child = cmd.spawn().map_err(|e| {
            append_log(&desktop_log, "ERROR", &format!("spawn failed: {}", e));
            format!("无法启动 daemon: {}", e)
        })?;
        append_log(&desktop_log, "INFO", &format!("daemon pid: {}", child.id()));

        #[cfg(windows)]
        let job = match assign_kill_on_close(&child) {
            Ok(j) => {
                append_log(&desktop_log, "INFO", "daemon assigned to kill-on-close job");
                Some(j)
            }
            Err(e) => {
                append_log(&desktop_log, "WARN",
                    &format!("job assign failed（无孤儿保护）: {}", e));
                None
            }
        };

        // daemon stdout/stderr → logs/daemon.log / daemon.err.log
        if let Some(stdout) = child.stdout.take() {
            let f = open_log(&log_dir, "daemon.log");
            thread::spawn(move || {
                for line in BufReader::new(stdout).lines().flatten() {
                    append_log(&f, "OUT", &line);
                }
            });
        }
        if let Some(stderr) = child.stderr.take() {
            let f = open_log(&log_dir, "daemon.err.log");
            thread::spawn(move || {
                for line in BufReader::new(stderr).lines().flatten() {
                    append_log(&f, "ERR", &line);
                }
            });
        }

        // 健康检查轮询
        let health_url = format!("http://127.0.0.1:{}/health", port);
        let start = std::time::Instant::now();
        loop {
            let elapsed = start.elapsed().as_secs();
            if elapsed > HEALTH_TIMEOUT_S as u64 {
                let _ = child.kill();
                append_log(&desktop_log, "ERROR",
                    &format!("timeout after {}s", HEALTH_TIMEOUT_S));
                return Err(format!("daemon 在 {}s 内未就绪", HEALTH_TIMEOUT_S));
            }
            match child.try_wait() {
                Ok(Some(status)) => {
                    append_log(&desktop_log, "ERROR",
                        &format!("daemon exited: {:?}", status));
                    return Err(format!("daemon 意外退出: {:?}", status));
                }
                Ok(None) => {}
                Err(e) => return Err(format!("检查 daemon 状态失败: {}", e)),
            }
            if let Ok(resp) = ureq::get(&health_url).timeout(Duration::from_secs(3)).call() {
                if resp.status() == 200 {
                    append_log(&desktop_log, "INFO",
                        &format!("daemon ready ({}s)", elapsed));
                    break;
                }
            }
            thread::sleep(Duration::from_millis(HEALTH_POLL_MS));
        }

        // 健康检查通过后从 runtime.json 读 shutdown token（文件为准）
        let runtime = fs::read_to_string(data.join("runtime.json"))
            .map_err(|e| format!("读 runtime.json 失败: {}", e))?;
        let v: serde_json::Value = serde_json::from_str(&runtime)
            .map_err(|e| format!("解析 runtime.json 失败: {}", e))?;
        let shutdown_token = v
            .get("token")
            .and_then(|t| t.as_str())
            .unwrap_or("")
            .to_string();

        let log_for_handle = desktop_log.as_ref().and_then(|f| f.try_clone().ok());
        Ok(DaemonHandle {
            child,
            port,
            shutdown_token,
            log: log_for_handle,
            #[cfg(windows)]
            _job: job,
        })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// 优雅停机：POST /shutdown（3s 超时）。失败由 Drop 的 kill + Job 兜底。
    pub fn graceful_stop(&self) {
        if self.shutdown_token.is_empty() {
            return;
        }
        let url = format!("http://127.0.0.1:{}/shutdown", self.port);
        let _ = ureq::post(&url)
            .set("X-Shutdown-Token", &self.shutdown_token)
            .timeout(Duration::from_secs(3))
            .call();
    }
}

impl Drop for DaemonHandle {
    fn drop(&mut self) {
        self.graceful_stop();
        let _ = self.child.kill();
        for _ in 0..50 {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                _ => std::thread::sleep(Duration::from_millis(100)),
            }
        }
    }
}
```

注意：`append_log` 里用了 `chrono` —— 在 `Cargo.toml` 的 `[dependencies]` 加 `chrono = "0.4"`（tether 同款做法）。

崩溃自动重启（spec 错误处理表"daemon 运行中崩溃"行）：在 daemon.rs 加监控线程，`spawn()` 成功返回前启动：

```rust
/// 崩溃监控：daemon 异常退出 → 重启一次；再挂则停（由用户手动处理）。
/// 放在 DaemonHandle::spawn() 末尾、构造 handle 前调用。
fn spawn_crash_watcher(
    handle: tauri::AppHandle,
    child_id: u32,
) {
    std::thread::spawn(move || loop {
        let state = handle.state::<crate::AppState>();
        let guard = state.daemon.lock().unwrap();
        if let Some(ref dh) = *guard {
            if dh.child_id() == child_id {
                match dh.try_wait() {
                    Ok(Some(status)) => {
                        drop(guard);
                        log_to_desktop(&format!(
                            "daemon 崩溃({:?})，自动重启一次", status));
                        // take 出旧 handle（Drop 会 kill——进程已死，无害）
                        let old = state.daemon.lock().unwrap().take();
                        drop(old);
                        match DaemonHandle::spawn() {
                            Ok(dh) => {
                                *state.daemon.lock().unwrap() = Some(dh);
                                if let Some(w) = handle.get_webview_window("main") {
                                    let _ = w.eval(&format!(
                                        "window.location.replace('http://127.0.0.1:{}/admin')",
                                        dh.port()));
                                }
                            }
                            Err(e) => log_to_desktop(&format!("重启失败: {}", e)),
                        }
                        return; // 只重启一次，新实例由新监控线程负责（spawn 递归装新 watcher）
                    }
                    Ok(None) => {} // 还活着
                    Err(_) => return,
                }
            } else {
                return; // handle 已被替换（重启过/退出流程），本 watcher 退役
            }
        } else {
            return; // 正常退出流程已 take 走 handle
        }
        drop(guard);
        std::thread::sleep(std::time::Duration::from_secs(2));
    });
}
```

配套小改：`DaemonHandle` 加 `pub fn child_id(&self) -> u32 { self.child.id() }` 与 `pub fn try_wait(&self) -> std::io::Result<Option<std::process::ExitStatus>> { self.child.try_wait() }`（字段级方法，child 是私有字段）；`spawn_crash_watcher` 需要 AppHandle，所以 `DaemonHandle::spawn()` 加参数 `app: &tauri::AppHandle`，lib.rs 调用处传 `&handle`（Task 3 Step 2 的代码里 `daemon::DaemonHandle::spawn()` 改为 `daemon::DaemonHandle::spawn(&handle.clone())`）。tray.rs/updater_cmds.rs 里调 `DaemonHandle::spawn` 的地方不存在（它们只消费已有 handle），无需改。

- [ ] **Step 2: lib.rs 接线（daemon spawn → splash 跳转 admin）**

```rust
// src-tauri/src/lib.rs — Task 3 版
mod daemon;

use std::sync::Mutex;
use tauri::Manager;

struct AppState {
    daemon: Mutex<Option<daemon::DaemonHandle>>,
}

pub fn run() {
    tauri::Builder::default()
        .manage(AppState { daemon: Mutex::new(None) })
        .setup(|app| {
            // dev 模式（tauri dev，devUrl 指向开发 uvicorn）不起 sidecar
            if cfg!(dev) {
                return Ok(());
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                match daemon::DaemonHandle::spawn(&handle.clone()) {
                    Ok(dh) => {
                        let port = dh.port();
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.eval(&format!(
                                "window.location.replace('http://127.0.0.1:{}/admin')",
                                port));
                        }
                        let state = handle.state::<AppState>();
                        *state.daemon.lock().unwrap() = Some(dh);
                    }
                    Err(e) => {
                        daemon::log_to_desktop(&format!("daemon 启动失败: {}", e));
                        if let Some(window) = handle.get_webview_window("main") {
                            let detail = format!(
                                "{}\n\n日志目录：{}\\logs",
                                e,
                                daemon::data_dir().display());
                            let _ = window.eval(&format!(
                                "window.__shellError('llm-apig 启动失败', {})",
                                serde_json::to_string(&detail).unwrap()));
                        }
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running llm-apig");
}
```

实现说明：错误分支调用的 `daemon::log_to_desktop(msg)` 是 daemon.rs 里新增的便捷函数（打开 logs/desktop.log 追加一行，见下方"配套"）；`daemon::data_dir()` 要求 daemon.rs 里 `fn data_dir` 标记为 `pub(crate)`。

配套（daemon.rs，与 Step 1 一起加）：

```rust
/// 便捷日志：没有 DaemonHandle（spawn 失败/托盘等）时写 desktop.log。
pub(crate) fn log_to_desktop(msg: &str) {
    let dir = ensure_log_dir(&data_dir());
    let f = open_log(&dir, "desktop.log");
    append_log(&f, "INFO", msg);
}
```

注意 `open_log` 是 truncate 语义（tether 同款）——`log_to_desktop` 在 daemon 已就绪后调用会截断 desktop.log。可接受（desktop.log 只是启动诊断用，daemon.log 才是运行日志），实现时不改。

另：`if cfg!(dev)` 在 release 构建的壳里为 false、`cargo tauri dev` 下为 true，用它区分"是否起 sidecar"是 Tauri 惯例，可行。

- [ ] **Step 3: 编译验证**

Run: `cd src-tauri && cargo build`
Expected: 编译成功，无 warning（`use tauri::Manager` 若未用到会警告，按需保留）。

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src
git commit -m "feat: daemon 生命周期管理（Job Object/健康检查/优雅停机）与壳接线"
```

---

### Task 4: 托盘 + 单实例 + 自启 + 关窗隐藏

**Files:**
- Create: `src-tauri/src/tray.rs`
- Modify: `src-tauri/src/lib.rs`（注册插件与托盘）

**Interfaces:**
- Consumes: Task 3 的 `AppState.daemon`（退出时取 handle 调 `graceful_stop`，Drop 自带 kill 兜底）
- Produces: 托盘菜单（打开主界面/开机自启勾选/检查更新/退出）；第二实例启动时前置主窗口；关窗 = 隐藏到托盘

- [ ] **Step 1: tray.rs**

```rust
//! 系统托盘：菜单四项（对等旧 pystray）+ 关窗隐藏 + 托盘退出。

use tauri::{
    menu::{CheckMenuItem, MenuBuilder, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime,
};
use tauri_plugin_autostart::ManagerExt;

use crate::daemon;

pub fn setup_tray<R: Runtime>(app: &AppHandle<R>) -> Result<(), Box<dyn std::error::Error>> {
    let show: MenuItem<R> = MenuItem::with_id(app, "show", "打开主界面", true, None::<&str>)?;
    let autostart: CheckMenuItem<R> =
        CheckMenuItem::with_id(app, "autostart", "开机自启", true,
            app.autolaunch().is_enabled().unwrap_or(false), None::<&str>)?;
    let check_update: MenuItem<R> =
        MenuItem::with_id(app, "check_update", "检查更新", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit: MenuItem<R> = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;

    let menu = MenuBuilder::new(app)
        .item(&show)
        .item(&autostart)
        .item(&check_update)
        .item(&sep)
        .item(&quit)
        .build()?;

    TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .tooltip("llm-apig API 网关")
        .icon(app.default_window_icon().cloned().unwrap())
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "show" => show_main(app),
            "autostart" => {
                let mgr = app.autolaunch();
                let enabled = mgr.is_enabled().unwrap_or(false);
                let result = if enabled { mgr.disable() } else { mgr.enable() };
                if let Err(e) = result {
                    daemon::append_log_tray(app, &format!("autostart 切换失败: {}", e));
                }
            }
            "check_update" => {
                // Task 5 Step 3 会把这里替换为 updater_cmds::tray_check_update(app);
                // 本任务先最小实现：打开主窗口（横幅在页内显示更新状态）
                show_main(app);
            }
            "quit" => {
                if let Some(dh) = app.state::<crate::AppState>()
                    .daemon.lock().unwrap().take()
                {
                    dh.graceful_stop();
                    // drop(dh) 触发 Drop：kill + Job 清理
                }
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;

    // 关窗 = 隐藏（服务继续），托盘退出才是真退出
    if let Some(window) = app.get_webview_window("main") {
        let w = window.clone();
        window.on_window_event(move |event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = w.hide();
            }
        });
    }
    Ok(())
}

fn show_main<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}
```

实现说明：
1. `daemon::append_log_tray` 不存在——tray.rs 里统一用 Task 3 配套的 `daemon::log_to_desktop(&format!(...))`。上面代码块中 `daemon::append_log_tray(app, &format!("autostart 切换失败: {}", e));` 一行替换为 `daemon::log_to_desktop(&format!("autostart 切换失败: {}", e));`。
2. 旧 pystray 是"双击图标 = 打开主界面"（default=True 菜单项即双击行为），上面用 `DoubleClick` 事件对齐；若想单击就打开可改 `Click`。
3. `use crate::daemon;` 若实际只用到 `log_to_desktop`，编译器会对未使用导入告警——按 cargo build 输出清理。

- [ ] **Step 2: lib.rs 注册插件 + 托盘 + 单实例**

在 `tauri::Builder::default()` 后链式加入（放在 `.manage(...)` 之前）：

```rust
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_dialog::init())
```

并在 `setup` 末尾（`Ok(())` 之前）加托盘（托盘 API 内部异步，放 async runtime）：

```rust
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = tray::setup_tray(&app_handle) {
                    daemon::log_to_desktop(&format!("托盘初始化失败: {}", e));
                }
            });
```

lib.rs 顶部补 `mod tray;`。

- [ ] **Step 3: 编译验证**

Run: `cd src-tauri && cargo build`
Expected: 编译成功。

- [ ] **Step 4: Commit**

```bash
git add src-tauri/src
git commit -m "feat: 托盘菜单/单实例/自启/关窗隐藏"
```

---

### Task 5: Tauri updater 自动更新

**Files:**
- Create: `src-tauri/src/updater_cmds.rs`（check/install/restart 三个命令）
- Create: `.tauri/`（私钥，gitignore 已覆盖）
- Modify: `src-tauri/src/lib.rs`（注册 updater 插件 + 命令）、`src-tauri/tauri.conf.json`（plugins.updater）、`app/admin/templates/base.html`（横幅 JS 改 invoke）

**Interfaces:**
- Consumes: Task 3 `DaemonHandle::graceful_stop`（重启前优雅停 daemon）
- Produces:
  - 壳命令（admin 页 JS 可 invoke）：`check_update() -> {version, notes} | null`、`install_update() -> String`（错误消息）、`restart_app()`
  - `latest.json` 发布协议：Tauri updater 标准 schema，由 `cargo tauri build` 自动生成

- [ ] **Step 1: 生成 minisign 密钥对**

Run: `cargo tauri signer generate -w .tauri/llm-apig.key`
（密码直接回车留空——单人项目，私钥文件本身是机密；备份提示见 Step 5。）
Expected: 输出公钥（一段 base64）并写出 `.tauri/llm-apig.key`（私钥）与 `.tauri/llm-apig.key.pub`（公钥）。
**私钥备份到密码管理器/离线介质——丢失则已发布版本无法再自动更新。**

- [ ] **Step 2: tauri.conf.json 加 updater 配置**

先取仓库远端：Run: `git remote -v` → 取 jihulab.com 上的 `<owner>/<repo>`（下同）。

在 tauri.conf.json 顶层加（`<PUBKEY>` 替换为 Step 1 输出的公钥字符串；`<owner>/<repo>` 替换为远端路径）：

```json
  "plugins": {
    "updater": {
      "pubkey": "<PUBKEY>",
      "endpoints": [
        "https://jihulab.com/<owner>/<repo>/-/releases/permalink/latest/assets/1/latest.json"
      ]
    }
  }
```

endpoint URL 说明：GitLab 系（jihulab 同源码）Release 资产直链格式为
`https://jihulab.com/<owner>/<repo>/-/releases/permalink/latest/assets/<asset-id>/<filename>`，
其中 `<asset-id>` 是上传 latest.json 后 Release 页面里该资产的数字 id
（permalink/latest 固定指最新 Release，发布新版本无需改壳内配置）。
**首次发版前拿不到 asset id**——先用占位 id 构建，发完首个 Release 后把
真实 id 填进 tauri.conf.json 再出正式版（一次性操作）。若 jihulab 的
资产直链格式与之不符，以 Release 页面"复制资产链接"按钮给出的格式为准。

- [ ] **Step 3: updater_cmds.rs + lib.rs 注册**

```rust
//! 自动更新命令：admin 页横幅经 window.__TAURI__.core.invoke 调用。

use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_updater::UpdaterExt;

use crate::daemon;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInfo {
    pub version: String,
    pub notes: String,
}

/// 检查更新：None = 已是最新。错误一律返回 Err(String) 由前端 catch 显示。
#[tauri::command]
pub async fn check_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?;
    Ok(update.map(|u| UpdateInfo {
        version: u.version.clone(),
        notes: u.body.clone().unwrap_or_default(),
    }))
}

/// 下载并安装（不自动重启）。顺序：下载完成 → 优雅停 daemon → 安装。
/// 下载失败时 daemon 仍在跑，admin 页面不中断。
#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or("已是最新版本")?;
    // tauri-plugin-updater 2.x: download() 返回 Result<Vec<u8>>
    let bytes: Vec<u8> = update
        .download(|_chunk: usize, _total: Option<u64>| {}, || {})
        .await
        .map_err(|e| format!("下载失败: {}", e))?;
    // daemon 先优雅停（admin 页面即将随重启断开，无所谓）
    if let Some(dh) = app.state::<crate::AppState>().daemon.lock().unwrap().take() {
        dh.graceful_stop();
    }
    update.install(bytes)
        .await
        .map_err(|e| format!("安装失败: {}", e))?;
    Ok(())
}

/// 重启壳（安装完成后前端确认调用）。
#[tauri::command]
pub fn restart_app(app: AppHandle) {
    if let Some(dh) = app.state::<crate::AppState>().daemon.lock().unwrap().take() {
        dh.graceful_stop();
    }
    app.restart();
}

/// 托盘"检查更新"共用入口（tray.rs Task 4 的 check_update 分支调这里）。
pub fn tray_check_update(app: &AppHandle) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        match check_update(handle.clone()).await {
            Ok(Some(info)) => {
                let _ = handle.dialog()
                    .message(format!("新版本 {} 可用，请打开主界面点击横幅更新",
                                     info.version))
                    .kind(MessageDialogKind::Info)
                    .title("llm-apig 更新")
                    .show(|_| {});
                if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            Ok(None) => {
                let _ = handle.dialog().message("已是最新版本")
                    .kind(MessageDialogKind::Info)
                    .title("llm-apig")
                    .show(|_| {});
            }
            Err(e) => {
                daemon::log_to_desktop(&format!("托盘检查更新失败: {}", e));
                let _ = handle.dialog().message(format!("检查更新失败：{}", e))
                    .kind(MessageDialogKind::Error)
                    .title("llm-apig")
                    .show(|_| {});
            }
        }
    });
}
```

lib.rs 修改：
- 顶部 `mod updater_cmds;`
- Builder 链上加 `.plugin(tauri_plugin_updater::Builder::new().build())`
- `.invoke_handler(tauri::generate_handler![
     updater_cmds::check_update,
     updater_cmds::install_update,
     updater_cmds::restart_app,
   ])`
- tray.rs 的 `"check_update"` 分支改为 `updater_cmds::tray_check_update(app);`（删掉 Task 4 的占位 `show_main(app);`）

`update.download(...)` 签名以 crates.io 上 tauri-plugin-updater 2.x 为准：`pub async fn download<C: FnMut(usize, Option<u64>), D: FnOnce()>(&self, on_chunk: C, on_download_finish: D) -> Result<Vec<u8>>`。若 cargo build 报签名不符，以编译器提示的实参形态微调闭包，语义不变。

- [ ] **Step 4: admin 横幅 JS 改造（base.html）**

`<script>` 块整体替换为（`<owner>/<repo>` 同 Step 2 取值；RELEASES_PAGE 用发布页地址 `https://jihulab.com/<owner>/<repo>/-/releases`）：

```html
<script>
(function(){
  // 桌面壳内（Tauri）走自动更新；普通浏览器打开（dev）无 __TAURI__ 则跳过
  var T = window.__TAURI__;
  if (!T || !T.core) return;
  var invoke = T.core.invoke;
  var RELEASES_PAGE = 'https://jihulab.com/<owner>/<repo>/-/releases';
  var banner = document.getElementById('upd-banner');
  var text = document.getElementById('upd-text');
  var link = document.getElementById('upd-link');
  function show(version, notes, action){
    text.textContent = '新版本 ' + version + ' 可用' +
      (notes ? '：' + notes : '');
    link.textContent = action;
    link.onclick = function(){
      link.textContent = '正在下载…';
      link.onclick = null;
      invoke('install_update').then(function(){
        text.textContent = '下载完成，重启后生效';
        link.textContent = '重启并安装';
        link.onclick = function(){
          invoke('restart_app');  // 用户已点更新，重启即预期，不再二次确认
        };
      }).catch(function(e){
        text.textContent = '更新失败：' + e;
        link.textContent = '手动下载';
        link.onclick = function(){ location.href = RELEASES_PAGE; };
      });
      return false;
    };
    banner.style.display = 'flex';
  }
  invoke('check_update').then(function(u){
    if (u) show(u.version, u.notes, '立即更新');
  }).catch(function(){});
})();
</script>
```

（横幅的 HTML 结构 `#upd-banner/#upd-text/#upd-link` 不动，只换脚本。`link.onclick` 返回 `false` 阻止 `<a>` 默认导航；Tauri WebView2 里原生 `confirm()` 不可用，故不二次确认。普通浏览器打开 admin 时 `__TAURI__` 不存在直接 return，横幅不显示——dev 下验证更新横幅需跑壳。）

- [ ] **Step 5: 编译 + 回归**

Run: `cd src-tauri && cargo build`
Expected: 编译成功。

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（base.html 改动不影响现有测试；旧 /admin/api/update 端点本任务不动，Task 7 删）。

- [ ] **Step 6: Commit**

```bash
git add src-tauri app/admin/templates/base.html
git commit -m "feat: Tauri updater 自动更新（横幅 invoke/托盘/优雅停机后重启）"
```

---

### Task 6: 构建链重写（build.py + 冒烟自检）

**Files:**
- Modify: `desktop/build.py`（整体重写）
- Modify: `pyproject.toml`（依赖调整）、`uv.lock`（随 uv 命令更新）

**Interfaces:**
- Consumes: Task 1-5 全部产物；环境变量 `TAURI_PRIVATE_KEY`（Task 5 生成的私钥内容或路径）
- Produces:
  - `uv run python desktop/build.py` 一键：pytest → PyInstaller daemon → cargo tauri build → 冒烟（健康/优雅停/Job 强杀三段）
  - 产物 `dist/llm-apig-setup-<ver>.exe` + bundle 目录内 `latest.json`

- [ ] **Step 1: pyproject 依赖调整**

dependencies 删除 `"pywebview>=5"`、`"pystray>=0.19"`；`Pillow>=10` 从 dependencies 移到 dev 组（图标脚本用）。结果：

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "httpx>=0.27",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "pyinstaller>=6",
    "Pillow>=10",
]
```

Run: `uv lock && uv sync`
Expected: lock 更新，无错误。

（旧壳 desktop/main.py 此时 import pywebview 会失败——但旧壳在 Task 7 删除，期间不运行旧壳即可；tests/desktop 旧壳测试里 test_tray/test_server 若因依赖缺失报收集错误，先在 Task 6 顺带 `git rm tests/desktop/test_tray.py tests/desktop/test_server.py`（它们测的模块 Task 7 删），test_autostart/test_singleinstance 不 import 第三方库，留到 Task 7 一起删。）

- [ ] **Step 2: 重写 build.py**

```python
"""一键构建：pytest → PyInstaller daemon → tauri build (NSIS) → 冒烟三段自检"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SRC_TAURI = ROOT / "src-tauri"
RESOURCES = SRC_TAURI / "resources"
BUNDLE = SRC_TAURI / "target" / "release" / "bundle"


def run(cmd: list, **kw) -> None:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT, **kw)


def version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def sync_tauri_version(ver: str) -> None:
    conf = SRC_TAURI / "tauri.conf.json"
    data = json.loads(conf.read_text(encoding="utf-8"))
    data["version"] = ver
    conf.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"tauri.conf.json 版本同步: {ver}")


def pyinstaller_daemon() -> Path:
    run([
        "uv", "run", "pyinstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "llm-apig-daemon",
        "--paths", ".",
        "--add-data", "app/admin/templates;app/admin/templates",
        "--add-data", "pyproject.toml;.",
        "desktop/daemon.py",
    ])
    exe = ROOT / "dist" / "llm-apig-daemon.exe"
    RESOURCES.mkdir(parents=True, exist_ok=True)
    shutil.copy(exe, RESOURCES / "llm-apig-daemon.exe")
    # 原始 exe 也放一份，供冒烟直接跑 target/release 的壳
    rel_res = SRC_TAURI / "target" / "release" / "resources"
    rel_res.mkdir(parents=True, exist_ok=True)
    shutil.copy(exe, rel_res / "llm-apig-daemon.exe")
    return RESOURCES / "llm-apig-daemon.exe"


def tauri_build() -> None:
    if not os.environ.get("TAURI_PRIVATE_KEY"):
        raise SystemExit(
            "缺少 TAURI_PRIVATE_KEY（.tauri/llm-apig.key 的内容）——"
            "updater 签名必需。set TAURI_PRIVATE_KEY=<内容> 后重跑")
    run(["cargo", "tauri", "build"], cwd=SRC_TAURI)


def _free_port_guard(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pid_alive(pid: int) -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True)
    return str(pid) in r.stdout


def _launch_shell(td: Path):
    exe = SRC_TAURI / "target" / "release" / "llm-apig.exe"
    env = {**os.environ, "LLMAPIG_DATA_DIR": str(td)}
    return subprocess.Popen([str(exe)], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL), exe


def _wait_runtime(td: Path, proc, timeout=60) -> dict:
    rj = td / "runtime.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"壳提前退出（exit={proc.returncode}）")
        if rj.exists():
            try:
                return json.loads(rj.read_text(encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(0.5)
    raise SystemExit(f"{timeout}s 未写出 runtime.json")


def _healthy(port: int) -> bool:
    import httpx
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health",
                         timeout=5).json() == {"status": "ok"}
    except Exception:
        return False


def _kill_hard(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                   capture_output=True)


def smoke() -> None:
    """三段：①健康 ②强杀壳→daemon 树消失（Job）③优雅停"""
    import httpx
    # ① 冷启动健康
    td1 = Path(tempfile.mkdtemp())
    p1, _ = _launch_shell(td1)
    meta = _wait_runtime(td1, p1)
    assert _healthy(meta["port"]), "冒烟①失败：健康检查未通过"
    # ② 强杀壳 → daemon 必须被 Job 连带清理
    _kill_hard(p1.pid)
    p1.wait(timeout=15)
    deadline = time.time() + 15
    while _pid_alive(meta["pid"]) and time.time() < deadline:
        time.sleep(0.5)
    assert not _pid_alive(meta["pid"]), "冒烟②失败：强杀壳后 daemon 未消失（Job Object 失效）"
    # ③ 优雅停：POST /shutdown 后 daemon 退出，壳还活着，最后强杀壳收尾
    td2 = Path(tempfile.mkdtemp())
    p2, _ = _launch_shell(td2)
    meta2 = _wait_runtime(td2, p2)
    assert _healthy(meta2["port"]), "冒烟③失败：二次启动健康检查未通过"
    httpx.post(f"http://127.0.0.1:{meta2['port']}/shutdown",
               headers={"X-Shutdown-Token": meta2["token"]}, timeout=5)
    deadline = time.time() + 15
    while _pid_alive(meta2["pid"]) and time.time() < deadline:
        time.sleep(0.5)
    assert not _pid_alive(meta2["pid"]), "冒烟③失败：优雅停后 daemon 未退出"
    _kill_hard(p2.pid)
    print("冒烟通过（健康/Job 强杀清理/优雅停机）")


def main() -> None:
    ver = version()
    print(f"=== 构建 llm-apig v{ver}（Tauri 壳） ===")
    run(["uv", "run", "pytest", "tests/", "-q"])
    pyinstaller_daemon()
    sync_tauri_version(ver)
    tauri_build()
    # NSIS 产物改名归档到 dist/
    nsis = list(BUNDLE.glob("nsis/*-setup.exe"))
    if nsis:
        dst = ROOT / "dist" / f"llm-apig-setup-{ver}.exe"
        shutil.copy(nsis[0], dst)
        print(f"安装包: {dst}")
    for lj in BUNDLE.glob("nsis/latest.json"):
        print(f"更新清单（上传到 Release）: {lj}")
    smoke()
    print("=== 构建全部完成 ===")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 完整构建**

Run: `set TAURI_PRIVATE_KEY=<.tauri/llm-apig.key 文件内容>` 然后 `uv run python desktop/build.py`
（bash 语法：`export TAURI_PRIVATE_KEY="$(cat .tauri/llm-apig.key)"`）
Expected: 依次打印 pytest/PyInstaller/cargo 输出，最后 `冒烟通过` + `构建全部完成`。首次 cargo release 编译约 10-20 分钟。

冒烟失败排查：①失败看 `td1/logs/desktop.log`（临时目录路径在异常栈里）；②失败=Job Object 未生效，检查 daemon.rs 的 `assign_kill_on_close` 是否被调用（desktop.log 里应有 `daemon assigned to kill-on-close job`）。

- [ ] **Step 4: Commit**

```bash
git add desktop/build.py pyproject.toml uv.lock
git rm tests/desktop/test_tray.py tests/desktop/test_server.py
git commit -m "build: Tauri 构建链（daemon PyInstaller + NSIS + 三段冒烟自检）"
```

---

### Task 7: 删除旧壳与旧更新链

**Files:**
- Delete: `desktop/main.py`、`desktop/tray.py`、`desktop/singleinstance.py`、`desktop/autostart.py`、`desktop/server.py`、`tests/desktop/test_autostart.py`、`tests/desktop/test_singleinstance.py`、`tests/desktop/test_tray.py`（若 Task 6 未删）、`tests/desktop/test_server.py`（若 Task 6 未删）、`llm-apig.iss`、`llm-apig.spec`、`version.json`
- Modify: `app/update_check.py`（删 get_update_info/version_gt/_cache）、`app/admin/routes.py`（删 /api/update、update_url 设置）、`app/admin/templates/settings.html`（删 update_url 行）、`tests/test_update_check.py`（重写）、`tests/test_admin_logs_settings.py`（如有 update_url 引用）、`README.md`、`start.bat`、新增 `start-desktop-dev.bat`

**Interfaces:**
- Consumes: Task 1-6 完成的新链路
- Produces: 仓库里只剩 Tauri 壳一条桌面路径

- [ ] **Step 1: 删旧壳文件**

```bash
git rm desktop/main.py desktop/tray.py desktop/singleinstance.py \
       desktop/autostart.py desktop/server.py \
       tests/desktop/test_autostart.py tests/desktop/test_singleinstance.py \
       llm-apig.iss llm-apig.spec version.json
git rm -f tests/desktop/test_tray.py tests/desktop/test_server.py 2>/dev/null || true
```

- [ ] **Step 2: 删旧更新链（Python 侧）**

`app/update_check.py` 整体替换为：

```python
"""版本号读取。更新检查已由 Tauri updater 承担（壳侧），本模块仅保留
current_version 供构建脚本与 daemon 的 runtime.json 使用。"""

import sys
import tomllib
from pathlib import Path


def current_version() -> str:
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "pyproject.toml"
    else:
        path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)["project"]["version"]
```

`app/admin/routes.py`：
- 删 `import app.update_check as update_check` 与 `from app.update_check import get_update_info`
- `_SETTING_KEYS` 删去 `"update_url"`（列表变为 `["cooldown_balance", "cooldown_ratelimit", "cooldown_auth", "capability_patterns"]` 之外保留其余原有项——执行时以现文件为准，仅移除 update_url）
- 删 `update_check._cache.clear()` 那行（settings POST 里）
- 删整个 `/api/update` 路由函数

`app/admin/templates/settings.html`：删除 update_url 的表单行（`<input name="update_url" ...>` 所在的完整 label/div 块，执行时看上下文结构）。

- [ ] **Step 3: 重写 test_update_check.py + 清引用**

```python
# tests/test_update_check.py
from app.update_check import current_version


def test_current_version_reads_pyproject():
    v = current_version()
    assert isinstance(v, str) and v.count(".") == 2
```

Run: `grep -rn "update_url\|get_update_info\|api/update\|version_gt" app/ tests/ desktop/ --include="*.py" --include="*.html"`
Expected: 无输出（或仅本文件历史注释）。`tests/test_admin_logs_settings.py` 若 grep 命中 update_url，删除对应断言/表单字段。

- [ ] **Step 4: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS。

- [ ] **Step 5: README 与启动脚本**

`README.md` 构建章节替换要点（沿用现有文风）：
- 构建：`set TAURI_PRIVATE_KEY=...` + `uv run python desktop/build.py`，产物 `dist/llm-apig-setup-<ver>.exe`
- 发布：NSIS 包与 bundle 里的 `latest.json` 一并上传 jihulab Release（tag=版本号）；latest.json 内 url 已指向同 Release 资产
- 私钥：`.tauri/llm-apig.key` 不入库，丢失=自动更新永久失效（需换公钥重发）
- 桌面开发：`start-desktop-dev.bat`（先起 uvicorn 再起壳）；从旧版升级需先卸载旧 Inno 安装（目录相同的可不卸，覆盖即可）

新增 `start-desktop-dev.bat`：

```bat
@echo off
rem 桌面壳开发：先起网关（dev 服务），再起 Tauri 壳（devUrl 指向它）
start "llm-apig dev server" cmd /c "uv run python -m app.main"
cd /d "%~dp0src-tauri" && cargo tauri dev
```

`start.bat` 保持不变（纯 Web 开发仍可用）。

- [ ] **Step 6: 最终验证 + Commit**

Run: `uv run pytest tests/ -q && cd src-tauri && cargo build`
Expected: 全绿。

```bash
git add -A
git commit -m "chore: 删除 pywebview 旧壳与 version.json 更新链，迁移完成"
```

---

## 人工验证清单（实现完成后走一遍，不写自动化）

- [ ] 安装 setup.exe（无 UAC 弹窗，装到 `%LOCALAPPDATA%\Programs\llm-apig`）
- [ ] 冷启动：splash 数秒 → admin 界面
- [ ] 关窗 → 托盘仍在、API 网关仍响应；托盘双击 → 窗口回来
- [ ] 托盘四菜单：打开主界面 / 开机自启勾选（重启 Windows 验证）/ 检查更新（无新版弹"已是最新"）/ 退出（进程全退、端口释放）
- [ ] 二次启动 exe → 不出第二窗口，主窗口前置
- [ ] 任务管理器强杀壳 → daemon 进程消失（与冒烟②一致）
- [ ] 横幅更新全流程：发一个高版本号到 Release → 旧版弹横幅 → 立即更新 → 下载 → 重启安装 → 版本号变为新版
- [ ] 卸载 → `%APPDATA%\llm-apig` 数据保留提示
