"""desktop/portutil.py 测试：端口冲突检测 + 僵尸进程清理。"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from desktop import portutil


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """获取一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 端口检测
# ---------------------------------------------------------------------------


class TestPortInUse:
    def test_free_port(self):
        port = _free_port()
        assert not portutil._port_in_use(port)

    def test_busy_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.listen(1)
            assert portutil._port_in_use(port)


class TestResolvePortConflict:
    def test_free_port_returns_true(self):
        port = _free_port()
        assert portutil.resolve_port_conflict(port)

    def test_non_llm_apig_process_returns_false(self, monkeypatch):
        """模拟非 llm-apig 进程占用端口 → 返回 False，不杀。"""
        port = _free_port()
        monkeypatch.setattr(
            portutil, "_port_in_use",
            lambda p: p == port,
        )
        monkeypatch.setattr(
            portutil, "_get_occupying_process",
            lambda p: (99999, "SomeOtherApp.exe"),
        )
        monkeypatch.setattr(
            portutil, "_is_stale_llm_apig",
            lambda pid, name: False,
        )
        killed = []
        monkeypatch.setattr(
            portutil, "_kill_process",
            lambda pid: killed.append(pid) or True,
        )
        assert not portutil.resolve_port_conflict(port)
        assert len(killed) == 0  # 不应该杀非 llm-apig 进程

    def test_stale_llm_apig_gets_cleaned(self, monkeypatch):
        """模拟僵尸 llm-apig 进程占用端口 → 清理后返回 True。"""
        port = _free_port()
        call_count = [0]

        def port_in_use_side_effect(p):
            call_count[0] += 1
            return call_count[0] == 1  # 第一次调用返回 True，之后返回 False

        monkeypatch.setattr(portutil, "_port_in_use", port_in_use_side_effect)
        monkeypatch.setattr(
            portutil, "_get_occupying_process",
            lambda p: (12345, "python.exe"),
        )
        monkeypatch.setattr(
            portutil, "_get_process_commandline",
            lambda pid: "python -m desktop.daemon",
        )
        killed = []
        monkeypatch.setattr(
            portutil, "_kill_process",
            lambda pid: killed.append(pid) or True,
        )
        assert portutil.resolve_port_conflict(port)
        assert killed == [12345]

    def test_unknown_occupier_returns_false(self, monkeypatch):
        """无法识别占用进程 → 返回 False。"""
        port = _free_port()
        monkeypatch.setattr(portutil, "_port_in_use", lambda p: True)
        monkeypatch.setattr(
            portutil, "_get_occupying_process", lambda p: None,
        )
        assert not portutil.resolve_port_conflict(port)


class TestIsStaleLlmApig:
    def test_packaged_daemon_exe(self):
        assert portutil._is_stale_llm_apig(123, "llm-apig-daemon.exe")
        assert portutil._is_stale_llm_apig(123, "llm-apig.exe")

    def test_python_with_llm_apig_cmdline(self, monkeypatch):
        monkeypatch.setattr(
            portutil, "_get_process_commandline",
            lambda pid: "python -m desktop.daemon",
        )
        assert portutil._is_stale_llm_apig(123, "python.exe")

    def test_python_without_llm_apig_cmdline(self, monkeypatch):
        monkeypatch.setattr(
            portutil, "_get_process_commandline",
            lambda pid: "python -m http.server 8000",
        )
        assert not portutil._is_stale_llm_apig(123, "python.exe")

    def test_random_app(self):
        assert not portutil._is_stale_llm_apig(123, "WeChat.exe")
        assert not portutil._is_stale_llm_apig(123, "chrome.exe")


# ---------------------------------------------------------------------------
# daemon 集成：端口冲突时 daemon 行为
# ---------------------------------------------------------------------------


class TestDaemonPortConflict:
    def test_daemon_starts_when_port_free(self, tmp_path):
        """端口空闲 → daemon 正常启动。"""
        port = _free_port()
        env = {
            **os.environ,
            "LLMAPIG_DATA_DIR": str(tmp_path),
            "LLMAPIG_PORT": str(port),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "desktop.daemon"], env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            import json
            deadline = time.time() + 30
            rj = tmp_path / "runtime.json"
            while time.time() < deadline:
                if proc.poll() is not None:
                    out = proc.stderr.read().decode(errors="replace")
                    pytest.fail(f"daemon 提前退出: {out}")
                if rj.exists():
                    break
                time.sleep(0.3)
            assert rj.exists(), "30s 未写出 runtime.json"

            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
            assert r.json() == {"status": "ok"}

            meta = json.loads(rj.read_text(encoding="utf-8"))
            httpx.post(
                f"http://127.0.0.1:{port}/shutdown",
                headers={"X-Shutdown-Token": meta["token"]},
                timeout=5,
            )
            proc.wait(timeout=10)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_app_main_exits_when_port_occupied_by_other_app(self, tmp_path):
        """app.main 启动时端口被其他应用占用 → 退出码 3。"""
        port = _free_port()
        # 占用端口（模拟其他应用）
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
            s.listen(1)

            env = {
                **os.environ,
                "LLMAPIG_DATA_DIR": str(tmp_path),
                "LLMAPIG_PORT": str(port),
                "PYTHONIOENCODING": "utf-8",
            }
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from app.main import main; main()"],
                env=env,
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True, timeout=30,
            )
            # 退出码应为 3（端口冲突）
            assert proc.returncode == 3
            stderr = proc.stderr.decode("utf-8", errors="replace")
            assert "端口" in stderr