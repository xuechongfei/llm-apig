from app.errors import ErrorCategory, classify_error


def test_network_error_retryable():
    v = classify_error(None, "connect timeout")
    assert v.category == ErrorCategory.SERVER and v.retryable and v.cooldown_seconds == 60


def test_balance_402():
    v = classify_error(402, '{"error":{"message":"Insufficient Balance"}}')
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE
    assert v.retryable and v.cooldown_seconds == 600


def test_balance_in_403_body():
    v = classify_error(403, "User quota is not enough, 余额不足")
    assert v.category == ErrorCategory.INSUFFICIENT_BALANCE


def test_rate_limit():
    v = classify_error(429, "rate limit reached")
    assert v.category == ErrorCategory.RATE_LIMIT and v.cooldown_seconds == 60


def test_auth():
    v = classify_error(401, "invalid api key")
    assert v.category == ErrorCategory.AUTH and v.cooldown_seconds == 1800


def test_capability_unsupported():
    v = classify_error(400, "this model does not support image input")
    assert v.category == ErrorCategory.CAPABILITY_UNSUPPORTED
    assert v.retryable and v.cooldown_seconds == 0


def test_client_error_not_retryable():
    v = classify_error(400, "max_tokens must be positive")
    assert v.category == ErrorCategory.CLIENT and not v.retryable


def test_server_5xx():
    v = classify_error(502, "bad gateway")
    assert v.category == ErrorCategory.SERVER and v.retryable


def test_unclassified_not_retryable():
    v = classify_error(418, "I'm a teapot")
    assert v.category == ErrorCategory.UNCLASSIFIED and not v.retryable
