from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    INSUFFICIENT_BALANCE = "insufficient_balance"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    SERVER = "server"
    CLIENT = "client"
    UNCLASSIFIED = "unclassified"


@dataclass
class Verdict:
    category: ErrorCategory
    retryable: bool
    cooldown_seconds: int
    summary: str


BALANCE_PATTERNS = [
    "insufficient balance", "insufficient_quota", "insufficient user quota",
    "quota exceeded", "exceeded your current quota", "balance is not enough",
    "余额不足", "欠费", "账户余额", "arrears",
]

CAPABILITY_PATTERNS = [
    "does not support image", "not support image", "image input is not supported",
    "does not support video", "not support video", "video input is not supported",
    "does not support vision", "multimodal input is not supported",
    "invalid image data", "unsupported content type",
]


def _matches(body_lower: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if p in body_lower:
            return p
    return None


def classify_error(status: int | None, body: str,
                   balance_patterns: list[str] | None = None,
                   capability_patterns: list[str] | None = None) -> Verdict:
    balance_patterns = balance_patterns or BALANCE_PATTERNS
    capability_patterns = capability_patterns or CAPABILITY_PATTERNS
    body_lower = (body or "").lower()

    if status is None:
        return Verdict(ErrorCategory.SERVER, True, 60, f"network error: {body[:200]}")

    hit = _matches(body_lower, balance_patterns)
    if status == 402 or hit:
        return Verdict(ErrorCategory.INSUFFICIENT_BALANCE, True, 600,
                       f"balance insufficient (matched: {hit or 'http 402'})")

    hit = _matches(body_lower, capability_patterns)
    if status == 400 and hit:
        return Verdict(ErrorCategory.CAPABILITY_UNSUPPORTED, True, 0,
                       f"capability unsupported (matched: {hit})")

    if status == 429:
        return Verdict(ErrorCategory.RATE_LIMIT, True, 60, "rate limited")
    if status in (401, 403):
        return Verdict(ErrorCategory.AUTH, True, 1800, f"auth failed: {body[:200]}")
    if status == 400 or status == 404 or status == 422:
        return Verdict(ErrorCategory.CLIENT, False, 0, f"client error: {body[:200]}")
    if status >= 500:
        return Verdict(ErrorCategory.SERVER, True, 60, f"upstream server error: {status}")

    return Verdict(ErrorCategory.UNCLASSIFIED, False, 0,
                   f"unclassified error (status={status}): {body[:200]}")
