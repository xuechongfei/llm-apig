# 模型组同渠道多模型映射 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持同一渠道在模型组下配置多个不同模型，同时建立版本化数据库迁移机制。

**Architecture:** 在 `model_mapping` 表上将唯一约束从 `(group_id, channel_id)` 改为 `(group_id, channel_id, actual_model)`，通过 `MIGRATIONS` 列表实现版本化迁移；后端拆分为纯 INSERT 的 `mapping_create` 和新的 `mapping_edit` 端点；前端表单支持添加/编辑双模式。

**Tech Stack:** Python 3, FastAPI, SQLite, Jinja2, vanilla JavaScript

**涉及文件:**
- 修改: `app/db.py`
- 修改: `app/admin/routes.py`
- 修改: `app/admin/templates/group_detail.html`
- 修改: `tests/test_admin_groups.py`
- 修改: `tests/test_db.py`

## Global Constraints

- 唯一约束: `UNIQUE (group_id, channel_id, actual_model)`
- 编辑端点不修改 `channel_id` 和 `group_id`
- 冲突时返回错误提示（query param `?error=...`）
- 迁移机制：`settings` 表存 `schema_version`，只跑未执行过的迁移

---

### Task 1: 数据库迁移机制 + SCHEMA 更新

**Files:**
- Modify: `app/db.py`

**Interfaces:**
- Produces: `MIGRATIONS: list[str]` — 版本化迁移 SQL 列表
- Produces: `init_db()` 更新 — 启动时自动执行未跑的迁移

- [ ] **Step 1: 更新 SCHEMA 中的 model_mapping 约束**

将 `app/db.py` 第 37 行的 `UNIQUE (group_id, channel_id)` 改为 `UNIQUE (group_id, channel_id, actual_model)`：

```python
# 第 37 行，改为：
UNIQUE (group_id, channel_id, actual_model)
```

- [ ] **Step 2: 添加 MIGRATIONS 列表**

在 `app/db.py` 的 `SCHEMA` 之后、`connect()` 之前添加：

```python
MIGRATIONS = [
    # version 1 → 2: 模型组支持同渠道多模型
    """
    CREATE TABLE model_mapping_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL REFERENCES model_group(id) ON DELETE CASCADE,
        channel_id INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
        actual_model TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        supports_image INTEGER NOT NULL DEFAULT 0,
        supports_video INTEGER NOT NULL DEFAULT 0,
        UNIQUE (group_id, channel_id, actual_model)
    );
    INSERT INTO model_mapping_new SELECT * FROM model_mapping;
    DROP TABLE model_mapping;
    ALTER TABLE model_mapping_new RENAME TO model_mapping;
    """,
]
```

- [ ] **Step 3: 更新 init_db() 函数**

将 `app/db.py` 的 `init_db()` 函数（第 87-93 行）替换为：

```python
def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        cur_ver = int(get_setting(conn, "schema_version", "0"))
        for i, sql in enumerate(MIGRATIONS):
            ver = i + 1
            if ver <= cur_ver:
                continue
            conn.executescript(sql)
            set_setting(conn, "schema_version", str(ver))
    finally:
        conn.close()
```

- [ ] **Step 4: 验证迁移**

```bash
cd /d/AI/llm-apig && source .venv/Scripts/activate && python -c "
from app.db import init_db, connect, get_setting
init_db()
conn = connect()
ver = get_setting(conn, 'schema_version', '0')
print(f'schema_version={ver}')
# 验证新约束
info = conn.execute('PRAGMA index_list(model_mapping)').fetchall()
for idx in info:
    sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (idx['name'],)).fetchone()
    if sql: print(sql[0])
conn.close()
"
```

期望输出: `schema_version=1`，且唯一约束包含 `group_id, channel_id, actual_model`。

- [ ] **Step 5: 提交**

```bash
git add app/db.py
git commit -m "feat: 版本化数据库迁移 + model_mapping 约束改为三字段唯一"
```

---

### Task 2: 后端路由 — mapping_create + mapping_edit

**Files:**
- Modify: `app/admin/routes.py:228-248`

**Interfaces:**
- Consumes: `MIGRATIONS` 已生效，新约束 `UNIQUE (group_id, channel_id, actual_model)` 已就位
- Produces: `POST /admin/groups/{gid}/mappings` — 纯 INSERT，冲突时重定向带 `?error=`
- Produces: `POST /admin/mappings/{mid}/edit` — 编辑现有映射

- [ ] **Step 1: 添加 sqlite3 import**

在 `app/admin/routes.py` 顶部（第 1 行 `import time` 之后）添加：

```python
import sqlite3
```

- [ ] **Step 2: 替换 mapping_upsert 为 mapping_create**

将第 228-248 行的 `mapping_upsert` 函数替换为：

