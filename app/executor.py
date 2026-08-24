import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.convert import a2o, o2a, stream_a2o, stream_o2a
from app.db import connect, get_setting
from app.errors import classify_error
from app.logging_ import add_attempt, create_log, finish_log, set_cooldown
from app.selector import detect_modalities, select_candidates

MAX_ATTEMPTS = 5
_transport: httpx.MockTransport | None = None  # 测试注入点

_COOLDOWN_SETTING_KEYS = {
    "insufficient_balance": ("cooldown_balance", 600),
    "rate_limit": ("cooldown_ratelimit", 60),
    "auth": ("cooldown_auth", 1800),
    "server": ("cooldown_server", 60),
}


@dataclass
class GatewayResult:
    status: int
    body: dict | None = None
    events: AsyncIterator[str] | None = None


def render_sse(events: list[dict], protocol: str) -> list[str]:
    out = []
    for e in events:
        data = json.dumps(e, ensure_ascii=False)
        if protocol == "anthropic":
            out.append(f"event: {e['type']}\ndata: {data}\n\n")
        else:
            out.append(f"data: {data}\n\n")
    return out


def _error_body(protocol: str, message: str, status: int) -> dict:
    if protocol == "anthropic":
        return {"type": "error",
                "error": {"type": "api_error", "message": message}}
    return {"error": {"message": message, "type": "api_error", "code": status}}


def _target(cand) -> tuple[str, dict]:
    base = cand.base_url.rstrip("/")
    if cand.protocol == "anthropic":
        return f"{base}/v1/messages", {
            "x-api-key": cand.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return f"{base}/v1/chat/completions", {
        "authorization": f"Bearer {cand.api_key}",
        "content-type": "application/json",
    }


def _cooldown_seconds(conn, category: str, default: int) -> int:
    item = _COOLDOWN_SETTING_KEYS.get(category)
    if not item:
        return default
    key, fallback = item
    try:
        return int(get_setting(conn, key, str(fallback)))
    except ValueError:
        return fallback


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=httpx.Timeout(120.0, connect=15.0))


async def execute(conn, *, entry_protocol: str, group_name: str,
                  payload: dict, stream: bool) -> GatewayResult:
    started = time.time()
    modalities = detect_modalities(payload)
    candidates, skipped = select_candidates(
        conn, group_name, entry_protocol, modalities)
    log_id = create_log(conn, client_protocol=entry_protocol,
                        group_name=group_name,
                        path="/v1/messages" if entry_protocol == "anthropic"
                        else "/v1/chat/completions",
                        stream=stream)
    for s in skipped:
        add_attempt(conn, log_id, channel_id=s.channel_id,
                    channel_name=s.channel_name, skipped=s.reason)

    if not candidates:
        finish_log(conn, log_id, status="failed",
                   total_ms=_elapsed(started), input_tokens=None, output_tokens=None)
        return GatewayResult(502, body=_error_body(
            entry_protocol, f"模型组 {group_name!r} 没有可用渠道", 502))

    last_status, last_body = 502, {}
    for cand in candidates[:MAX_ATTEMPTS]:
        url, headers = _target(cand)
        upstream_payload = dict(payload)
        upstream_payload["model"] = cand.actual_model
        upstream_payload["stream"] = stream
        if cand.needs_conversion:
            try:
                if entry_protocol == "anthropic":
                    upstream_payload = a2o.request(upstream_payload)
                else:
                    upstream_payload = o2a.request(upstream_payload)
            except a2o.ConversionError as e:
                add_attempt(conn, log_id, channel_id=cand.channel_id,
                            channel_name=cand.channel_name,
                            actual_model=cand.actual_model, converted=True,
                            error_category="conversion",
                            error_summary=str(e))
                continue

        if stream:
            result, consumed = await _try_stream(
                conn, log_id, cand, entry_protocol, url, headers,
                upstream_payload, started, group_name)
            if result is not None:
                return result
            last_status, last_body = consumed
            continue

        result, consumed = await _try_non_stream(
            conn, log_id, cand, entry_protocol, url, headers,
            upstream_payload, started, group_name)
        if result is not None:
            return result
        last_status, last_body = consumed

    finish_log(conn, log_id, status="failed", total_ms=_elapsed(started),
               input_tokens=None, output_tokens=None)
    body = last_body if isinstance(last_body, dict) and last_body else _error_body(
        entry_protocol, "所有渠道均失败", last_status)
    return GatewayResult(last_status, body=body)


def _elapsed(started: float) -> int:
    return int((time.time() - started) * 1000)


