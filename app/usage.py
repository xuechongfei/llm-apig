"""上游 usage 与网关统一字段之间的换算。

Anthropic 与 OpenAI 的输入语义不同：
- Anthropic：input_tokens 不含缓存部分，三者并列；
- OpenAI：prompt_tokens 含缓存部分。
两者都按上游原样记录 input_tokens，不做加减换算，避免误导。
"""

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
          "cache_creation_tokens")


def empty() -> dict:
    return dict.fromkeys(FIELDS)


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value) -> dict:
    """非 dict 一律视为空，保证采集旁路不影响转发。"""
    return value if isinstance(value, dict) else {}


def from_anthropic_usage(usage: dict | None) -> dict:
    usage = _as_dict(usage)
    return {
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
        "cache_read_tokens": _int_or_none(
            usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": _int_or_none(
            usage.get("cache_creation_input_tokens")),
    }


def from_openai_usage(usage: dict | None) -> dict:
    usage = _as_dict(usage)
    details = _as_dict(usage.get("prompt_tokens_details"))
    cached = details.get("cached_tokens")
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens")
    return {
        "input_tokens": _int_or_none(usage.get("prompt_tokens")),
        "output_tokens": _int_or_none(usage.get("completion_tokens")),
        "cache_read_tokens": _int_or_none(cached),
        "cache_creation_tokens": None,
    }


def to_anthropic_usage(usage: dict) -> dict:
    """归一化字典 → Anthropic wire 形，None 值省略。"""
    pairs = (("input_tokens", "input_tokens"),
             ("output_tokens", "output_tokens"),
             ("cache_read_input_tokens", "cache_read_tokens"),
             ("cache_creation_input_tokens", "cache_creation_tokens"))
    return {wire: usage[key] for wire, key in pairs
            if usage.get(key) is not None}
