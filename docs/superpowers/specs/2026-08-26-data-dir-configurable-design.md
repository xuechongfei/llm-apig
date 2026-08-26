# 桌面端数据目录可配置

**日期**: 2026-08-26
**状态**: 设计完成

## 背景

当前桌面端应用的运行数据（`gateway.db`、日志、`runtime.json`）固定保存在 `%APPDATA%\llm-apig\`。用户希望能够通过管理界面自行配置数据保存路径。

## 目标

在设置页面增加"数据目录"配置项，允许用户修改数据存储路径，修改后自动迁移现有数据到新目录并清理旧目录，最后重启 daemon 生效。

## 核心挑战：引导配置

数据目录决定了 `gateway.db` 的位置，而所有设置（包括用户配置的数据目录）都存在 `gateway.db` 的 `settings` 表中。这形成了循环依赖——要读取用户配置的路径就得先打开 DB，要打开 DB 就得先知道路径。

**解决方案**：引入一个固定位置的引导配置文件 `%APPDATA%\llm-apig\config.json`，该文件：
- 始终在默认位置，不随数据目录迁移
- 内容极简，仅含 `data_dir` 字段
- 不存在或字段为空时，行为与现在完全一致（默认 `%APPDATA%\llm-apig\`）

## 架构

### 启动流程

```
Rust 壳 (daemon.rs)
  → 读取 %APPDATA%\llm-apig\config.json
  → 有 data_dir 字段 → 使用自定义路径
  → 无 / 文件不存在 → 使用默认 %APPDATA%\llm-apig\
  → 设置 LLMAPIG_DATA_DIR 环境变量
  → 拉起 Python daemon
  → daemon 从环境变量获取路径，定位 gateway.db 和日志
```

### 修改路径流程

```
用户在设置页面输入新路径
  → POST /admin/settings/data-dir
  → 后端校验：路径合法、可写、空间足够
  → 关闭当前 DB 连接
  → 复制 gateway.db 到新目录
  → 写入 %APPDATA%\llm-apig\config.json
  → 删除旧数据目录
  → 返回成功，前端显示"重启生效"按钮
  → 用户点击"重启 daemon"
  → POST /admin/settings/restart
  → 后端写入 .restart 标记文件到 %APPDATA%\llm-apig\
  → 后端延迟 0.2s 触发 uvicorn 退出（返回响应后再退出）
  → Rust 壳的崩溃监控线程检测到 daemon 退出
  → 检查 .restart 标记文件存在 → 删除标记，重启 daemon（绕过重启预算）
  → 新 daemon 从 config.json 读取新路径，使用新数据目录
  → 壳将 webview 重定向到新端口
```

### daemon 重启机制（Rust 侧）

在 `spawn_crash_watcher` 中增加对 `.restart` 标记文件的检查：
- daemon 退出后，检查 `%APPDATA%\llm-apig\.restart` 是否存在
- 存在 → 删除标记文件，调用 `DaemonHandle::spawn()` 重启 daemon（不受 `restart_budget` 限制）
- 不存在 → 维持现有逻辑（正常退出不重启，崩溃重启一次）

### 组件关系

```
config.json (固定位置: %APPDATA%\llm-apig\)
  ├── [Rust] daemon.rs::data_dir() —— 读取并设置 LLMAPIG_DATA_DIR
  ├── [Python] desktop/paths.py::data_dir() —— 读取环境变量
  ├── [Python] desktop/config.py (新增) —— 读写 config.json
  └── [Python] app/db.py::_db_path() —— 读取环境变量
