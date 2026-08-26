import httpx

import app.db as db
from app.errors import ErrorCategory, classify_error
from app.main import app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


def test_classify_with_custom_patterns():
    v = classify_error(400, "xxx 账户已冻结 xxx",
                       balance_patterns=["账户已冻结"])
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE
    # 默认 patterns 不命中 → 仍是 client error
    assert classify_error(400, "xxx 账户已冻结 xxx").category == ErrorCategory.CLIENT


async def test_logs_pages(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO request_log (created_at,client_protocol,group_name,"
                 "path,stream,status) VALUES (1.0,'anthropic','g','/v1/messages',0,"
                 "'failed')")
    conn.commit()
    log_id = conn.execute("SELECT id FROM request_log").fetchone()["id"]
    conn.execute("INSERT INTO attempt (log_id,created_at,channel_name,http_status,"
                 "error_summary) VALUES (?,1.0,'c1',402,'no money')", (log_id,))
    conn.commit(); conn.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/admin/logs?status=failed")
        assert r.status_code == 200 and "402" not in r.text  # 列表不含 attempt
        assert "'g'" in r.text or ">g<" in r.text
        r = await c.get(f"/admin/logs/{log_id}")
        assert r.status_code == 200 and "no money" in r.text


async def test_settings_page(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/admin/settings", data={
            "cooldown_balance": "300", "cooldown_ratelimit": "45",
            "cooldown_auth": "1800", "cooldown_server": "60",
            "api_key": "k", "balance_patterns": "余额不足\nquota",
            "capability_patterns": "not support image"})
        assert r.status_code == 303
        conn = db.connect()
        assert db.get_setting(conn, "cooldown_balance", "") == "300"
        assert "quota" in db.get_setting(conn, "balance_patterns", "")
        conn.close()


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


