import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from desktop import daemon


def test_pick_port_env_valid():
    assert daemon.pick_port("9000") == 9000


def test_pick_port_env_invalid_falls_back():
    assert daemon.pick_port("not-a-port") == 58317
    assert daemon.pick_port(None) == 58317
    assert daemon.pick_port("") == 58317


def test_runtime_payload_shape():
    p = daemon.runtime_payload(58317, "tok")
    assert p["port"] == 58317
    assert p["token"] == "tok"
    assert p["pid"] == os.getpid()
    assert isinstance(p["version"], str) and p["version"]


class _StubServer:
    def __init__(self):
        self.should_exit = False


def test_shutdown_wrong_token_403():
    app = FastAPI()
    daemon.attach_shutdown(app, "secret", _StubServer())
    r = TestClient(app).post("/shutdown",
                             headers={"X-Shutdown-Token": "wrong"})
    assert r.status_code == 403


def test_shutdown_ok_sets_flag():
    srv = _StubServer()
    app = FastAPI()
    daemon.attach_shutdown(app, "secret", srv)
    r = TestClient(app).post("/shutdown",
                             headers={"X-Shutdown-Token": "secret"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    deadline = time.time() + 2
    while not srv.should_exit and time.time() < deadline:
        time.sleep(0.05)
    assert srv.should_exit


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_daemon_process_e2e(tmp_path):
    """子进程完整生命周期：起→runtime.json→health→shutdown→退出"""
    port = _free_port()
    env = {**os.environ, "LLMAPIG_DATA_DIR": str(tmp_path),
           "LLMAPIG_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "desktop.daemon"], env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        rj = tmp_path / "runtime.json"
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stderr.read().decode(errors="replace")
                pytest.fail(f"daemon 提前退出: {out}")
            if rj.exists():
                break
            time.sleep(0.3)
        assert rj.exists(), "30s 未写出 runtime.json"
        meta = json.loads(rj.read_text(encoding="utf-8"))
        assert meta["port"] == port
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r.json() == {"status": "ok"}
        r = httpx.post(f"http://127.0.0.1:{port}/shutdown",
                       headers={"X-Shutdown-Token": meta["token"]},
                       timeout=5)
        assert r.status_code == 200
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_daemon_requires_data_dir(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "LLMAPIG_DATA_DIR"}
    proc = subprocess.run(
        [sys.executable, "-m", "desktop.daemon"], env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True, timeout=30)
    assert proc.returncode == 2
