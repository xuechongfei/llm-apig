# 模型组支持同渠道多模型映射

**日期**: 2026-08-29
**状态**: 设计完成

## 背景

当前 `model_mapping` 表有 `UNIQUE (group_id, channel_id)` 约束，导致一个渠道在一个模型组里只能配置一条映射。如果用户尝试为同一渠道添加第二个模型，由于后端使用 `ON CONFLICT ... DO UPDATE` 的 upsert 语法，会静默覆盖原有记录，而不是新增一条。

**问题表现**：在模型组详情页，用户选择同一个渠道填入不同模型名后点击"保存映射"，原有的那条记录被编辑了，而不是新增一条。

## 目标

- 同一个渠道在同一个模型组下可以配置多个不同模型（不同 `actual_model`）
- 添加和编辑是两种明确的交互，不再混用 upsert
- 前端表单支持"添加模式"和"编辑模式"两种状态

## 设计

### 数据库变更

```sql
-- model_mapping 表：移除旧约束，新增三字段约束
-- 旧：UNIQUE (group_id, channel_id)
-- 新：UNIQUE (group_id, channel_id, actual_model)
```

执行方式：直接在代码中修改 `SCHEMA` 常量，SQLite 不支持 `ALTER TABLE DROP CONSTRAINT`，需要在应用启动时做迁移：

```sql
-- 迁移逻辑
-- 1. 创建新表 model_mapping_new（含新约束）
-- 2. INSERT INTO model_mapping_new SELECT * FROM model_mapping
-- 3. DROP TABLE model_mapping
-- 4. ALTER TABLE model_mapping_new RENAME TO model_mapping
```

### 后端路由变更

| 路由 | 变更 |
|------|------|
| `POST /admin/groups/{gid}/mappings` | 改为纯 `INSERT`（去掉 `ON CONFLICT`），冲突时返回错误提示 |
| `POST /admin/mappings/{mid}/edit` | **新增**，编辑已有映射的 `actual_model`、`priority`、`supports_image`、`supports_video` |
| `POST /admin/mappings/{mid}/delete` | 不变 |
| `POST /admin/mappings/{mid}/move` | 不变 |

### 前端变更（group_detail.html）

表格每行新增"编辑"按钮：

```
[↑] [↓] [编辑] [删除]
```

表单区域两种模式：

| 模式 | 标题 | 表单 action | 按钮 |
|------|------|------------|------|
| 添加 | "添加映射" | `/admin/groups/{gid}/mappings` | [保存映射] |
| 编辑 | "编辑映射" | `/admin/mappings/{mid}/edit` | [保存修改] [取消编辑] |

交互流程：
1. 点击某行的"编辑"按钮 → 该行数据回填到表单（渠道、模型名、优先级、图片/视频复选框）→ 标题变为"编辑映射" → 表单 action 指向编辑端点
2. 修改后点击"保存修改" → 提交编辑 → 重定向回详情页
3. 点击"取消编辑" → 清空表单，回到添加模式
4. "添加映射"模式保持不变，提交后新增一条记录

### 数据流

```
添加流程：
  用户填表 → POST /admin/groups/{gid}/mappings → INSERT → Redirect /admin/groups/{gid}

编辑流程：
  用户点击编辑 → 回填表单 → 修改 → POST /admin/mappings/{mid}/edit → UPDATE → Redirect /admin/groups/{gid}

删除流程（不变）：
  用户点击删除 → POST /admin/mappings/{mid}/delete → DELETE → Redirect /admin/groups/{gid}
```

## 涉及文件

| 文件 | 变更 |
|------|------|
| `app/db.py` | 修改 `SCHEMA` 中的 `model_mapping` 约束 + 添加迁移逻辑 |
| `app/admin/routes.py` | `mapping_upsert` → `mapping_create`（纯 INSERT），新增 `mapping_edit` |
| `app/admin/templates/group_detail.html` | 表格加编辑按钮，表单支持双模式 |

## 测试要点

- 同一渠道在同一模型组下添加两个不同模型，两条记录均存在
- 编辑已有映射，值正确更新
- 删除已有映射，记录消失
- 同渠道同模型名重复添加，应提示冲突错误
- 优先级排序（上移/下移）不受影响