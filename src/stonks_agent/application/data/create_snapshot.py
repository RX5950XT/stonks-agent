"""Authorized request path for durable snapshot ingestion."""

from __future__ import annotations

from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.snapshot import CreateSnapshotRequest, SnapshotJobRefs
from stonks_agent.ports.snapshot_request import SnapshotRequestStore


def request_snapshot(
    principal: LocalPrincipal,
    request: CreateSnapshotRequest,
    store: SnapshotRequestStore,
) -> Result[SnapshotJobRefs]:
    grant = authorize(principal, Permission.RUN_RESEARCH)
    if isinstance(grant, Failure):
        return grant
    return store.submit(request)
