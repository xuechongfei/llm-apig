from app.usage import (empty, from_anthropic_usage, from_openai_usage,
                       to_anthropic_usage)


def test_anthropic_usage_maps_cache_fields():
    u = from_anthropic_usage({
        "input_tokens": 12, "output_tokens": 34,
        "cache_read_input_tokens": 56,
        "cache_creation_input_tokens": 78})
    assert u == {"input_tokens": 12, "output_tokens": 34,
                 "cache_read_tokens": 56, "cache_creation_tokens": 78}


def test_anthropic_usage_missing_fields_are_none():
    assert from_anthropic_usage({"input_tokens": 1}) == {
        "input_tokens": 1, "output_tokens": None,
        "cache_read_tokens": None, "cache_creation_tokens": None}
    assert from_anthropic_usage(None) == empty()


def test_openai_usage_cached_tokens_details():
    u = from_openai_usage({
        "prompt_tokens": 100, "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 80}})
    assert u["input_tokens"] == 100 and u["output_tokens"] == 5
    assert u["cache_read_tokens"] == 80
    assert u["cache_creation_tokens"] is None


def test_openai_usage_prompt_cache_hit_fallback():
    u = from_openai_usage({"prompt_tokens": 9, "completion_tokens": 1,
                           "prompt_cache_hit_tokens": 7})
    assert u["cache_read_tokens"] == 7


def test_openai_usage_without_usage_block():
    assert from_openai_usage({}) == empty()


def test_to_anthropic_usage_omits_none():
    assert to_anthropic_usage({
        "input_tokens": 3, "output_tokens": 4,
        "cache_read_tokens": 5, "cache_creation_tokens": None}) == {
        "input_tokens": 3, "output_tokens": 4,
        "cache_read_input_tokens": 5}


def test_anthropic_usage_non_dict_is_empty():
    """非 dict 的 usage 视为空，不得抛异常（采集必须旁路）"""
    for bad in ([1, 2], "oops"):
        assert from_anthropic_usage(bad) == {
            "input_tokens": None, "output_tokens": None,
            "cache_read_tokens": None, "cache_creation_tokens": None}


def test_openai_usage_non_dict_details_is_empty():
    """prompt_tokens_details 非 dict 时缓存读取为 None，其余照常"""
    u = from_openai_usage({"prompt_tokens": 5,
                           "prompt_tokens_details": [1, 2]})
    assert u["input_tokens"] == 5
    assert u["cache_read_tokens"] is None


def test_openai_usage_non_dict_usage_is_empty():
    """非 dict 的 usage 视为空，不得抛异常（采集必须旁路）"""
    assert from_openai_usage(["bad"]) == {
        "input_tokens": None, "output_tokens": None,
        "cache_read_tokens": None, "cache_creation_tokens": None}
