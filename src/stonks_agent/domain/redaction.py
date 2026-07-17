"""Pure, bounded secret redaction and leak detection."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence, Set
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from pydantic import BaseModel, SecretBytes, SecretStr

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|passphrase|privatekey|apikey|"
    r"accesstoken|refreshtoken|idtoken|clientsecret|secret|token|credential)$",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?"
    r"[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s,;]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|passphrase|secret|token|credential|"
    r"api[_-]?key|private[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_CREDENTIAL = re.compile(r"(?i)\bhttps?://[^\s/@:]+:[^\s/@]+@[^\s]+")
_QUERY_CREDENTIAL = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|token|secret)=)[^&#\s]+"
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_PROVIDER_CREDENTIALS = (
    re.compile(r"\bsk-(?:ant-[A-Za-z0-9_-]*|(?:proj-)?)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
)


@dataclass(frozen=True, slots=True)
class RedactionLimits:
    """Resource limits shared by redaction and leak detection."""

    max_depth: int = 12
    max_items: int = 10_000
    max_string_length: int = 131_072
    max_bytes_length: int = 131_072

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (
                self.max_depth,
                self.max_items,
                self.max_string_length,
                self.max_bytes_length,
            )
        ):
            raise ValueError("redaction limits must be positive integers")


DEFAULT_REDACTION_LIMITS = RedactionLimits()


class SecretLeakDetected(ValueError):
    """Raised when a value cannot be proven free of secret material."""

    def __init__(self) -> None:
        super().__init__("secret material detected")


@dataclass(slots=True)
class _Traversal:
    limits: RedactionLimits
    known_secrets: tuple[str, ...]
    remaining_items: int
    active: set[int]


def redact(
    value: object,
    *,
    known_secrets: Collection[str | bytes] = (),
    limits: RedactionLimits = DEFAULT_REDACTION_LIMITS,
) -> object:
    """Return a bounded redacted copy without modifying the source object."""

    traversal = _Traversal(
        limits=limits,
        known_secrets=_normalize_known_secrets(known_secrets, limits),
        remaining_items=limits.max_items,
        active=set(),
    )
    return _redact(value, depth=0, traversal=traversal)


def redact_text(
    value: str,
    *,
    known_secrets: Collection[str | bytes] = (),
    limits: RedactionLimits = DEFAULT_REDACTION_LIMITS,
) -> str:
    """Redact one bounded text value, including explicit known credentials."""

    if len(value) > limits.max_string_length:
        return TRUNCATED
    secrets = _normalize_known_secrets(known_secrets, limits)
    redacted = _redact_known(value, secrets)
    redacted = _PRIVATE_KEY.sub(REDACTED, redacted)
    redacted = _URL_CREDENTIAL.sub(REDACTED, redacted)
    redacted = _QUERY_CREDENTIAL.sub(
        lambda match: f"{match.group(1)}{REDACTED}", redacted
    )
    redacted = _AUTHORIZATION.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = _BEARER.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)
    redacted = _JWT.sub(REDACTED, redacted)
    for pattern in _PROVIDER_CREDENTIALS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def ensure_secret_free(
    value: object,
    *,
    known_secrets: Collection[str | bytes] = (),
    limits: RedactionLimits = DEFAULT_REDACTION_LIMITS,
) -> None:
    """Fail closed if a canonical value contains or may conceal a secret."""

    traversal = _Traversal(
        limits=limits,
        known_secrets=_normalize_known_secrets(known_secrets, limits),
        remaining_items=limits.max_items,
        active=set(),
    )
    _ensure_secret_free(value, depth=0, traversal=traversal)


def _redact(value: object, *, depth: int, traversal: _Traversal) -> object:
    if depth > traversal.limits.max_depth:
        return TRUNCATED
    if isinstance(value, str):
        return _redact_text_with_traversal(value, traversal)
    if isinstance(value, (bytes, bytearray, memoryview, SecretBytes, SecretStr)):
        return REDACTED
    if isinstance(value, BaseException):
        return {
            "error_type": type(value).__name__,
            "message": _redact_text_with_traversal(str(value), traversal),
        }
    if isinstance(value, BaseModel):
        return _redact_container(
            value,
            value.model_dump(mode="python"),
            depth=depth,
            traversal=traversal,
        )
    if is_dataclass(value) and not isinstance(value, type):
        candidate = {field.name: getattr(value, field.name) for field in fields(value)}
        return _redact_container(
            value,
            candidate,
            depth=depth,
            traversal=traversal,
        )
    if isinstance(value, Mapping):
        return _redact_container(
            value,
            value,
            depth=depth,
            traversal=traversal,
        )
    if isinstance(value, Set):
        return _redact_set(value, depth=depth, traversal=traversal)
    if isinstance(value, Sequence):
        return _redact_sequence(value, depth=depth, traversal=traversal)
    return value


def _redact_container(
    identity: object,
    value: Mapping[Any, Any],
    *,
    depth: int,
    traversal: _Traversal,
) -> object:
    if not _enter(identity, len(value), traversal):
        return TRUNCATED
    try:
        result: dict[str, object] = {}
        for key, item in value.items():
            rendered_key = _redact_text_with_traversal(str(key), traversal)
            result[rendered_key] = (
                REDACTED
                if _is_sensitive_key(str(key)) and not _already_safe(item)
                else _redact(item, depth=depth + 1, traversal=traversal)
            )
        return result
    finally:
        traversal.active.remove(id(identity))


def _redact_sequence(
    value: Sequence[object], *, depth: int, traversal: _Traversal
) -> object:
    if not _enter(value, len(value), traversal):
        return TRUNCATED
    try:
        items = [_redact(item, depth=depth + 1, traversal=traversal) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    finally:
        traversal.active.remove(id(value))


def _redact_set(value: Set[object], *, depth: int, traversal: _Traversal) -> object:
    if not _enter(value, len(value), traversal):
        return TRUNCATED
    try:
        items = (_redact(item, depth=depth + 1, traversal=traversal) for item in value)
        return tuple(sorted(items, key=repr))
    finally:
        traversal.active.remove(id(value))


def _ensure_secret_free(value: object, *, depth: int, traversal: _Traversal) -> None:
    if depth > traversal.limits.max_depth:
        raise SecretLeakDetected
    if isinstance(value, str):
        _ensure_text_secret_free(value, traversal)
        return
    if isinstance(value, (bytes, bytearray, memoryview, SecretBytes, SecretStr)):
        raise SecretLeakDetected
    if isinstance(value, BaseException):
        _ensure_text_secret_free(str(value), traversal)
        return
    if isinstance(value, BaseModel):
        _ensure_mapping(
            value,
            value.model_dump(mode="python"),
            depth=depth,
            traversal=traversal,
        )
        return
    if is_dataclass(value) and not isinstance(value, type):
        candidate = {field.name: getattr(value, field.name) for field in fields(value)}
        _ensure_mapping(
            value,
            candidate,
            depth=depth,
            traversal=traversal,
        )
        return
    if isinstance(value, Mapping):
        _ensure_mapping(value, value, depth=depth, traversal=traversal)
        return
    if isinstance(value, (Set, Sequence)):
        _ensure_sequence(value, depth=depth, traversal=traversal)


def _ensure_mapping(
    identity: object,
    value: Mapping[Any, Any],
    *,
    depth: int,
    traversal: _Traversal,
) -> None:
    _require_enter(identity, len(value), traversal)
    try:
        for key, item in value.items():
            rendered_key = str(key)
            _ensure_text_secret_free(rendered_key, traversal)
            if _is_sensitive_key(rendered_key) and not _already_safe(item):
                raise SecretLeakDetected
            _ensure_secret_free(item, depth=depth + 1, traversal=traversal)
    finally:
        traversal.active.remove(id(identity))


def _ensure_sequence(
    value: Set[object] | Sequence[object], *, depth: int, traversal: _Traversal
) -> None:
    _require_enter(value, len(value), traversal)
    try:
        for item in value:
            _ensure_secret_free(item, depth=depth + 1, traversal=traversal)
    finally:
        traversal.active.remove(id(value))


def _redact_text_with_traversal(value: str, traversal: _Traversal) -> str:
    return redact_text(
        value,
        known_secrets=traversal.known_secrets,
        limits=traversal.limits,
    )


def _ensure_text_secret_free(value: str, traversal: _Traversal) -> None:
    if len(value) > traversal.limits.max_string_length:
        raise SecretLeakDetected
    if _redact_text_with_traversal(value, traversal) != value:
        raise SecretLeakDetected


def _enter(identity: object, count: int, traversal: _Traversal) -> bool:
    if id(identity) in traversal.active or count > traversal.remaining_items:
        return False
    traversal.remaining_items -= count
    traversal.active.add(id(identity))
    return True


def _require_enter(identity: object, count: int, traversal: _Traversal) -> None:
    if not _enter(identity, count, traversal):
        raise SecretLeakDetected


def _normalize_known_secrets(
    values: Collection[str | bytes], limits: RedactionLimits
) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        if isinstance(value, bytes):
            if len(value) > limits.max_bytes_length:
                raise ValueError("known secret exceeds redaction bounds")
            try:
                candidate = value.decode("utf-8")
            except UnicodeDecodeError:
                continue
        elif isinstance(value, str):
            candidate = value
        else:
            raise TypeError("known secrets must be strings or bytes")
        if len(candidate) > limits.max_string_length:
            raise ValueError("known secret exceeds redaction bounds")
        if candidate:
            normalized.add(candidate)
    return tuple(sorted(normalized, key=lambda item: (-len(item), item)))


def _redact_known(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    return value


def _already_safe(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and value in {"", REDACTED, TRUNCATED}
    )


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9]", "", key)
    return _SENSITIVE_KEY.search(normalized) is not None
