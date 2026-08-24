import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import app.executor as ex
from app.db import connect, get_setting
from app.selector import select_candidates

router = APIRouter()


async def verify_token(request: Request):
    conn = connect()
    try:
        token = get_setting(conn, "api_key", "")
    finally:
        conn.close()
    if not token:
        return
    provided = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        provided = provided or auth[7:]
    if provided != token:
        raise HTTPException(status_code=401, detail={
            "type": "error", "error": {"type": "authentication_error",
                                       "message": "invalid api key"}})


@router.post("/v1/messages", dependencies=[Depends(verify_token)])
async def messages(request: Request):
    payload = await request.json()
    conn = connect()
    try:
        result = await ex.execute(
            conn, entry_protocol="anthropic",
            group_name=payload.get("model", ""),
            payload=payload, stream=bool(payload.get("stream")))
    finally:
        conn.close()
    if result.events is not None:
        return StreamingResponse(result.events,
                                 media_type="text/event-stream",
                                 status_code=result.status)
    return JSONResponse(result.body, status_code=result.status)


@router.post("/v1/messages/count_tokens", dependencies=[Depends(verify_token)])
async def count_tokens(request: Request):
    payload = await request.json()
    conn = connect()
    try:
        candidates, _ = select_candidates(
            conn, payload.get("model", ""), "anthropic", set())
        cand = next((c for c in candidates if c.protocol == "anthropic"), None)
        if cand is None:
            # OpenAI 渠道不支持 count_tokens：近似估算
            text = str(payload.get("messages", ""))
            return JSONResponse({"input_tokens": max(1, len(text) // 4)},
                                headers={"x-estimated": "true"})
        url = cand.base_url.rstrip("/") + "/v1/messages/count_tokens"
        headers = {"x-api-key": cand.api_key,
                   "anthropic-version": "2023-06-01"}
        async with httpx.AsyncClient(
                transport=ex._transport, timeout=30) as client:
            resp = await client.post(url, json={**payload, "model": cand.actual_model},
                                     headers=headers)
        return JSONResponse(resp.json() if resp.status_code < 400 else
                            {"input_tokens": 1}, status_code=resp.status_code)
    finally:
        conn.close()
