"""Typed isolated research-worker boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.research import ResearchArtifact, ResearchRequest


@runtime_checkable
class ResearchWorkerPort(Protocol):
    def research(self, request: ResearchRequest) -> Result[ResearchArtifact]: ...
