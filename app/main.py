import os
import sys
import threading
import webbrowser

import uvicorn
from fastapi import FastAPI

from app.admin.routes import router as admin_router
from app.db import init_db
from app.router_anthropic import router as anthropic_router
from app.router_openai import router as openai_router

app = FastAPI(title="llm-apig")

app.include_router(anthropic_router)
app.include_router(openai_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    init_db()
    port = int(os.environ.get("LLMAPIG_PORT", "58317"))

    # 端口冲突检测：僵尸 llm-apig 自动清理，其他应用占用则报错退出
    try:
        from desktop.portutil import resolve_port_conflict
    except ImportError:
        pass  # 非桌面模式（如直接 python -m app.main）可能没有 desktop 包
    else:
        if not resolve_port_conflict(port):
            print(f"端口 {port} 被其他应用占用，无法启动", file=sys.stderr)
            sys.exit(3)

    if not os.environ.get("LLMAPIG_DATA_DIR"):  # 桌面模式由壳开窗口
        threading.Timer(1.0,
                        lambda: webbrowser.open(f"http://127.0.0.1:{port}/admin")).start()

    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()