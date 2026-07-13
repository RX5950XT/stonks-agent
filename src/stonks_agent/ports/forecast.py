"""Typed forecast boundary for isolated deterministic or stochastic workers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.signal import ForecastOutputArtifact, ForecastRequest


@runtime_checkable
class ForecastPort(Protocol):
    def forecast(self, request: ForecastRequest) -> Result[ForecastOutputArtifact]: ...
