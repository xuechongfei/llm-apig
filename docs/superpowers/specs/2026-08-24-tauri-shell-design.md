# llm-apig 桌面壳迁移 Tauri 设计

日期：2026-08-24
状态：已与用户逐节确认
前置：取代 `2026-08-24-desktop-app-design.md` 的壳层方案（app/ 网关本体、数据目录、
更新检查等决策不变）

## 背景与目标

现有 pywebview 桌面壳（desktop/main.py 等，PyInstaller 单进程打包）已可用，
但约 200 行手写 Win32 interop（单实例互斥锁、托盘、注册表自启、窗口前置）
需自行维护。参照 D:\AI\Tether\tether-desktop 已验证的 Tauri 2 sidecar 架构，
把壳层迁到 Rust/Tauri，换取：

- 官方插件替代手写平台代码（single-instance / autostart / tray-icon）
- 进程级隔离：壳崩溃 → Job Object 连带清理 daemon 树，无孤儿进程
- 与 tether-desktop 壳层技术统一，共享维护经验

约束不变：仅 Windows；目标用户不懂技术（分发机器零 Python 依赖）；
数据目录 `%APPDATA%\llm-apig\` 沿用；admin 前端零改动（无 JS 桥耦合）。

## 需求确认结果

- 旧 pywebview 壳彻底删除，Rust 壳成为唯一桌面形态
- 安装包改用 Tauri bundler 自带 NSIS（弃 Inno Setup）
- 日常开发 devUrl 直指开发态 uvicorn（127.0.0.1:8317/admin），不起 sidecar
- 停机：POST /shutdown 优雅停（3s 超时）+ kill 兜底
- 冷启动：splash 页 → daemon 就绪后跳转
- 托盘菜单严格对等现有：打开主界面 / 开机自启(勾选) / 检查更新 / 退出
- Python 侧新写 daemon 入口，不动 app/main.py 开发路径
- 冒烟自检保留并适配（含 Job Object 强杀验证）

## 架构

```
llm-apig.exe                      ← Tauri 2 Rust 壳 (src-tauri/)
 ├─ 单实例锁 (tauri-plugin-single-instance, 前置已有窗口)
 ├─ 托盘 (tray-icon feature)
 ├─ 开机自启 (tauri-plugin-autostart, HKCU Run)
 ├─ splash 窗口 → daemon 就绪后 eval 跳转 http://127.0.0.1:<port>/admin
 └─ daemon.rs: spawn llm-apig-daemon.exe
     ├─ 端口探测: 8317 起被占则 +1（壳侧 TCP connect 探测，孤儿 daemon 换端口
     │   不误杀，与 tether 同策略）
     ├─ 健康检查: 轮询 /health 200，超时 30s kill + splash 报错
     ├─ KILL_ON_JOB_CLOSE Job Object（壳死 → OS 连带杀 daemon 整棵树）
     ├─ CREATE_NO_WINDOW spawn（无黑窗）
     └─ stdout/stderr 转发 → logs/daemon.log / daemon.err.log

llm-apig-daemon.exe               ← PyInstaller onefile（新入口 desktop/daemon.py）
 └─ app/ FastAPI 网关本体原样
     ├─ LLMAPIG_DATA_DIR 环境变量（壳注入；daemon 侧缺失即报错退出，
     │   数据目录主权归壳）
     ├─ LLMAPIG_PORT 环境变量选端口（壳探测后注入）
     ├─ runtime.json: 端口 + 版本 + shutdown token（先写文件再起 uvicorn，
     │   健康检查仍是最终裁决）
     └─ POST /shutdown（127.0.0.1 only + token 校验）
```

### 目录布局

```
llm-apig/
 ├─ app/               # 网关本体不动（main.py 开发路径原样保留）
 ├─ desktop/
 │   ├─ daemon.py      # sidecar 进程入口（原 server.py 端口/健康逻辑移此，进程化）
 │   └─ build.py       # 一键构建：pytest → PyInstaller daemon → tauri build → 冒烟
 │   （main.py / tray.py / singleinstance.py / autostart.py / server.py 删除）
 ├─ src-tauri/         # Rust 壳
 │   ├─ src/lib.rs     # 装配：插件 + setup + 状态管理
 │   ├─ src/daemon.rs  # DaemonHandle（从 tether 适配：spawn/健康/Job/日志转发）
 │   ├─ src/tray.rs    # 托盘 + 菜单 + 关窗隐藏
 │   ├─ tauri.conf.json
 │   └─ icons/         # 由现有 PIL 图标脚本生成 .ico/.png
 └─ desktop-ui/        # splash 静态页（frontendDist 占位）
