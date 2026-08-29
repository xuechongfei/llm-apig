"""一键构建：pytest → PyInstaller daemon → tauri build (NSIS) → 冒烟三段自检"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SRC_TAURI = ROOT / "src-tauri"
RESOURCES = SRC_TAURI / "resources"
BUNDLE = SRC_TAURI / "target" / "release" / "bundle"


def run(cmd: list, **kw) -> None:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    kw.setdefault("cwd", ROOT)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def sync_tauri_version(ver: str) -> None:
    conf = SRC_TAURI / "tauri.conf.json"
    data = json.loads(conf.read_text(encoding="utf-8"))
    data["version"] = ver
    conf.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"tauri.conf.json 版本同步: {ver}")


def pyinstaller_daemon() -> Path:
    run([
        "uv", "run", "pyinstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "llm-apig-daemon",
        "--paths", ".",
        "--add-data", "app/admin/templates;app/admin/templates",
        "--add-data", "pyproject.toml;.",
        "desktop/daemon.py",
    ])
    exe = ROOT / "dist" / "llm-apig-daemon.exe"
    RESOURCES.mkdir(parents=True, exist_ok=True)
    shutil.copy(exe, RESOURCES / "llm-apig-daemon.exe")
    # 原始 exe 也放一份，供冒烟直接跑 target/release 的壳
    rel_res = SRC_TAURI / "target" / "release" / "resources"
    rel_res.mkdir(parents=True, exist_ok=True)
    shutil.copy(exe, rel_res / "llm-apig-daemon.exe")
    return RESOURCES / "llm-apig-daemon.exe"


def tauri_build() -> None:
    # Tauri 2 bundler 读取 TAURI_SIGNING_PRIVATE_KEY（非 TAURI_PRIVATE_KEY）。
    # 优先读 .tauri/llm-apig.key 文件，环境变量已设则跳过文件读取。
    if not os.environ.get("TAURI_SIGNING_PRIVATE_KEY"):
        key_file = ROOT / ".tauri" / "llm-apig.key"
        if key_file.exists():
            os.environ["TAURI_SIGNING_PRIVATE_KEY"] = \
                key_file.read_text(encoding="utf-8").strip()
    if not os.environ.get("TAURI_SIGNING_PRIVATE_KEY"):
        raise SystemExit(
            "缺少 TAURI_SIGNING_PRIVATE_KEY —— "
            "请在 .tauri/llm-apig.key 放置私钥文件，"
            "或设置环境变量 TAURI_SIGNING_PRIVATE_KEY")
    # 私钥密码（无密码则为空字符串）。部分环境下 os.environ 修改可能不被
    # cargo/tauri 子进程继承，因此同时设置 TAURI_PRIVATE_KEY_PASSWORD 兜底。
    for _name in ("TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
                  "TAURI_PRIVATE_KEY_PASSWORD"):
        os.environ[_name] = ""
    run(["cargo", "tauri", "build"], cwd=SRC_TAURI)


def generate_latest_json(ver: str) -> Path:
    """Tauri 2.11 bundler 的 createUpdaterArtifacts v1Compatible 仅产 .sig，
    需手动生成 latest.json。"""
    import datetime
    nsis_dir = BUNDLE / "nsis"
    # 查找 .nsis.zip.sig 文件获取签名
    sig_files = list(nsis_dir.glob("*.nsis.zip.sig"))
    if not sig_files:
        raise SystemExit("未找到 updater 签名文件（*.nsis.zip.sig）")
    signature = sig_files[0].read_text(encoding="utf-8").strip()
    # 对应 .nsis.zip 文件名
    zip_name = sig_files[0].name.replace(".sig", "")
    latest = {
        "version": ver,
        "notes": "",
        "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "windows-x86_64": {
                "signature": signature,
                "url": (f"https://github.com/xuechongfei/llm-apig/releases/"
                        f"download/v{ver}/{zip_name}"),
            }
        },
    }
    lj_path = nsis_dir / "latest.json"
    lj_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    print(f"更新清单: {lj_path}")
    return lj_path


def _free_port_guard(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pid_alive(pid: int) -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True)
    return str(pid) in r.stdout


def _launch_shell(td: Path):
    exe = SRC_TAURI / "target" / "release" / "llm-apig-shell.exe"
    env = {**os.environ, "LLMAPIG_DATA_DIR": str(td)}
    return subprocess.Popen([str(exe)], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL), exe


def _wait_runtime(td: Path, proc, timeout=60) -> dict:
    rj = td / "runtime.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"壳提前退出（exit={proc.returncode}）")
        if rj.exists():
            try:
                return json.loads(rj.read_text(encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(0.5)
    raise SystemExit(f"{timeout}s 未写出 runtime.json")


def _healthy(port: int) -> bool:
    import httpx
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health",
                         timeout=5).json() == {"status": "ok"}
    except Exception:
        return False


def _kill_hard(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                   capture_output=True)


def smoke() -> None:
    """三段：①健康 ②强杀壳→daemon 树消失（Job）③优雅停"""
    import httpx
    # ① 冷启动健康
    td1 = Path(tempfile.mkdtemp())
    p1, _ = _launch_shell(td1)
    meta = _wait_runtime(td1, p1)
    assert _healthy(meta["port"]), "冒烟①失败：健康检查未通过"
    # ② 强杀壳 → daemon 必须被 Job 连带清理
    _kill_hard(p1.pid)
    p1.wait(timeout=15)
    deadline = time.time() + 15
    while _pid_alive(meta["pid"]) and time.time() < deadline:
        time.sleep(0.5)
    assert not _pid_alive(meta["pid"]), "冒烟②失败：强杀壳后 daemon 未消失（Job Object 失效）"
    # ③ 优雅停：POST /shutdown 后 daemon 退出，壳还活着，最后强杀壳收尾
    td2 = Path(tempfile.mkdtemp())
    p2, _ = _launch_shell(td2)
    meta2 = _wait_runtime(td2, p2)
    assert _healthy(meta2["port"]), "冒烟③失败：二次启动健康检查未通过"
    httpx.post(f"http://127.0.0.1:{meta2['port']}/shutdown",
               headers={"X-Shutdown-Token": meta2["token"]}, timeout=5)
    deadline = time.time() + 15
    while _pid_alive(meta2["pid"]) and time.time() < deadline:
        time.sleep(0.5)
    assert not _pid_alive(meta2["pid"]), "冒烟③失败：优雅停后 daemon 未退出"
    _kill_hard(p2.pid)
    print("冒烟通过（健康/Job 强杀清理/优雅停机）")


def main() -> None:
    ap = argparse.ArgumentParser(description="llm-apig 一键构建")
    ap.add_argument("--no-smoke", action="store_true",
                    help="跳过冒烟自检（CI 环境使用）")
    ap.add_argument("--no-test", action="store_true",
                    help="跳过 pytest（CI 环境测试已单独跑）")
    args = ap.parse_args()

    ver = version()
    print(f"=== 构建 llm-apig v{ver}（Tauri 壳） ===")
    if not args.no_test:
        run(["uv", "run", "pytest", "tests/", "-q"])
    pyinstaller_daemon()
    sync_tauri_version(ver)
    tauri_build()
    generate_latest_json(ver)
    # NSIS 产物改名归档到 dist/
    nsis = list(BUNDLE.glob("nsis/*-setup.exe"))
    if nsis:
        dst = ROOT / "dist" / f"llm-apig-setup-{ver}.exe"
        shutil.copy(nsis[0], dst)
        print(f"安装包: {dst}")
    for lj in BUNDLE.glob("nsis/latest.json"):
        print(f"更新清单（上传到 Release）: {lj}")
    if not args.no_smoke:
        smoke()
    else:
        print("跳过冒烟自检（--no-smoke）")
    print("=== 构建全部完成 ===")


if __name__ == "__main__":
    main()