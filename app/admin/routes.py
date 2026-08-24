import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import connect
from app.logging_ import clear_cooldown

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"))
_transport = None  # 测试注入点


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = connect()
    try:
        channels = conn.execute(
            "SELECT c.*, COALESCE(s.cooldown_until,0) cooldown_until, s.last_error"
            " FROM channel c LEFT JOIN channel_state s ON s.channel_id=c.id"
            " ORDER BY c.id").fetchall()
        logs = conn.execute(
            "SELECT * FROM request_log ORDER BY id DESC LIMIT 20").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "channels": channels, "logs": logs,
        "now": time.time()})


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    conn = connect()
    try:
        channels = conn.execute(
            "SELECT c.*, COALESCE(s.cooldown_until,0) cooldown_until, s.last_error"
            " FROM channel c LEFT JOIN channel_state s ON s.channel_id=c.id"
            " ORDER BY c.id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "channels.html",
                                      {"channels": channels})


@router.get("/channels/new", response_class=HTMLResponse)
async def channel_new(request: Request):
    return templates.TemplateResponse(request, "channel_form.html",
                                      {"ch": None})


@router.post("/channels")
async def channel_create(name: str = Form(...), protocol: str = Form(...),
                         base_url: str = Form(...), api_key: str = Form("")):
    conn = connect()
    try:
        conn.execute("INSERT INTO channel (name,protocol,base_url,api_key)"
                     " VALUES (?,?,?,?)", (name, protocol, base_url, api_key))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.get("/channels/{cid}/edit", response_class=HTMLResponse)
async def channel_edit(request: Request, cid: int):
    conn = connect()
    try:
        ch = conn.execute("SELECT * FROM channel WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "channel_form.html",
                                      {"ch": ch})


@router.post("/channels/{cid}/edit")
async def channel_update(cid: int, name: str = Form(...),
                         protocol: str = Form(...), base_url: str = Form(...),
                         api_key: str = Form("")):
    conn = connect()
    try:
        if api_key:
            conn.execute("UPDATE channel SET name=?,protocol=?,base_url=?,api_key=?"
                         " WHERE id=?", (name, protocol, base_url, api_key, cid))
        else:  # 留空表示不修改密钥
            conn.execute("UPDATE channel SET name=?,protocol=?,base_url=?"
                         " WHERE id=?", (name, protocol, base_url, cid))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/toggle")
async def channel_toggle(cid: int):
    conn = connect()
    try:
        conn.execute("UPDATE channel SET enabled = 1 - enabled WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/delete")
async def channel_delete(cid: int):
    conn = connect()
    try:
        conn.execute("DELETE FROM channel WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/clear-cooldown")
async def channel_clear_cooldown(cid: int):
    conn = connect()
    try:
        clear_cooldown(conn, cid)
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@router.post("/channels/{cid}/test")
async def channel_test(cid: int):
    conn = connect()
    try:
        ch = conn.execute("SELECT * FROM channel WHERE id=?", (cid,)).fetchone()
        model_row = conn.execute(
            "SELECT actual_model FROM model_mapping WHERE channel_id=?"
            " ORDER BY priority LIMIT 1", (cid,)).fetchone()
    finally:
        conn.close()
    if ch is None:
        return JSONResponse({"ok": False, "detail": "渠道不存在"}, 404)
    model = model_row["actual_model"] if model_row else (
        "claude-haiku-4-5" if ch["protocol"] == "anthropic" else "gpt-4o-mini")
    base = ch["base_url"].rstrip("/")
    if ch["protocol"] == "anthropic":
        url, headers = f"{base}/v1/messages", {
            "x-api-key": ch["api_key"], "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": 1,
                   "messages": [{"role": "user", "content": "hi"}]}
    else:
        url, headers = f"{base}/v1/chat/completions", {
            "authorization": f"Bearer {ch['api_key']}"}
        payload = {"model": model, "max_tokens": 1,
                   "messages": [{"role": "user", "content": "hi"}]}
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"网络错误: {e}"}
    ok = resp.status_code < 400
    return {"ok": ok, "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}
