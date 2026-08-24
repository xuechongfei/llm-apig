import threading
import webbrowser

import uvicorn
from fastapi import FastAPI

from app.db import init_db

app = FastAPI(title="llm-apig")


@app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    init_db()
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8317/admin")).start()
    uvicorn.run(app, host="127.0.0.1", port=8317)


if __name__ == "__main__":
    main()