```

## 需要修改的文件

### 1. 新增 `desktop/config.py`

引导配置的读写逻辑，封装对 `config.json` 的 CRUD：
- `load_config() -> dict`：读取 config.json，返回 dict（文件不存在返回空 dict）
- `save_config(data: dict) -> None`：写入 config.json
- `get_data_dir() -> str | None`：返回用户配置的自定义路径，无则返回 None
- `set_data_dir(path: str) -> None`：写入自定义路径

### 2. 修改 `desktop/paths.py`

`data_dir()` 函数增加从 config.json 读取的逻辑：
- 环境变量 `LLMAPIG_DATA_DIR` 优先（壳注入）
- 其次读取 `config.json` 中的 `data_dir`
- 最后回退到 `%APPDATA%\llm-apig`

### 3. 修改 `src-tauri/src/daemon.rs`

`data_dir()` 函数增加从 config.json 读取的逻辑：
- 环境变量 `LLMAPIG_DATA_DIR` 优先
- 其次读取 `%APPDATA%\llm-apig\config.json` 中的 `data_dir`
- 最后回退到 `%APPDATA%\llm-apig`

### 4. 修改 `desktop/daemon.py`

新增 `attach_restart_handler` 函数（与 `attach_shutdown` 相同模式，需要 `server` 引用）：
- `POST /admin/settings/restart`：写入 `.restart` 标记文件到 `%APPDATA%\llm-apig\.restart`，延迟 0.2s 触发 `server.should_exit = True`

daemon 启动时清理残留的 `.restart` 标记文件。

### 5. 修改 `app/admin/routes.py`

新增一个接口 + 修改 settings 页面数据：

**`POST /admin/settings/data-dir`**
- 入参：`data_dir`（字符串，新路径）
- 校验：路径合法、所在磁盘空间 > DB 文件大小 × 1.5、可创建目录并可写
- 迁移：关闭当前 DB 连接 → 复制 `gateway.db` 到新目录 → 写入 `config.json` → 删除旧目录
- 返回：`{"ok": true, "data_dir": "/new/path"}` 或 `{"ok": false, "detail": "错误原因"}`

**`POST /admin/settings/restart`** — 实现在 `desktop/daemon.py` 中（需要 `server` 引用），见上节

### 6. 修改 `app/admin/templates/settings.html`

在设置页面新增"数据目录"卡片：
- 显示当前数据目录路径（只读）
- 文本输入框供用户输入新路径
- "修改并迁移"按钮，点击后发 POST 到 `/admin/settings/data-dir`
- 迁移成功后显示"重启生效"按钮，点击后发 POST 到 `/admin/settings/restart`
- 操作结果（成功/失败）用提示信息展示

### 7. 修改 `src-tauri/src/daemon.rs` — 崩溃监控

`spawn_crash_watcher` 函数增加对 `.restart` 标记文件的检查：
- 在 daemon 退出后（无论退出码），检查 `data_dir().join(".restart")` 是否存在
- 存在 → 删除该文件，调用 `DaemonHandle::spawn()` 重启，不受 `restart_budget` 限制
- 不存在 → 维持现有逻辑

## 边界情况处理

| 场景 | 处理方式 |
|------|----------|
| 新路径不存在 | 自动创建目录树，验证可写 |
| 新路径权限不足 | 捕获异常，返回明确错误提示 |
| 磁盘空间不足 | 迁移前检查目标磁盘剩余空间 > DB 文件大小 * 1.5，不足则拒绝 |
| 新路径与旧路径相同 | 无操作，直接返回成功 |
| config.json 损坏（非法 JSON） | 记录警告日志，回退到默认路径 |
| 迁移过程中断电/崩溃 | 先复制到临时目录，复制完成后再移动到目标，最后删旧目录（原子性尽可能） |
| 旧目录删除失败 | 不影响新路径使用，日志警告，旧目录留待用户手动清理 |
| 并发修改 | 迁移期间锁定设置操作，返回"系统繁忙" |
| 残留 `.restart` 标记 | daemon 启动时清理 `.restart` 标记文件（上次重启可能未完成），避免误触发重启 |
| 迁移后 DB 连接 | 迁移前关闭当前 DB 连接，迁移后新 daemon 实例会在新路径重新打开 DB |

## 不做的事情

- 不支持多数据目录切换（完整版）
- 不提供目录选择器 GUI（浏览器安全限制，仅文本输入 + 路径校验提示）
- 不迁移历史日志文件（仅迁移 gateway.db 和最近的日志）