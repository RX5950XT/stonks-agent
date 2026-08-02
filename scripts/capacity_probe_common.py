"""Shared safe constants for the bounded capacity probe."""

from __future__ import annotations

from datetime import UTC, datetime

EXPECTED_SCHEMA_REVISION = "0018"
FIXED_NOW = datetime(2026, 7, 1, 21, tzinfo=UTC)


class ProbeError(RuntimeError):
    """Public-safe capacity probe failure."""
