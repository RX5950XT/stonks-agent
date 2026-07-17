"""Secret-safe standard-library logging sink."""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from copy import copy
from typing import Any, Literal

from stonks_agent.adapters.observability.context import current_trace_context
from stonks_agent.domain.redaction import (
    DEFAULT_REDACTION_LIMITS,
    REDACTED,
    TRUNCATED,
    RedactionLimits,
    redact_text,
)

_CORRELATION_FIELDS = ("trace_id", "span_id", "request_id", "run_id", "job_id")


class RedactingFormatter(logging.Formatter):
    """Format a record and redact the complete rendered output before emission."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: Literal["%", "{", "$"] = "%",
        validate: bool = True,
        *,
        defaults: Mapping[str, Any] | None = None,
        known_secrets: Collection[str | bytes] = (),
        limits: RedactionLimits = DEFAULT_REDACTION_LIMITS,
    ) -> None:
        super().__init__(fmt, datefmt, style, validate, defaults=defaults)
        self._known_secrets = tuple(known_secrets)
        self._limits = limits

    def format(self, record: logging.LogRecord) -> str:
        enriched = copy(record)
        context = current_trace_context()
        attributes = context.correlation_attributes() if context is not None else {}
        for name in _CORRELATION_FIELDS:
            setattr(enriched, name, attributes.get(name, "-"))
        try:
            rendered = super().format(enriched)
        except Exception:
            return f"ERROR {REDACTED}"
        redacted = redact_text(
            rendered,
            known_secrets=self._known_secrets,
            limits=self._limits,
        )
        escaped = _escape_control_characters(redacted)
        return escaped if len(escaped) <= self._limits.max_string_length else TRUNCATED


def _escape_control_characters(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character.isprintable():
            escaped.append(character)
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)
