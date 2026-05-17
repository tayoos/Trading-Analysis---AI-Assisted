"""
Classify Claude / analysis failures for user-facing messages.
"""
from __future__ import annotations


class AnalysisQuotaError(Exception):
    """Claude subscription usage or token limit exceeded."""


_QUOTA_MARKERS = (
    "out of tokens",
    "token limit",
    "token quota",
    "usage limit",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "insufficient quota",
    "billing",
    "credit balance",
    "credits exhausted",
    "subscription limit",
    "monthly limit",
    "request limit",
    "limit reached",
    "too many requests",
    "overloaded",
    "capacity",
    "max usage",
    "exceeded your",
    "usage cap",
)

_AUTH_MARKERS = (
    "not authenticated",
    "authentication",
    "unauthorized",
    "invalid api key",
    "login required",
    "credentials",
    "claude login",
)


def _error_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return " ".join(parts).lower()


def classify_analysis_error(exc: BaseException) -> tuple[str, str]:
    """
    Returns (kind, user_message).
    kind is one of: quota, auth, generic
    """
    text = _error_text(exc)

    if any(m in text for m in _QUOTA_MARKERS):
        return (
            "quota",
            "Claude usage limit reached — you may be out of tokens or monthly requests. "
            "Check your Claude subscription or wait for limits to reset, then run analysis again.",
        )

    if any(m in text for m in _AUTH_MARKERS):
        return (
            "auth",
            "Claude is not authenticated. Run "
            "`docker exec -it stock-analyzer claude login` and try again.",
        )

    msg = str(exc).strip() if str(exc) else "Analysis failed unexpectedly."
    return "generic", msg


def is_quota_error(exc: BaseException) -> bool:
    return classify_analysis_error(exc)[0] == "quota"
