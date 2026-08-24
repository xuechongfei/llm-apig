import sys
import time
import tomllib
from pathlib import Path

import httpx

_transport: httpx.MockTransport | None = None  # 测试注入点
CACHE_SECONDS = 3600
_cache: dict = {"at": 0.0, "result": None}


def version_gt(a: str, b: str) -> bool:
    def key(v: str) -> tuple:
        parts = []
        for p in str(v).strip().lstrip("vV").split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)
    return key(a) > key(b)


def current_version() -> str:
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "pyproject.toml"
    else:
        path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)["project"]["version"]


async def get_update_info(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache.get("result") is not None \
            and now - _cache.get("at", 0.0) < CACHE_SECONDS:
        return _cache["result"]
    from app.db import connect, get_setting
    conn = connect()
    try:
        url = get_setting(conn, "update_url", "")
    finally:
        conn.close()
    result: dict = {"update": None}
    if url:
        try:
            async with httpx.AsyncClient(transport=_transport, timeout=10) as client:
                resp = await client.get(url)
            data = resp.json()
            if not isinstance(data, dict):
                data = {}
            latest = str(data.get("version", ""))
            if latest and version_gt(latest, current_version()):
                result = {"update": {"latest": latest,
                                     "notes": str(data.get("notes", "")),
                                     "url": str(data.get("url", ""))}}
        except (httpx.HTTPError, ValueError):
            pass  # 检查失败静默，不影响使用
    _cache["result"] = result
    _cache["at"] = now
    return result
