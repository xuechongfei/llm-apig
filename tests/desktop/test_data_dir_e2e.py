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