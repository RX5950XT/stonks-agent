"""Secret-safe standard-library logging sink."""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import Any, Literal

from stonks_agent.domain.redaction import (
    DEFAULT_REDACTION_LIMITS,
    REDACTED,
    RedactionLimits,
    redact_text,
)


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
        try:
            rendered = super().format(record)
        except Exception:
            return f"ERROR {REDACTED}"
        return redact_text(
            rendered,
            known_secrets=self._known_secrets,
            limits=self._limits,
        )
