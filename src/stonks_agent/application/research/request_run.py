"""Authorized command/query services for research API and CLI entrypoints."""

from __future__ import annotations

from uuid import UUID

from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.research_run import (
    CanonicalRunEvent,
    ReportProjection,
    ResearchRunRefs,
    ResearchRunRequest,
)
from stonks_agent.ports.research_query import (
    ReportReader,
    ResearchRequestStore,
    RunEventReader,
)


def request_research_run(
    principal: LocalPrincipal,
    request: ResearchRunRequest,
    store: ResearchRequestStore,
) -> Result[ResearchRunRefs]:
    granted = authorize(principal, Permission.RUN_RESEARCH)
    if isinstance(granted, Failure):
        return granted
    return store.submit(request)


def read_run_events(
    principal: LocalPrincipal,
    run_id: UUID,
    *,
    after_sequence: int,
    limit: int,
    reader: RunEventReader,
) -> Result[tuple[CanonicalRunEvent, ...]]:
    granted = authorize(principal, Permission.READ)
    if isinstance(granted, Failure):
        return granted
    return reader.list_after(run_id, after_sequence=after_sequence, limit=limit)


def read_report(
    principal: LocalPrincipal,
    content_hash: str,
    reader: ReportReader,
) -> Result[ReportProjection]:
    granted = authorize(principal, Permission.READ)
    if isinstance(granted, Failure):
        return granted
    return reader.read(content_hash)
