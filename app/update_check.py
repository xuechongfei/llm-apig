"""版本号读取。更新检查已由 Tauri updater 承担（壳侧），本模块仅保留
current_version 供构建脚本与 daemon 的 runtime.json 使用。"""

import sys
import tomllib
from pathlib import Path


def current_version() -> str:
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "pyproject.toml"
    else:
        path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)["project"]["version"]