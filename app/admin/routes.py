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


def ts_fmt(value):
    """unix 秒 → 本地可读时间"""
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError):
        return value


templates.env.filters["ts"] = ts_fmt
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


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request):
    conn = connect()
    try:
        groups = conn.execute(
            "SELECT g.*, COUNT(m.id) AS n FROM model_group g"
            " LEFT JOIN model_mapping m ON m.group_id=g.id"
            " GROUP BY g.id ORDER BY g.id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "groups.html",
                                      {"groups": groups})


@router.post("/groups")
async def group_create(name: str = Form(...)):
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO model_group (name) VALUES (?)", (name,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/groups", status_code=303)


@router.post("/groups/{gid}/delete")
async def group_delete(gid: int):
    conn = connect()
    try:
        conn.execute("DELETE FROM model_group WHERE id=?", (gid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/groups", status_code=303)


@router.get("/groups/{gid}", response_class=HTMLResponse)
async def group_detail(request: Request, gid: int):
    conn = connect()
    try:
        group = conn.execute("SELECT * FROM model_group WHERE id=?",
                             (gid,)).fetchone()
        mappings = conn.execute(
            "SELECT m.*, c.name AS channel_name, c.protocol"
            " FROM model_mapping m JOIN channel c ON c.id=m.channel_id"
            " WHERE m.group_id=? ORDER BY m.priority", (gid,)).fetchall()
        channels = conn.execute("SELECT * FROM channel ORDER BY id").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "group_detail.html", {
        "group": group, "mappings": mappings,
        "channels": channels})


@router.post("/groups/{gid}/mappings")
async def mapping_upsert(gid: int, channel_id: int = Form(...),
                         actual_model: str = Form(...),
                         priority: int = Form(100),
                         supports_image: str | None = Form(None),
                         supports_video: str | None = Form(None)):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO model_mapping (group_id,channel_id,actual_model,priority,"
            "supports_image,supports_video) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(group_id,channel_id,actual_model) DO UPDATE SET"
            " actual_model=excluded.actual_model, priority=excluded.priority,"
            " supports_image=excluded.supports_image,"
            " supports_video=excluded.supports_video",
            (gid, channel_id, actual_model, priority,
             1 if supports_image else 0, 1 if supports_video else 0))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{gid}", status_code=303)


@router.post("/mappings/{mid}/delete")
async def mapping_delete(mid: int):
    conn = connect()
    try:
        row = conn.execute("SELECT group_id FROM model_mapping WHERE id=?",
                           (mid,)).fetchone()
        conn.execute("DELETE FROM model_mapping WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{row['group_id']}", status_code=303)


@router.post("/mappings/{mid}/move")
async def mapping_move(mid: int, dir: str = "up"):
    conn = connect()
    try:
        cur = conn.execute("SELECT * FROM model_mapping WHERE id=?",
                           (mid,)).fetchone()
        op = "<" if dir == "up" else ">"
        order = "DESC" if dir == "up" else "ASC"
        neighbor = conn.execute(
            f"SELECT * FROM model_mapping WHERE group_id=? AND priority {op} ?"
            f" ORDER BY priority {order} LIMIT 1",
            (cur["group_id"], cur["priority"])).fetchone()
        if neighbor is None:  # 同优先级相邻的情况：按 id 找
            neighbor = conn.execute(
                f"SELECT * FROM model_mapping WHERE group_id=? AND priority=?"
                f" AND id {'<' if dir == 'up' else '>'} ? ORDER BY id {order}"
                " LIMIT 1",
                (cur["group_id"], cur["priority"], mid)).fetchone()
        if neighbor is not None:
            conn.execute("UPDATE model_mapping SET priority=? WHERE id=?",
                         (neighbor["priority"], mid))
            conn.execute("UPDATE model_mapping SET priority=? WHERE id=?",
                         (cur["priority"], neighbor["id"]))
            conn.commit()
        gid = cur["group_id"]
    finally:
        conn.close()
    return RedirectResponse(f"/admin/groups/{gid}", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, status: str = "", q: str = ""):
    sql = "SELECT * FROM request_log WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"; args.append(status)
    if q:
        sql += " AND group_name LIKE ?"; args.append(f"%{q}%")
    sql += " ORDER BY id DESC LIMIT 200"
    conn = connect()
    try:
        logs = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "logs.html",
                                      {"logs": logs, "status": status, "q": q})


