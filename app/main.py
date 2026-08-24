import os
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
    if not os.environ.get("LLMAPIG_DATA_DIR"):  # 桌面模式由壳开窗口
        threading.Timer(1.0,
                        lambda: webbrowser.open("http://127.0.0.1:8317/admin")).start()
    uvicorn.run(app, host="127.0.0.1", port=8317)


if __name__ == "__main__":
    main()