```python
@router.post("/groups/{gid}/mappings")
async def mapping_create(gid: int, channel_id: int = Form(...),
                         actual_model: str = Form(...),
                         priority: int = Form(100),
                         supports_image: str | None = Form(None),
                         supports_video: str | None = Form(None)):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO model_mapping (group_id,channel_id,actual_model,priority,"
            "supports_image,supports_video) VALUES (?,?,?,?,?,?)",
            (gid, channel_id, actual_model, priority,
             1 if supports_image else 0, 1 if supports_video else 0))
        conn.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(
            f"/admin/groups/{gid}?error=该渠道下已存在相同模型名", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{gid}", status_code=303)
```

- [ ] **Step 3: 新增 mapping_edit 端点**

在 `mapping_delete` 函数（第 251-261 行）之前添加：

```python
@router.post("/mappings/{mid}/edit")
async def mapping_edit(mid: int, actual_model: str = Form(...),
                       priority: int = Form(100),
                       supports_image: str | None = Form(None),
                       supports_video: str | None = Form(None)):
    conn = connect()
    try:
        conn.execute(
            "UPDATE model_mapping SET actual_model=?, priority=?,"
            " supports_image=?, supports_video=? WHERE id=?",
            (actual_model, priority,
             1 if supports_image else 0, 1 if supports_video else 0, mid))
        conn.commit()
        row = conn.execute("SELECT group_id FROM model_mapping WHERE id=?",
                           (mid,)).fetchone()
        gid = row["group_id"] if row else None
    finally:
        conn.close()
    if gid is None:
        return RedirectResponse("/admin/groups", status_code=303)
    return RedirectResponse(f"/admin/groups/{gid}", status_code=303)
```

- [ ] **Step 4: 验证路由**

```bash
cd /d/AI/llm-apig && source .venv/Scripts/activate && python -c "
from app.main import app
# 确认路由存在
routes = [r.path for r in app.routes if hasattr(r, 'path')]
assert '/admin/groups/{gid}/mappings' in routes
assert '/admin/mappings/{mid}/edit' in routes
print('Routes OK')
"
```

- [ ] **Step 5: 提交**

```bash
git add app/admin/routes.py
git commit -m "feat: mapping_create 纯 INSERT + mapping_edit 端点"
```

---

### Task 3: 前端 — group_detail.html 双模式表单

**Files:**
- Modify: `app/admin/templates/group_detail.html`

**Interfaces:**
- Consumes: `POST /admin/groups/{gid}/mappings`（添加）和 `POST /admin/mappings/{mid}/edit`（编辑）
- Consumes: 模板变量 `request.query_params.get('error', '')` 用于显示错误
- Produces: 双模式表单（添加/编辑），表格行编辑按钮

- [ ] **Step 1: 替换整个 group_detail.html**

将 `app/admin/templates/group_detail.html` 完整替换为：

