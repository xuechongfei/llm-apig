"""一键构建：pytest → PyInstaller → Inno Setup → version.json → 冒烟"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 中文 Windows 控制台默认 GBK，无法编码 ⚠ 等字符 —— 输出流改为替换模式防崩溃
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

ISCC_CANDIDATES = [
    os.environ.get("ISCC"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def run(cmd: list, **kw) -> None:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT, **kw)


def current_version() -> str:
    sys.path.insert(0, str(ROOT))
    from app.update_check import current_version as cv
    return cv()


def pyinstaller(version: str) -> None:
    ico = ROOT / "build" / "llm-apig.ico"
    ico.parent.mkdir(exist_ok=True)
    from desktop.tray import make_icon_image
    make_icon_image().save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    run([
        "uv", "run", "pyinstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", "llm-apig",
        "--icon", ico,
        "--paths", ".",
        "--add-data", "app/admin/templates;app/admin/templates",
        "--add-data", "pyproject.toml;.",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.platforms.winforms",
        "desktop/main.py",
    ])
    print(f"PyInstaller 完成: {DIST / 'llm-apig' / 'llm-apig.exe'}")


def update_version_json(version: str) -> None:
    path = ROOT / "version.json"
    old: dict = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data = {"version": version,
            "notes": old.get("notes", "") if old.get("version") == version else "",
            "url": old.get("url", "") if old.get("version") == version else ""}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"version.json 已更新: {version}（发布前请填写 notes/url）")


def inno(version: str) -> bool:
    iscc = next((p for p in ISCC_CANDIDATES if p and Path(p).exists()), None)
    if not iscc:
        print("⚠ 未找到 Inno Setup（ISCC.exe），跳过安装包。")
        print("  安装：winget install -e --id JRSoftware.InnoSetup 后重跑")
        return False
    run([iscc, f"/DMyAppVersion={version}", "llm-apig.iss"])
    print(f"安装包完成: {DIST / f'llm-apig-setup-{version}.exe'}")
    return True


def smoke() -> None:
    """启动打包产物 → 等 runtime.json → 健康检查 → 结束进程"""
    exe = DIST / "llm-apig" / "llm-apig.exe"
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "LLMAPIG_DATA_DIR": td}
        proc = subprocess.Popen([str(exe)], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        rj = Path(td) / "runtime.json"
        deadline = time.time() + 30
        port = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise SystemExit("冒烟失败：进程提前退出（是否已有实例在运行？先退出再构建）")
            if rj.exists():
                port = json.loads(rj.read_text(encoding="utf-8"))["port"]
                break
            time.sleep(0.5)
        if port is None:
            proc.kill()
            raise SystemExit("冒烟失败：30s 内未写出 runtime.json")
        import httpx
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r.json() == {"status": "ok"}, "冒烟失败：健康检查未通过"
        proc.terminate()
        proc.wait(timeout=10)
    print(f"冒烟通过（端口 {port}）")


def main() -> None:
    version = current_version()
    print(f"=== 构建 llm-apig v{version} ===")
    run(["uv", "run", "pytest", "tests/", "-q"])
    pyinstaller(version)
    update_version_json(version)
    inno(version)
    smoke()
    print("=== 构建全部完成 ===")


if __name__ == "__main__":
    main()