```

## daemon 生命周期

### 启动序列（壳）

1. Tauri setup → 建 splash 窗口（desktop-ui 静态页：启动中/失败两态）
2. 后台线程 DaemonHandle::spawn()：
   探测端口 → find llm-apig-daemon.exe（resources 下，回落
   `python -m desktop.daemon` dev 兜底）→ CREATE_NO_WINDOW spawn
   （注入 LLMAPIG_DATA_DIR/LLMAPIG_PORT）→ 挂 Job → 轮询 /health
3. 就绪 → 读 runtime.json 拿端口/token（壳以文件为准）→ eval 跳转 admin
4. 托盘异步创建（不阻塞）

### daemon 侧（desktop/daemon.py）

解析 LLMAPIG_DATA_DIR（缺失报错退出）→ init_db() → 选端口 →
生成 shutdown token → 写 runtime.json（先于 uvicorn）→ 主线程 uvicorn.run。
信号（SIGTERM/CTRL_BREAK）→ 优雅退出。

### 优雅停机

```
托盘"退出" → app.exit(0) → Rust Drop:
  ureq POST /shutdown?token=…（timeout 3s）
  → daemon: uvicorn.should_exit=True → 关 SQLite → 退出
  → 超时/失败 → child.kill()；OS 销毁 Job 清理整树
```

token 机制：daemon 启动生成一次性 token 写 runtime.json，壳读后作为
/shutdown 凭据（防本机网页 CSRF 打该端点——admin 页有 fetch 能力）。

### daemon 崩溃恢复

Rust 后台线程 wait() 监控：异常退出 → 托盘 tooltip 提示 + 自动重启一次
（简单退避）；二次失败停错误态，等用户手动处理。

### 更新检查

服务端逻辑不变（/admin/api/update）。托盘"检查更新"：Rust ureq 调同一
接口 + tauri-plugin-dialog 原生对话框展示结果（交互与现状一致）。

## 错误处理

| 场景 | 处理 |
|---|---|
| 端口被占 | 壳侧 +1 重试；daemon 绑定失败退出非 0 → 壳捕获"进程提前退出"→ splash 报错 |
| daemon 30s 未就绪 | kill + splash 错误详情 + 日志路径 |
| daemon 运行中崩溃 | 自动重启一次；二次失败停错误态 |
| WebView2 缺失 | NSIS 安装包自带 WebView2 引导下载 |
| 壳崩溃/强杀 | OS 销毁 Job → daemon 树清理；单实例锁由插件回收 |
| 优雅停机失败 | 3s 超时 → kill 兜底 |
| 重复拉起 | single-instance 插件挡第二壳实例；孤儿 daemon 由端口探测兜住 |

## 测试策略

- 现有 pytest 全保留（app/ 未动）
- daemon.py 新增测试：端口选择、runtime.json 写出、/shutdown token 校验
- Rust 壳不写单测（薄壳靠冒烟）
- 冒烟（build.py 内置）：启动壳 exe → 等 runtime.json → /health 200 →
  POST /shutdown → 断言进程树退出；**强杀壳 → 断言 daemon exe 消失**
  （验证 Job Object）
- 人工清单：托盘四菜单项、关窗最小化、自启勾选、单实例前置、
  安装/卸载/覆盖升级数据保留

## 构建与发布

`uv run python desktop/build.py` 一键：

1. pytest
2. PyInstaller 打 llm-apig-daemon.exe（onefile、--console、无窗口依赖，
   剔除 pywebview/pystray/Pillow）
3. 拷贝 daemon exe 到 src-tauri 资源位
4. cargo tauri build（NSIS；WebView2 引导内置）
5. version.json 更新（沿用）
6. 冒烟自检（上述）

产物：`src-tauri/target/release/bundle/nsis/llm-apig-setup-<ver>.exe`。
发布流程不变（传 jihulab Release + commit version.json）。

## 明确不做（YAGNI）

- 不做 Tauri 自动更新（仍 version.json + 手动下载引导）
- 不做 MSI（NSIS 足够）
- 不迁 admin 前端到 Tauri 前端工程（仍 FastAPI 模板渲染，devUrl 指过去）
- 不加 tether 式托盘增强（daemon 状态菜单项、单击切换显隐）
- 不做跨平台（仍仅 Windows）
