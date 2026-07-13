"""Core-owned state-machine and durable checkpoint boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result, StructuredError
from stonks_agent.domain.paper_cycle import (
    CancelPaperCycle,
    PaperCycleRunResult,
    PaperCycleStage,
    PaperCycleStageOutput,
    PaperCycleState,
    RunPaperCycle,
)
from stonks_agent.ports.artifact_store import ArtifactManifest


@runtime_checkable
class PaperCycleStageHandler(Protocol):
    def advance(
        self,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]: ...


@runtime_checkable
class PaperCycleStore(Protocol):
    def load(self, command: RunPaperCycle) -> Result[PaperCycleState]: ...

    def checkpoint(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        *,
        expected_state_hash: str,
    ) -> Result[PaperCycleState]: ...

    def fail(
        self,
        command: RunPaperCycle,
        error: StructuredError,
    ) -> Result[PaperCycleRunResult]: ...

    def complete(
        self,
        command: RunPaperCycle,
        state: PaperCycleState,
        artifact: ArtifactManifest,
    ) -> Result[PaperCycleRunResult]: ...

    def cancel(
        self,
        command: CancelPaperCycle,
    ) -> Result[PaperCycleRunResult]: ...
