import socket
import time

import httpx
import pytest

import app.db as db
from app.main import app
from desktop.server import GatewayServer, ServerError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _init(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


def test_start_health_stop(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    port = _free_port()
    srv = GatewayServer(app, ports=(port,))
    assert srv.start() == port
    r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
    assert r.json() == {"status": "ok"}
    srv.stop()
    time.sleep(0.5)
    # Windows: 连接已关闭的本地端口收到 SYN 重传（无 RST），表现为 ConnectTimeout；
    # Linux: 立即 ECONNREFUSED → ConnectError。两者都表示服务已停止。
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)


def test_port_fallback(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    busy = _free_port()
    hold = socket.socket()
    hold.bind(("127.0.0.1", busy))
    hold.listen(1)
    try:
        free = _free_port()
        srv = GatewayServer(app, ports=(busy, free))
        assert srv.start() == free
        srv.stop()
    finally:
        hold.close()


def test_all_ports_busy_raises(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    busy = _free_port()
    hold = socket.socket()
    hold.bind(("127.0.0.1", busy))
    hold.listen(1)
    try:
        with pytest.raises(ServerError):
            GatewayServer(app, ports=(busy,)).start()
    finally:
        hold.close()