async def _try_non_stream(conn, log_id, cand, entry_protocol, url, headers,
                          upstream_payload, started, group_name):
    """返回 (GatewayResult | None, (status, body))。None 表示失败已记录可重试。"""
    try:
        async with _client() as client:
            resp = await client.post(url, json=upstream_payload, headers=headers)
    except httpx.HTTPError as e:
        _record_failure(conn, log_id, cand, classify_error(None, str(e)), None, str(e))
        return None, (502, {})

    if resp.status_code < 400:
        body = resp.json()
        if cand.needs_conversion:
            body = (o2a.response(body, group_name) if entry_protocol == "openai"
                    else a2o.response(body, group_name))
        add_attempt(conn, log_id, channel_id=cand.channel_id,
                    channel_name=cand.channel_name,
                    actual_model=cand.actual_model,
                    converted=cand.needs_conversion, http_status=resp.status_code)
        usage = body.get("usage") or {}
        finish_log(conn, log_id, status="success", total_ms=_elapsed(started),
                   input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
                   output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"))
        return GatewayResult(resp.status_code, body=body), (resp.status_code, body)

    body_text = resp.text
    verdict = classify_error(resp.status_code, body_text)
    _record_failure(conn, log_id, cand, verdict, resp.status_code, body_text)
    if not verdict.retryable:
        finish_log(conn, log_id, status="failed", total_ms=_elapsed(started),
                   input_tokens=None, output_tokens=None)
        try:
            parsed = resp.json()
        except ValueError:
            parsed = _error_body(entry_protocol, body_text[:500], resp.status_code)
        return GatewayResult(resp.status_code, body=parsed), (resp.status_code, {})
    return None, (resp.status_code, _safe_json(body_text))


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _record_failure(conn, log_id, cand, verdict, http_status, raw: str) -> None:
    add_attempt(conn, log_id, channel_id=cand.channel_id,
                channel_name=cand.channel_name, actual_model=cand.actual_model,
                converted=cand.needs_conversion, http_status=http_status,
                error_category=verdict.category.value,
                error_summary=verdict.summary, raw_error=(raw or "")[:2000])
    if verdict.cooldown_seconds:
        secs = _cooldown_seconds(conn, verdict.category.value,
                                 verdict.cooldown_seconds)
        set_cooldown(conn, cand.channel_id, secs, verdict.summary)


async def _try_stream(conn, log_id, cand, entry_protocol, url, headers,
                      upstream_payload, started, group_name):
    """流式尝试。状态码错误（未读 body）可安全重试；2xx 后产出事件生成器。"""
    try:
        client = _client()
        req = client.build_request("POST", url, json=upstream_payload,
                                   headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        _record_failure(conn, log_id, cand, classify_error(None, str(e)), None, str(e))
        return None, (502, {})

    if resp.status_code >= 400:
        raw = (await resp.aread()).decode("utf-8", "replace")
        await resp.aclose()
        await client.aclose()
        verdict = classify_error(resp.status_code, raw)
        _record_failure(conn, log_id, cand, verdict, resp.status_code, raw)
        if not verdict.retryable:
            finish_log(conn, log_id, status="failed",
                       total_ms=_elapsed(started), input_tokens=None,
                       output_tokens=None)
            return GatewayResult(resp.status_code,
                                 body=_safe_json(raw) or _error_body(
                                     entry_protocol, raw[:500], resp.status_code)), \
                (resp.status_code, {})
        return None, (resp.status_code, _safe_json(raw))

    add_attempt(conn, log_id, channel_id=cand.channel_id,
                channel_name=cand.channel_name, actual_model=cand.actual_model,
                converted=cand.needs_conversion, http_status=200)

    async def events() -> AsyncIterator[str]:
        failed_mid = None
        try:
            if not cand.needs_conversion:
                async for line in resp.aiter_text():
                    yield line
            else:
                converter = (stream_o2a.O2AStreamConverter(group_name)
                             if cand.protocol == "anthropic"
                             else stream_a2o.A2OStreamConverter(group_name))
                buf = ""
                async for chunk_bytes in resp.aiter_text():
                    buf += chunk_bytes
                    while "\n\n" in buf:
                        raw_event, buf = buf.split("\n\n", 1)
                        data = _sse_data(raw_event)
                        if data is None:
                            continue
                        if data.strip() == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        for out in render_sse(converter.feed(obj), entry_protocol):
                            yield out
                for out in render_sse(converter.finish(), entry_protocol):
                    yield out
                if entry_protocol == "openai":
                    yield "data: [DONE]\n\n"
        except httpx.HTTPError as e:
            failed_mid = str(e)
            for out in render_sse(
                    [{"type": "error", "error": {
                        "type": "api_error",
                        "message": f"上游流中断: {e}"}}], entry_protocol):
                yield out
            if entry_protocol == "openai":
                yield "data: [DONE]\n\n"
        finally:
            await resp.aclose()
            await client.aclose()
            # 流式生成器的生命周期可能长于路由层传入的连接：新开连接写日志
            wconn = connect()
            try:
                status = "success" if failed_mid is None else "failed"
                finish_log(wconn, log_id, status=status,
                           total_ms=_elapsed(started),
                           input_tokens=None, output_tokens=None)
                if failed_mid is not None:
                    verdict = classify_error(None, failed_mid)
                    _record_failure(wconn, log_id, cand, verdict, None, failed_mid)
            finally:
                wconn.close()

    return GatewayResult(200, events=events()), (200, {})


def _sse_data(raw_event: str) -> str | None:
    """从一个 SSE 事件块中提取 data 字段（可能多行）。"""
    lines = [ln[5:] for ln in raw_event.splitlines() if ln.startswith("data:")]
    if not lines:
        return None
    return "\n".join(lines)
