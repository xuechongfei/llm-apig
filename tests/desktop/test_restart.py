from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db


def test_restart_endpoint_writes_marker(tmp_path, monkeypatch):
    """POST /admin/settings/restart 写入 .restart 标记并返回 ok。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "gateway.db")
    db.init_db()

    from desktop.daemon import attach_restart_handler

    app = FastAPI()

    # 模拟 uvicorn Server（仅需 should_exit 属性）
    class FakeServer:
        should_exit = False

    server = FakeServer()
    attach_restart_handler(app, server)

    client = TestClient(app)
    resp = client.post("/admin/settings/restart")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "重启" in data["message"]

    # 验证 .restart 标记文件已创建
    marker = tmp_path / "llm-apig" / ".restart"
    assert marker.exists()


def test_restart_triggers_server_exit(tmp_path, monkeypatch):
    """重启端点应设置 server.should_exit = True（由定时器延迟触发）。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "gateway.db")
    db.init_db()

    import time
    from desktop.daemon import attach_restart_handler

    app = FastAPI()

    class FakeServer:
        should_exit = False

    server = FakeServer()
    attach_restart_handler(app, server)

    client = TestClient(app)
    client.post("/admin/settings/restart")
    # 立即检查：should_exit 应为 False（0.2s 延迟）
    assert server.should_exit is False
    # 轮询等待 should_exit 变为 True
    deadline = time.time() + 2
    while not server.should_exit and time.time() < deadline:
        time.sleep(0.05)
    assert server.should_exit is True