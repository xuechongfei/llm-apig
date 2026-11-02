import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app import usage as usage_mod
from app.convert import a2o, o2a, stream_a2o, stream_o2a
from app.db import connect, get_setting
from app.errors import classify_error
from app.logging_ import add_attempt, create_log, finish_log, set_cooldown
from app.reconstruct import AnthropicStreamReconstructor, parse_sse_event
from app.selector import detect_modalities, select_candidates

MAX_BODY_SIZE = 100_000  # 请求报文最大存储字节数
_transport: httpx.MockTransport | None = None  # 测试注入点

def _sanitize_body(payload: dict) -> str | None:
    """将请求报文脱敏后返回 JSON 字符串：base64 图片数据替换为占位符，截断到 MAX_BODY_SIZE。"""
    if not payload:
        return None
    try:
        sanitized = _replace_binary(payload)
        text = json.dumps(sanitized, ensure_ascii=False)
        if len(text) > MAX_BODY_SIZE:
            text = text[:MAX_BODY_SIZE] + "\n…[truncated]"
        return text
    except Exception:
        return None


def _replace_binary(node):
    """递归替换 source.data 等 base64 二进制字段为占位符。"""
    if isinstance(node, dict):
        replaced = {}
        for k, v in node.items():
            if k == "data" and isinstance(v, str) and len(v) > 512:
                replaced[k] = f"[base64 data, {len(v)} chars]"
            elif k == "source" and isinstance(v, dict):
                replaced[k] = _replace_binary(v)
            elif k == "url" and isinstance(v, str) and v.startswith("data:"):
                replaced[k] = f"[data URL, {len(v)} chars]"
            else:
                replaced[k] = _replace_binary(v)
        return replaced
    if isinstance(node, list):
        return [_replace_binary(item) for item in node]
    return node


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


def _max_attempts(conn) -> int:
    try:
        n = int(get_setting(conn, "max_attempts", "5"))
        return n if n >= 1 else 5
    except ValueError:
        return 5


def _patterns(conn) -> tuple[list[str] | None, list[str] | None]:
    def load(key: str) -> list[str] | None:
        raw = get_setting(conn, key, "")
        lines = [ln.strip().lower() for ln in raw.splitlines() if ln.strip()]
        return lines or None
    return load("balance_patterns"), load("capability_patterns")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=httpx.Timeout(120.0, connect=15.0))


