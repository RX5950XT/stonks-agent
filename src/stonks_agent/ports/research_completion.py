"""Exact snapshot-scoped preflight for a research worker lease."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.research_job import ResearchLeaseInput


@runtime_checkable
class ResearchLeasePreflight(Protocol):
    def preflight(
        self,
        lease: JobLease,
        *,
        now: datetime,
    ) -> Result[ResearchLeaseInput]: ...
