# 模型组最大尝试次数配置化设计

日期：2026-09-05
状态：已批准

## 背景

网关执行请求时，对模型组内候选渠道的故障转移次数写死为 `MAX_ATTEMPTS = 5`（`app/executor.py:14`），执行循环取 `candidates[:MAX_ATTEMPTS]`（`app/executor.py:147`）。用户希望该上限可在运行时调整。

项目已有 settings 体系：SQLite `settings` 键值表 + `app/db.py` 的 `get_setting`/`set_setting` + 管理端 `/admin/settings` 设置页（`app/admin/routes.py` 的 `_SETTING_KEYS` 白名单驱动保存，`app/admin/templates/settings.html` 渲染表单）。冷却时间（`cooldown_balance` 等）、错误特征库（`balance_patterns`、`capability_patterns`）均走此体系。

## 需求

- 把最大尝试次数做成**全局**配置项（用户已确认作用域为全局，非每组）。
- 默认值为 5，与现状行为一致。
- 通过管理端设置页查看和修改，保存后即时生效。

## 方案

复用现有 settings 体系（对比过的备选：环境变量/配置文件——背离项目单一配置源现状，弃用）。

## 设计

### 数据与存储

- 新键：`max_attempts`，存 `settings` 表。
- 无表结构变更、无迁移（表为自由键值）。
- 未配置时读取方回退默认值 `"5"`。

### 读取逻辑（app/executor.py）

- 删除模块级常量 `MAX_ATTEMPTS`。
- 新增辅助函数 `_max_attempts(conn) -> int`，模式照搬现有 `_cooldown_seconds`：

  ```python
  def _max_attempts(conn) -> int:
      try:
          n = int(get_setting(conn, "max_attempts", "5"))
          return n if n >= 1 else 5
      except ValueError:
          return 5
  ```

- `execute()` 中 `candidates[:MAX_ATTEMPTS]` 改为 `candidates[:_max_attempts(conn)]`。
- 每次请求读一次 DB，与 cooldown 设置同样的开销与实时生效语义，不加缓存。

### 非法值处理

- 非数字、空值、`ValueError` → 回退 5。
- 数值 < 1 → 回退 5。
- 不设上限钳制：候选数量本身有限，用户配大值即代表其真实意图。

### 管理界面

- `app/admin/routes.py`：`_SETTING_KEYS` 列表加入 `"max_attempts"`；保存逻辑（白名单驱动）无需改动。
- `app/admin/templates/settings.html`：在"冷却时间"卡片之后新增"失败重试"卡片，含一个数字输入框：

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

  输入框 `min="1"` 仅为前端提示，真实兜底以服务端 `_max_attempts` 为准。

## 测试

- `tests/test_executor.py`：
  - 设置 `max_attempts=2`、组内 3 个候选渠道全部失败，断言注入的 mock transport 实际收到的上游请求数为 2，且该请求的 attempt 记录为 2 条（不含 skipped 记录）。
  - 设置非法值（如 `"abc"`），断言回退为 5 次上限的行为。
- `tests/test_admin_logs_settings.py`：现有 `test_settings_page` 的 POST 表单数据中加入 `max_attempts`，断言保存成功且设置页回显该值。

## 影响面

- 修改文件：`app/executor.py`、`app/admin/routes.py`、`app/admin/templates/settings.html`、`tests/test_executor.py`、`tests/test_admin_logs_settings.py`。
- 无 schema 变更、无 API 变更、无桌面端（Tauri）改动。
- 默认行为与现状完全一致（5 次）。
