import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

import app.executor as ex
from app.db import connect
from app.router_anthropic import verify_token

router = APIRouter()


@router.post("/v1/chat/completions", dependencies=[Depends(verify_token)])
async def chat_completions(request: Request):
    payload = await request.json()
    conn = connect()
    try:
        result = await ex.execute(
            conn, entry_protocol="openai",
            group_name=payload.get("model", ""),
            payload=payload, stream=bool(payload.get("stream")))
    finally:
        conn.close()
    if result.events is not None:
        return StreamingResponse(result.events,
                                 media_type="text/event-stream",
                                 status_code=result.status)
    return JSONResponse(result.body, status_code=result.status)


@router.get("/v1/models", dependencies=[Depends(verify_token)])
async def list_models():
    conn = connect()
    try:
        rows = conn.execute("SELECT name FROM model_group ORDER BY name").fetchall()
    finally:
        conn.close()
    return {"object": "list", "data": [
        {"id": r["name"], "object": "model", "created": int(time.time()),
         "owned_by": "llm-apig"} for r in rows]}
