"""端口冲突检测与僵尸进程清理（Windows）。

启动时检测目标端口是否被占用：
- 旧 llm-apig 进程 → 自动清理，返回 True
- 其他应用 → 报错提示，返回 False
- 端口空闲 → 直接返回 True
"""

import logging
import os
import socket
import subprocess
import time

logger = logging.getLogger(__name__)


def _port_in_use(port: int) -> bool:
    """检查 127.0.0.1:port 是否已被监听（TCP connect 探测）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _get_occupying_process(port: int) -> tuple[int, str] | None:
    """Windows: 通过 netstat 查找占用指定端口的进程 PID 和名称。

    Returns (pid, name) or None.
    """
    if os.name != "nt":
        return None
    try:
        output = subprocess.check_output(
            f'netstat -ano | findstr :{port}',
            shell=True, timeout=5, text=True,
        )
        for line in output.strip().split("\n"):
            parts = line.split()
            # 找 LISTENING 行：Proto  Local_Address  Foreign_Address  State  PID
            if len(parts) >= 5 and "LISTENING" in line:
                pid = int(parts[-1])
                name = _get_process_name(pid)
                return (pid, name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _get_process_name(pid: int) -> str:
    """通过 tasklist 获取进程名。"""
    try:
        output = subprocess.check_output(
            f'tasklist /FI "PID eq {pid}" /FO CSV /NH',
            shell=True, timeout=5, text=True,
        )
        name = output.strip().split("\n")[0].split(",")[0].strip('"')
        return name if name else f"PID_{pid}"
    except Exception:
        return f"PID_{pid}"


def _get_process_commandline(pid: int) -> str:
    """通过 PowerShell 获取进程命令行（用于判断 Python 进程是否为 llm-apig）。"""
    try:
        output = subprocess.check_output(
            f'powershell -NoProfile -Command '
            f'"try {{ (Get-Process -Id {pid}).CommandLine }} catch {{}}"',
            shell=True, timeout=10, text=True,
        )
        return output.strip()
    except Exception:
        return ""


def _is_stale_llm_apig(pid: int, name: str) -> bool:
    """判断占用端口的进程是否是残留的 llm-apig 进程。"""
    name_lower = name.lower()

    # 打包后的 daemon 可执行文件
    if name_lower in ("llm-apig-daemon.exe", "llm-apig.exe"):
        return True

    # Python 进程：需要检查命令行
    if name_lower in ("python.exe", "pythonw.exe", "python", "python3"):
        cmdline = _get_process_commandline(pid)
        cmd_lower = cmdline.lower()
        llm_keywords = ("llm-apig", "llmapig", "desktop.daemon", "uvicorn")
        return any(kw in cmd_lower for kw in llm_keywords)

    return False


def _kill_process(pid: int) -> bool:
    """终止指定 PID 的进程。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.kill(pid, 9)
        return True
    except Exception:
        return False


def resolve_port_conflict(port: int) -> bool:
    """解决端口冲突：如果是僵尸 llm-apig 进程则清理并返回 True。

    其他应用占用时返回 False（不杀非 llm-apig 进程）。
    端口空闲时直接返回 True。
    """
    if not _port_in_use(port):
        return True

    info = _get_occupying_process(port)
    if info is None:
        logger.warning("端口 %d 被占用，但无法识别占用进程", port)
        return False

    pid, name = info
    if _is_stale_llm_apig(pid, name):
        logger.info(
            "端口 %d 被旧 llm-apig 进程(PID %d, %s)占用，正在清理...",
            port, pid, name,
        )
        if _kill_process(pid):
            time.sleep(0.5)
            if not _port_in_use(port):
                logger.info("端口 %d 已释放", port)
                return True
            logger.warning("端口 %d 释放超时", port)
            return False
        else:
            logger.warning("无法终止进程 PID %d", pid)
            return False
    else:
        logger.warning(
            "端口 %d 被 %s(PID %d) 占用，请更换 LLMAPIG_PORT 或关闭该应用",
            port, name, pid,
        )
        return False