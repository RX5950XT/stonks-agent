"""Typed provider boundary for one snapshot materialization request."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.dataset_snapshot import ProviderSnapshotMaterialization
from stonks_agent.domain.errors import Result


@runtime_checkable
class SnapshotMaterializationSource[RequestT](Protocol):
    def fetch(
        self,
        request: RequestT,
        *,
        provider_policy_id: str,
    ) -> Result[ProviderSnapshotMaterialization]: ...
