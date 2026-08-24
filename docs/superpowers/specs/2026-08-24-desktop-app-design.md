# llm-apig 桌面端应用设计

日期：2026-08-24
状态：已与用户逐节确认

## 背景与目标

llm-apig 是本机部署的 LLM API 网关（Python 3.13 + FastAPI + uvicorn + SQLite，
Jinja2 管理界面，监听 127.0.0.1:8317）。现需打包为可分发的 Windows 桌面应用，
目标用户是**完全不懂技术的普通用户**。

需求确认结果：

- 平台：仅 Windows
- 界面：内嵌桌面窗口（pywebview + 系统 WebView2），非浏览器
- 数据：存 `%APPDATA%\llm-apig\`，卸载/升级保留
- 托盘：关窗口最小化到托盘，服务继续；托盘菜单可退出
- 开机自启：默认关，托盘菜单/设置可开（HKCU Run 键，免 UAC）
- 分发：Inno Setup 安装向导 setup.exe
- 更新：启动时后台检查 jihulab.com 上的 version.json，有新版弹横幅引导
  到 Release 页手动下载（无代码签名证书，SmartScreen 警告在 README 说明）

## 方案选型

选定 **pywebview 壳 + PyInstaller + Inno Setup**（备选 PySide6 因体积大一倍被否决）：

- pywebview（约 1MB）在 Windows 上用系统自带 WebView2 运行时（Win10/11 预装），
  安装包可控制在 25-35MB
- 托盘用 pystray + Pillow，自启用注册表 Run 键，均为小代码量
- Electron 被否决：引入 Node 技术栈，维护成本不成比例

## 架构

```
llm-apig.exe (PyInstaller 打包)
 │
 ├─ desktop/main.py 启动流程
 │   1. 单实例锁（命名互斥锁；已运行则前置已有窗口并退出）
 │   2. 解析数据目录（LLMAPIG_DATA_DIR）
 │   3. 子线程启动 uvicorn 跑 FastAPI app（127.0.0.1，端口 8317 起，
 │      被占用则 +1 重试，最多 5 个；实际端口写 runtime.json）
 │   4. 轮询 /health（最多 10s）
 │   5. 打开 pywebview 窗口 → http://127.0.0.1:<port>/admin
 │   6. 后台延迟 3s 检查更新，失败静默
 │
 ├─ 托盘（pystray）：打开主界面 / 开机自启(勾选) / 检查更新 / 退出
 │   关窗口 → 隐藏，服务继续；托盘退出 → 停服务、释放锁、退出
 │
 └─ 网关本体（app/ 现有代码，仅两处小改）
```

### 对现有代码的改动（仅两处）

1. `app/db.py`：数据目录解析——环境变量 `LLMAPIG_DATA_DIR` 优先，
   缺省为项目 `data/`（开发模式与现有测试零影响）
2. `app/main.py`：`webbrowser.open` 仅在开发模式（无 `LLMAPIG_DATA_DIR`）时触发

## 数据目录与配置

```
%APPDATA%\llm-apig\
 ├─ gateway.db        # SQLite（现有库直接沿用）
 ├─ runtime.json      # 实际监听端口、版本号（自动生成）
 └─ logs\app.log      # 桌面壳日志
```

- 桌面级设置（更新检查开关等）复用 SQLite `settings` 表，单一数据源，
  不引入新配置文件
- Inno Setup 卸载时询问是否保留 `%APPDATA%` 数据；升级覆盖安装不碰数据
- SQLite schema 沿用 `CREATE TABLE IF NOT EXISTS`，暂不需要迁移框架

## 托盘、开机自启、单实例

- **托盘**：pystray；双击图标 = 打开主界面；退出时先停 uvicorn 线程再退进程
  （避免 SQLite WAL 残留锁）
- **自启**：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，键名
  `llm-apig`，值为 exe 完整路径。装到 `%LOCALAPPDATA%\Programs\llm-apig`，
  全程免 UAC
- **单实例**：Windows 命名互斥锁 `llm-apig-singleton`；已存在时 FindWindow
  按窗口标题定位 + SetForegroundWindow 前置，第二进程静默退出；
  强杀时互斥锁由 OS 回收

## 更新检查与发布

- 仓库根 `version.json`：`{version, notes, url}`，jihulab raw 地址被检查
- 版本号语义化比较（如 1.0.1 > 1.0.0），构建时从 pyproject.toml 注入
- 有新版：窗口顶部横幅「新版本可用 → 查看下载」（打开 jihulab Release 页）；
  托盘「检查更新」手动触发时弹窗
- 发布命令 `uv run python desktop/build.py`：
  pytest → PyInstaller → ISCC 编译 → 更新 version.json
  人工步骤：上传 setup.exe 到 jihulab Release、commit version.json
- 无代码签名：首次安装触发 SmartScreen 警告，README 提供操作指引

## 文件结构

```
desktop/
 ├─ main.py           # 入口：锁→服务→窗口→托盘→更新
 ├─ server.py         # uvicorn 线程封装：启动/健康等待/优雅停止
 ├─ tray.py           # pystray 托盘
 ├─ updater.py        # 版本比较 + version.json 检查
 ├─ autostart.py      # HKCU Run 键读写
 ├─ singleinstance.py # 命名互斥锁 + 窗口前置
 ├─ paths.py          # 数据目录、日志目录解析
 └─ build.py          # 一键构建
llm-apig.iss          # Inno Setup 脚本
version.json          # 版本信息
```

新增依赖：`pywebview`、`pystray`、`Pillow`；dev 组加 `pyinstaller`。

## 错误处理

| 场景 | 处理 |
|---|---|
| 端口被占 | +1 重试 5 次；仍失败弹原生 MessageBox 报错退出 |
| WebView2 缺失 | 引导打开微软官方安装页后退出 |
| 服务启动超时 10s | 弹错误框（附 app.log 路径）退出 |
| 更新检查失败 | 静默 |
| 崩溃/强杀 | 互斥锁 OS 回收；SQLite WAL 保证库不损坏 |

## 测试策略

- desktop/ 纯逻辑模块（version_gt、autostart 读写、paths 解析）用 pytest 直接测
- 互斥锁、pywebview、托盘等 GUI/Win32 部分人工验证，不写自动化
- 现有 tests/ 全部继续通过
- 构建脚本内置冒烟：打包后启动 exe → 轮询 /health → 自动退出
