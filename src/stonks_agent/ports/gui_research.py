"""Future durable GUI research workflow boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import Result
from stonks_agent.domain.gui_research import (
    GuiResearchCommand,
    GuiResearchEvidenceView,
    GuiResearchHistoryView,
    GuiResearchRunRef,
    GuiResearchRunView,
)
from stonks_agent.domain.research_run import CanonicalRunEvent


@runtime_checkable
class GuiResearchFacade(Protocol):
    """One narrow port; this slice deliberately supplies no runtime adapter."""

    def submit(
        self,
        principal: LocalPrincipal,
        command: GuiResearchCommand,
    ) -> Result[GuiResearchRunRef]: ...

    def read(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Result[GuiResearchRunView]: ...

    def recent(
        self,
        principal: LocalPrincipal,
        *,
        limit: int,
    ) -> Result[GuiResearchHistoryView]: ...

    def evidence(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Result[GuiResearchEvidenceView]: ...

    def events(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> Result[tuple[CanonicalRunEvent, ...]]: ...
