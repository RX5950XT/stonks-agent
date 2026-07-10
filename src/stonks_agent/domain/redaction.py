"""Pure secret redaction for logs, errors, events, and snapshots."""

from __future__ import annotations

import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|private[_-]?key|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|token)$",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(r"(?i)\bauthorization\s*[:=]\s*bearer\s+[^\s,;]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|key)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def redact(value: object) -> object:
    """Return a redacted copy without modifying the source object."""

    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = _AUTHORIZATION.sub(f"Authorization: {REDACTED}", value)
    redacted = _BEARER.sub(f"Bearer {REDACTED}", redacted)
    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)
    return _OPENAI_KEY.sub(REDACTED, redacted)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "", key)
    return _SENSITIVE_KEY.search(normalized) is not None
