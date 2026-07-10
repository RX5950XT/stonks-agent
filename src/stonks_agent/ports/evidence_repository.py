"""Canonical evidence repository boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.errors import Result
from stonks_contracts.evidence import EvidenceItem


@runtime_checkable
class EvidenceRepository(Protocol):
    def append(self, item: EvidenceItem) -> Result[EvidenceItem]: ...

    def get(self, evidence_id: UUID) -> Result[EvidenceItem]: ...

    def query_available(
        self,
        *,
        subject: str,
        as_of: datetime,
    ) -> Result[tuple[EvidenceItem, ...]]: ...
