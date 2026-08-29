"""llm-apig daemon 入口：独立 sidecar 进程，由 Tauri 壳拉起。

环境变量（均由壳注入）：
  LLMAPIG_DATA_DIR  数据目录（必需，缺失退出码 2）
  LLMAPIG_PORT      监听端口（缺省 58317）
"""

import json
import os
import secrets
import sys
import threading

import uvicorn


def pick_port(env: str | None) -> int:
    if env:
        try:
            p = int(env)
        except ValueError:
            p = 0
        if 0 < p < 65536:
            return p
    return 58317


def runtime_payload(port: int, token: str) -> dict:
    from app.update_check import current_version
    return {"port": port, "version": current_version(),
            "token": token, "pid": os.getpid()}


def attach_shutdown(app, token: str, server: uvicorn.Server) -> None:
    """POST /shutdown：token 校验通过后置 should_exit 优雅停机。

    仅绑定 127.0.0.1（uvicorn 配置保证），token 防 admin 页被恶意网页
    CSRF 后借 fetch 打该端点。
    """
    from fastapi import HTTPException, Request

    @app.post("/shutdown")
    async def shutdown(request: Request):
        got = request.headers.get("X-Shutdown-Token", "")
        if not secrets.compare_digest(got, token):
            raise HTTPException(status_code=403)
        threading.Timer(
            0.2, lambda: setattr(server, "should_exit", True)).start()
        return {"status": "ok"}


def attach_restart_handler(app, server: uvicorn.Server) -> None:
    """POST /admin/settings/restart：写入 .restart 标记后优雅退出。

    壳的崩溃监控线程检测到 .restart 标记后会重启 daemon（绕过重启预算）。
    """
    from desktop.config import default_data_dir

    @app.post("/admin/settings/restart")
    async def restart():
        marker = default_data_dir() / ".restart"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        threading.Timer(
            0.2, lambda: setattr(server, "should_exit", True)).start()
        return {"ok": True, "message": "正在重启..."}


def main() -> int:
    data_env = os.environ.get("LLMAPIG_DATA_DIR")
    if not data_env:
        print("LLMAPIG_DATA_DIR 未设置（daemon 必须由桌面壳拉起）",
              file=sys.stderr)
        return 2

    from desktop import paths
    paths.setup_logging()
    from app.db import init_db
    from app.main import app
    init_db()

    port = pick_port(os.environ.get("LLMAPIG_PORT"))
    token = secrets.token_urlsafe(32)
    (paths.data_dir() / "runtime.json").write_text(
        json.dumps(runtime_payload(port, token)), encoding="utf-8")

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None))
    attach_shutdown(app, token, server)
    attach_restart_handler(app, server)

    # 清理残留的 .restart 标记（上次重启可能未完成）
    from desktop.config import default_data_dir
    marker = default_data_dir() / ".restart"
    if marker.exists():
        marker.unlink()
        import logging
        logging.getLogger(__name__).info("已清理残留的 .restart 标记")

    server.run()  # 阻塞；should_exit 后返回
    return 0


if __name__ == "__main__":
    sys.exit(main())