async def execute(conn, *, entry_protocol: str, group_name: str,
                  payload: dict, stream: bool) -> GatewayResult:
    started = time.time()
    modalities = detect_modalities(payload)
    candidates, skipped = select_candidates(
        conn, group_name, entry_protocol, modalities)
    bp, cp = _patterns(conn)
    log_id = create_log(conn, client_protocol=entry_protocol,
                        group_name=group_name,
                        path="/v1/messages" if entry_protocol == "anthropic"
                        else "/v1/chat/completions",
                        stream=stream,
                        request_body=_sanitize_body(payload))
    for s in skipped:
        add_attempt(conn, log_id, channel_id=s.channel_id,
                    channel_name=s.channel_name,
                    actual_model=s.actual_model,
                    skipped=s.reason)

    if not candidates:
        finish_log(conn, log_id, status="failed",
                   total_ms=_elapsed(started), input_tokens=None, output_tokens=None)
        return GatewayResult(502, body=_error_body(
            entry_protocol, f"模型组 {group_name!r} 没有可用渠道", 502))

    last_status, last_body = 502, {}
    for cand in candidates[:_max_attempts(conn)]:
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
                upstream_payload, started, group_name, bp, cp)
            if result is not None:
                return result
            last_status, last_body = consumed
            continue

        result, consumed = await _try_non_stream(
            conn, log_id, cand, entry_protocol, url, headers,
            upstream_payload, started, group_name, bp, cp)
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
                          upstream_payload, started, group_name,
                          bp=None, cp=None):
    """返回 (GatewayResult | None, (status, body))。None 表示失败已记录可重试。"""
    try:
        async with _client() as client:
            resp = await client.post(url, json=upstream_payload, headers=headers)
    except httpx.HTTPError as e:
        _record_failure(conn, log_id, cand, classify_error(None, str(e)), None, str(e))
        return None, (502, {})

    if resp.status_code < 400:
        body = resp.json()
        u = (usage_mod.from_anthropic_usage(body.get("usage"))
             if cand.protocol == "anthropic"
             else usage_mod.from_openai_usage(body.get("usage")))
        if cand.needs_conversion:
            body = (o2a.response(body, group_name) if entry_protocol == "openai"
                    else a2o.response(body, group_name))
        add_attempt(conn, log_id, channel_id=cand.channel_id,
                    channel_name=cand.channel_name,
                    actual_model=cand.actual_model,
                    converted=cand.needs_conversion, http_status=resp.status_code)
        finish_log(conn, log_id, status="success", total_ms=_elapsed(started),
                   input_tokens=u["input_tokens"],
                   output_tokens=u["output_tokens"],
                   cache_read_tokens=u["cache_read_tokens"],
                   cache_creation_tokens=u["cache_creation_tokens"],
                   response_body=_sanitize_body(body))
        return GatewayResult(resp.status_code, body=body), (resp.status_code, body)

    body_text = resp.text
    verdict = classify_error(resp.status_code, body_text, bp, cp)
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
                      upstream_payload, started, group_name,
                      bp=None, cp=None):
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
        verdict = classify_error(resp.status_code, raw, bp, cp)
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
        # OpenAI 入口的流式采集不在本轮范围，recon 为 None 时保持现状
        recon = (AnthropicStreamReconstructor()
                 if entry_protocol == "anthropic" else None)
        conv_usage = None
        try:
            if not cand.needs_conversion:
                buf = ""
                async for chunk in resp.aiter_text():
                    yield chunk  # 原样转发
                    if recon is None:
                        continue
                    buf += chunk
                    raw_events, buf = _split_sse_events(buf)
                    for raw_event in raw_events:
                        _safe_feed(recon, _safe_parse(raw_event))
            else:
                converter = (stream_o2a.O2AStreamConverter(group_name)
                             if cand.protocol == "anthropic"
                             else stream_a2o.A2OStreamConverter(group_name))
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    raw_events, buf = _split_sse_events(buf)
                    for raw_event in raw_events:
                        obj = _safe_parse(raw_event)
                        if obj is None:
                            continue
                        for event in converter.feed(obj):
                            if recon is not None:
                                _safe_feed(recon, event)
                            yield render_sse([event], entry_protocol)[0]
                for event in converter.finish():
                    if recon is not None:
                        _safe_feed(recon, event)
                    yield render_sse([event], entry_protocol)[0]
                if entry_protocol == "openai":
                    yield "data: [DONE]\n\n"
                if recon is not None:
                    conv_usage = _safe_usage(converter)
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
                if recon is not None:
                    # 旁路采集失败只影响日志里的出参，不能影响状态与转发
                    try:
                        final = recon.to_json()
                        if final is not None and conv_usage is not None:
                            final["usage"] = usage_mod.to_anthropic_usage(
                                conv_usage)
                        response_body = _sanitize_body(final) if final else None
                        u = (conv_usage
                             or usage_mod.from_anthropic_usage(
                                 (final or {}).get("usage")))
                    except Exception:
                        response_body, u = None, usage_mod.empty()
                    finish_log(wconn, log_id, status=status,
                               total_ms=_elapsed(started),
                               input_tokens=u["input_tokens"],
                               output_tokens=u["output_tokens"],
                               cache_read_tokens=u["cache_read_tokens"],
                               cache_creation_tokens=u["cache_creation_tokens"],
                               response_body=response_body)
                else:
                    finish_log(wconn, log_id, status=status,
                               total_ms=_elapsed(started),
                               input_tokens=None, output_tokens=None)
                if failed_mid is not None:
                    verdict = classify_error(None, failed_mid)
                    _record_failure(wconn, log_id, cand, verdict, None,
                                    failed_mid)
            finally:
                wconn.close()

    return GatewayResult(200, events=events()), (200, {})


_SSE_SEPARATORS = ("\r\n\r\n", "\n\n", "\r\r")


def _split_sse_events(buf: str) -> tuple[list[str], str]:
    """按 SSE 空行切分事件块；返回 (完整事件块列表, 未完结的剩余缓冲)。

    兼容 CRLF / LF / CR 三种行尾，避免上游只用 "\\r\\n\\r\\n" 时缓冲无限增长。
    """
    events: list[str] = []
    while True:
        idx, sep_len = -1, 0
        for sep in _SSE_SEPARATORS:
            i = buf.find(sep)
            if i != -1 and (idx == -1 or i < idx):
                idx, sep_len = i, len(sep)
        if idx == -1:
            return events, buf
        events.append(buf[:idx])
        buf = buf[idx + sep_len:]


def _safe_parse(raw_event: str) -> dict | None:
    """解析一个 SSE 事件块；旁路代码出任何问题都只返回 None。"""
    try:
        return parse_sse_event(raw_event)
    except Exception:
        return None


def _safe_feed(recon, event: dict | None) -> None:
    """把事件喂给旁路重建器；解析/重建异常一律吞掉，绝不影响转发。"""
    if event is None:
        return
    try:
        recon.feed(event)
    except Exception:
        pass


def _safe_usage(converter) -> dict | None:
    """取转换器累计用量；异常时返回 None（日志退化为无用量）。"""
    try:
        return converter.usage()
    except Exception:
        return None
