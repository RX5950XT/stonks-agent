"""Authorized command/query services for research API and CLI entrypoints."""

from __future__ import annotations

from uuid import UUID

from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    Permission,
    ResourceKind,
    authorize,
    authorize_owned_target,
)
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
    if request.owner_subject != principal.subject:
        return _forbidden("Research owner must match authenticated principal")
    owner = store.snapshot_owner(request.snapshot_id)
    if isinstance(owner, Failure):
        return owner
    target = AccessTarget(
        kind=ResourceKind.SNAPSHOT,
        identifier=str(request.snapshot_id),
    )
    scoped = authorize_owned_target(
        principal,
        Permission.RUN_RESEARCH,
        target,
        owner.value,
    )
    if isinstance(scoped, Failure):
        return scoped
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
    owner = reader.owner_subject(run_id)
    if isinstance(owner, Failure):
        return owner
    scoped = authorize_owned_target(
        principal,
        Permission.READ,
        AccessTarget(kind=ResourceKind.RESEARCH_RUN, identifier=str(run_id)),
        owner.value,
    )
    if isinstance(scoped, Failure):
        return scoped
    return reader.list_after(run_id, after_sequence=after_sequence, limit=limit)


def read_report(
    principal: LocalPrincipal,
    content_hash: str,
    reader: ReportReader,
) -> Result[ReportProjection]:
    granted = authorize(principal, Permission.READ)
    if isinstance(granted, Failure):
        return granted
    owner = reader.owner_subject(content_hash)
    if isinstance(owner, Failure):
        return owner
    scoped = authorize_owned_target(
        principal,
        Permission.READ,
        AccessTarget(kind=ResourceKind.REPORT, identifier=content_hash),
        owner.value,
    )
    if isinstance(scoped, Failure):
        return scoped
    return reader.read(content_hash)


def _forbidden(message: str) -> Failure:
    from stonks_agent.domain.errors import ErrorCode, StructuredError

    return Failure(StructuredError(code=ErrorCode.FORBIDDEN, message=message))