```html
{% extends "base.html" %}
{% block nav_groups %}class="on"{% endblock %}
{% block content %}
<div class="crumb"><a href="/admin/groups">模型组</a> / {{ group["name"] }}</div>
<div class="page-head">
  <div>
    <h2 class="code">{{ group["name"] }}</h2>
    <p>客户端请求该模型名时，按下列顺序尝试渠道</p>
  </div>
</div>

{% set error = request.query_params.get('error', '') %}
{% if error %}
<div style="margin:0 0 16px;padding:10px 16px;background:var(--bad-bg);color:var(--bad);border-radius:var(--radius);font-size:13px">
  {{ error }}
</div>
{% endif %}

<div class="card table-scroll">
<table>
<thead><tr><th>渠道</th><th>协议</th><th>真实模型名</th><th>优先级</th>
<th>图片</th><th>视频</th><th>操作</th></tr></thead>
<tbody>
{% for m in mappings %}
<tr>
<td><b>{{ m["channel_name"] }}</b></td>
<td><span class="chip">{{ m["protocol"] }}</span></td>
<td class="code">{{ m["actual_model"] }}</td>
<td class="code">{{ m["priority"] }}</td>
<td>{{ "✓" if m["supports_image"] else "—" }}</td>
<td>{{ "✓" if m["supports_video"] else "—" }}</td>
<td>
  <div style="display:flex;gap:6px">
  <form class="inline" method="post" action="/admin/mappings/{{ m["id"] }}/move?dir=up"><button class="btn btn-sm" title="上移">↑</button></form>
  <form class="inline" method="post" action="/admin/mappings/{{ m["id"] }}/move?dir=down"><button class="btn btn-sm" title="下移">↓</button></form>
  <button class="btn btn-sm edit-btn"
          data-mid="{{ m["id"] }}"
          data-channel-id="{{ m["channel_id"] }}"
          data-actual-model="{{ m["actual_model"] }}"
          data-priority="{{ m["priority"] }}"
          data-supports-image="{{ m["supports_image"] }}"
          data-supports-video="{{ m["supports_video"] }}">编辑</button>
  <form class="inline" method="post" action="/admin/mappings/{{ m["id"] }}/delete"><button class="btn btn-sm btn-danger">删除</button></form>
  </div>
</td>
</tr>
{% else %}
<tr><td colspan="7" class="empty">该组还没有渠道映射，在下方添加。</td></tr>
{% endfor %}
</tbody>
</table>
</div>

<h3 id="form-title">添加映射</h3>
<div class="card form-card">
<form id="map-form" class="stack" method="post" action="/admin/groups/{{ group["id"] }}/mappings">
  <input type="hidden" id="edit-mid" name="edit_mid" value="">
  <div class="field">
    <label>渠道</label>
    <select name="channel_id" id="channel-select">
    {% for c in channels %}
    <option value="{{ c["id"] }}">{{ c["name"] }}（{{ c["protocol"] }}）</option>
    {% endfor %}
    </select>
  </div>
  <div class="field">
    <label>真实模型名</label>
    <input name="actual_model" id="actual-model" required class="code" placeholder="如 claude-sonnet-4-5 / deepseek-chat">
  </div>
  <div class="field">
    <label>优先级 <span class="hint">越小越优先</span></label>
    <input name="priority" id="priority" type="number" value="100" style="max-width:140px">
  </div>
  <div style="display:flex;gap:22px">
    <label class="check"><input type="checkbox" name="supports_image" id="supports-image"> 支持图片输入</label>
    <label class="check"><input type="checkbox" name="supports_video" id="supports-video"> 支持视频输入</label>
  </div>
  <div class="form-actions" style="display:flex;gap:10px">
    <button class="btn btn-primary" type="submit" id="submit-btn">保存映射</button>
    <button class="btn" type="button" id="cancel-btn" style="display:none" onclick="cancelEdit()">取消编辑</button>
  </div>
</form>
</div>

<script>
(function(){
  var form = document.getElementById('map-form');
  var title = document.getElementById('form-title');
  var submitBtn = document.getElementById('submit-btn');
  var cancelBtn = document.getElementById('cancel-btn');
  var editMid = document.getElementById('edit-mid');
  var channelSelect = document.getElementById('channel-select');
  var actualModel = document.getElementById('actual-model');
  var priority = document.getElementById('priority');
  var supportsImage = document.getElementById('supports-image');
  var supportsVideo = document.getElementById('supports-video');

  var btns = document.querySelectorAll('.edit-btn');
  btns.forEach(function(btn){
    btn.addEventListener('click', function(){
      editMid.value = this.dataset.mid;
      channelSelect.value = this.dataset.channelId;
      channelSelect.disabled = true;
      actualModel.value = this.dataset.actualModel;
      priority.value = this.dataset.priority;
      supportsImage.checked = this.dataset.supportsImage === '1';
      supportsVideo.checked = this.dataset.supportsVideo === '1';
      form.action = '/admin/mappings/' + this.dataset.mid + '/edit';
      title.textContent = '编辑映射';
      submitBtn.textContent = '保存修改';
      cancelBtn.style.display = '';
      title.scrollIntoView({behavior:'smooth'});
    });
  });

  window.cancelEdit = function(){
    editMid.value = '';
    channelSelect.disabled = false;
    channelSelect.selectedIndex = 0;
    actualModel.value = '';
    priority.value = '100';
    supportsImage.checked = false;
    supportsVideo.checked = false;
    form.action = '/admin/groups/{{ group["id"] }}/mappings';
    title.textContent = '添加映射';
    submitBtn.textContent = '保存映射';
    cancelBtn.style.display = 'none';
  };
})();
</script>
{% endblock %}
```

- [ ] **Step 2: 验证前端渲染**

```bash
cd /d/AI/llm-apig && source .venv/Scripts/activate && python -c "
from app.main import app
from fastapi.testclient import TestClient
client = TestClient(app)
# 先创建测试数据
import app.db as db
import tempfile, os
tmp = tempfile.mkdtemp()
db.DB_PATH = os.path.join(tmp, 't.db')
db.init_db()
conn = db.connect()
conn.execute(\"INSERT INTO channel (id,name,protocol,base_url) VALUES (1,'test','anthropic','http://a')\")
conn.execute(\"INSERT INTO model_group (id,name) VALUES (1,'g')\")
conn.execute(\"INSERT INTO model_mapping (id,group_id,channel_id,actual_model) VALUES (1,1,1,'m1')\")
conn.commit(); conn.close()
resp = client.get('/admin/groups/1')
assert resp.status_code == 200
assert '编辑' in resp.text
assert '添加映射' in resp.text
assert 'edit-btn' in resp.text
print('Template OK')
"
```

- [ ] **Step 3: 提交**

