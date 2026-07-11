"""Atomic snapshot run/job request boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.snapshot import CreateSnapshotRequest, SnapshotJobRefs


@runtime_checkable
class SnapshotRequestStore(Protocol):
    def submit(self, request: CreateSnapshotRequest) -> Result[SnapshotJobRefs]: ...
