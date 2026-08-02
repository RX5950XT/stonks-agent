"""Narrow lease-bound forecast boundary used by the research worker."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.research_job import ResearchLeaseInput
from stonks_agent.domain.signal import ForecastOutputArtifact


@runtime_checkable
class ResearchForecastPort(Protocol):
    def forecast(
        self,
        lease: JobLease,
        value: ResearchLeaseInput,
    ) -> Result[ForecastOutputArtifact]: ...