```bash
git add app/admin/templates/group_detail.html
git commit -m "feat: 模型组详情页支持编辑映射 + 双模式表单"
```

---

### Task 4: 测试

**Files:**
- Modify: `tests/test_db.py`
- Modify: `tests/test_admin_groups.py`

**Interfaces:**
- Consumes: 所有上述变更

- [ ] **Step 1: 添加迁移测试到 test_db.py**

在 `tests/test_db.py` 末尾追加：

```python
def test_migration_preserves_data(tmp_path, monkeypatch):
    """模拟旧库 → 新库迁移，数据不丢失"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    # 模拟旧 schema：先手动建表（含旧约束）
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channel (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        protocol TEXT NOT NULL,
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
    """)
    conn.execute("INSERT INTO channel (id,name,protocol,base_url) VALUES (1,'a','anthropic','http://a')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping (id,group_id,channel_id,actual_model,priority) VALUES (1,1,1,'m1',10)")
    conn.commit()
    # 不设置 schema_version，模拟旧库
    conn.close()

    # 执行迁移
    db.init_db()

    conn = db.connect()
    # 数据保留
    m = conn.execute("SELECT * FROM model_mapping WHERE id=1").fetchone()
    assert m is not None
    assert m["actual_model"] == "m1"
    assert m["priority"] == 10
    # schema_version 已更新
    ver = db.get_setting(conn, "schema_version", "0")
    assert ver == str(len(db.MIGRATIONS))
    # 新约束生效：同渠道同模型重复插入报错
    import sqlite3
    try:
        conn.execute("INSERT INTO model_mapping (group_id,channel_id,actual_model) VALUES (1,1,'m1')")
        conn.commit()
        assert False, "should raise IntegrityError"
    except sqlite3.IntegrityError:
        pass
    conn.close()
```

- [ ] **Step 2: 添加多模型测试到 test_admin_groups.py**

在 `tests/test_admin_groups.py` 末尾追加：

```python
async def test_same_channel_multi_model(tmp_path, monkeypatch):
    """同一渠道在同一模型组下可以添加多个不同模型"""
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/groups", data={"name": "multi"})
        assert r.status_code == 303
        gid = db.connect().execute(
            "SELECT id FROM model_group WHERE name='multi'").fetchone()["id"]
        # 添加第一个模型
        r1 = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "model-a", "priority": "10"})
        assert r1.status_code == 303
        # 添加第二个模型（同渠道，不同模型名）
        r2 = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "model-b", "priority": "20"})
        assert r2.status_code == 303
        conn = db.connect()
        rows = conn.execute(
            "SELECT * FROM model_mapping WHERE group_id=? ORDER BY priority",
            (gid,)).fetchall()
        assert len(rows) == 2
        assert rows[0]["actual_model"] == "model-a"
        assert rows[1]["actual_model"] == "model-b"
        conn.close()


async def test_duplicate_model_rejected(tmp_path, monkeypatch):
    """同渠道同模型名重复添加应被拒绝"""
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/groups", data={"name": "dup"})
        assert r.status_code == 303
        gid = db.connect().execute(
            "SELECT id FROM model_group WHERE name='dup'").fetchone()["id"]
        # 第一次添加
        await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "same-model", "priority": "10"})
        # 第二次添加相同组合
        r2 = await c.post(f"/admin/groups/{gid}/mappings", data={
            "channel_id": "1", "actual_model": "same-model", "priority": "20"})
        assert r2.status_code == 303
        assert "error=" in r2.headers["location"]
        conn = db.connect()
        rows = conn.execute(
            "SELECT * FROM model_mapping WHERE group_id=?", (gid,)).fetchall()
        assert len(rows) == 1  # 只有一条
        conn.close()


async def test_mapping_edit(tmp_path, monkeypatch):
    """编辑映射的模型名和优先级"""
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'g')")
    conn.execute("INSERT INTO model_mapping (id,group_id,channel_id,actual_model,priority) VALUES (1,1,1,'old',10)")
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/mappings/1/edit", data={
            "actual_model": "new-model", "priority": "50",
            "supports_image": "on"})
        assert r.status_code == 303
    conn = db.connect()
    m = conn.execute("SELECT * FROM model_mapping WHERE id=1").fetchone()
    assert m["actual_model"] == "new-model"
    assert m["priority"] == 50
    assert m["supports_image"] == 1
    conn.close()
```

- [ ] **Step 3: 运行全部测试**

```bash
cd /d/AI/llm-apig && source .venv/Scripts/activate && python -m pytest tests/test_db.py tests/test_admin_groups.py -v
```

期望: 全部 PASS（原有测试 + 新增 4 个测试）。

- [ ] **Step 4: 提交**

```bash
git add tests/test_db.py tests/test_admin_groups.py
git commit -m "test: 迁移 + 多模型 + 编辑映射测试"
```

---