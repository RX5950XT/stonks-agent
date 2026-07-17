"""Atomic storage boundary for API rate-limit decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.rate_limit import RateLimitDecision


@runtime_checkable
class RateLimitStore(Protocol):
    """Atomically consume one request from an exact fixed-window key."""

    def consume(
        self,
        key: str,
        *,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision: ...
