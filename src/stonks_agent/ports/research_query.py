"""Research request and read-model boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_agent.domain.research_run import (
    CanonicalRunEvent,
    ReportProjection,
    ResearchRunRefs,
    ResearchRunRequest,
)


@runtime_checkable
class ResearchRequestStore(Protocol):
    def submit(self, request: ResearchRunRequest) -> Result[ResearchRunRefs]: ...

    def snapshot_owner(self, snapshot_id: UUID) -> Result[str]: ...


@runtime_checkable
class RunEventReader(Protocol):
    def owner_subject(self, run_id: UUID) -> Result[str]: ...

    def list_after(
        self, run_id: UUID, *, after_sequence: int, limit: int
    ) -> Result[tuple[CanonicalRunEvent, ...]]: ...


@runtime_checkable
class ReportReader(Protocol):
    def owner_subject(self, content_hash: str) -> Result[str]: ...

    def read(self, content_hash: str) -> Result[ReportProjection]: ...