@router.get("/logs/{log_id}", response_class=HTMLResponse)
async def log_detail(request: Request, log_id: int):
    conn = connect()
    try:
        log = conn.execute("SELECT * FROM request_log WHERE id=?",
                           (log_id,)).fetchone()
        attempts = conn.execute(
            "SELECT * FROM attempt WHERE log_id=? ORDER BY id",
            (log_id,)).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "log_detail.html", {
        "log": log, "attempts": attempts})


_SETTING_KEYS = ["cooldown_balance", "cooldown_ratelimit", "cooldown_auth",
                 "cooldown_server", "api_key", "balance_patterns",
                 "capability_patterns"]


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.db import get_setting
    from app.errors import BALANCE_PATTERNS, CAPABILITY_PATTERNS
    from desktop.paths import data_dir
    conn = connect()
    try:
        values = {k: get_setting(conn, k, "") for k in _SETTING_KEYS}
    finally:
        conn.close()
    if not values["balance_patterns"]:
        values["balance_patterns"] = "\n".join(BALANCE_PATTERNS)
    if not values["capability_patterns"]:
        values["capability_patterns"] = "\n".join(CAPABILITY_PATTERNS)
    return templates.TemplateResponse(request, "settings.html", {
        "v": values,
        "current_data_dir": str(data_dir()),
    })


@router.post("/settings")
async def settings_save(request: Request):
    from app.db import set_setting
    form = await request.form()
    conn = connect()
    try:
        for k in _SETTING_KEYS:
            set_setting(conn, k, str(form.get(k, "")))
    finally:
        conn.close()
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/settings/data-dir")
async def settings_data_dir(request: Request):
    import os
    import shutil
    import sqlite3
    from pathlib import Path

    from desktop.config import set_data_dir
    from desktop.paths import data_dir

    form = await request.form()
    new_dir = form.get("data_dir", "").strip()

    if not new_dir:
        return JSONResponse({"ok": False, "detail": "路径不能为空"}, 400)

    new_path = Path(new_dir).resolve()
    old_path = data_dir()

    if new_path == old_path:
        return JSONResponse({"ok": True, "data_dir": str(new_path)})

    # 校验：可创建且可写
    try:
        new_path.mkdir(parents=True, exist_ok=True)
        test_file = new_path / ".write_test"
        test_file.touch()
        test_file.unlink()
    except (OSError, PermissionError) as e:
        return JSONResponse(
            {"ok": False, "detail": f"路径不可写: {e}"}, 400)

    # 校验：磁盘空间
    db_file = old_path / "gateway.db"
    if db_file.exists():
        db_size = db_file.stat().st_size
        usage = shutil.disk_usage(new_path)
        if usage.free < db_size * 1.5:
            return JSONResponse(
                {"ok": False, "detail": "目标磁盘空间不足"}, 400)

    # 迁移
    try:
        if db_file.exists():
            # WAL checkpoint: 将所有 WAL 数据合并到主 DB 文件
            old_conn = sqlite3.connect(str(db_file))
            old_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            old_conn.close()

            # 复制主 DB 文件
            shutil.copy2(db_file, new_path / "gateway.db")

            # 复制 WAL 和 SHM 文件（如果存在）
            for suffix in (".db-wal", ".db-shm"):
                wal_file = old_path / f"gateway{suffix}"
                if wal_file.exists():
                    shutil.copy2(wal_file, new_path / f"gateway{suffix}")

        set_data_dir(str(new_path))

        shutil.rmtree(old_path, ignore_errors=True)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "detail": f"迁移失败: {e}"}, 500)

    return JSONResponse({"ok": True, "data_dir": str(new_path)})


