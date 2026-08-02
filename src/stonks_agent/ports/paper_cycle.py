"""Core-owned state-machine and durable checkpoint boundaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from stonks_agent.domain.errors import Result, StructuredError
from stonks_agent.domain.paper_cycle import (
    CancelPaperCycle,
    CanonicalCycleReference,
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
        command: RunPaperCycle,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]: ...


@runtime_checkable
class PaperCycleObjectResolver(Protocol):
    """Durably resolve and revalidate one exact canonical stage object.

    Implementations must load from persistent storage, validate ``object_type``,
    and reject unless both callbacks exactly match ``reference.ref_id`` and
    ``reference.content_hash``.
    """

    def resolve[T: BaseModel](
        self,
        reference: CanonicalCycleReference,
        *,
        object_type: type[T],
        object_id: Callable[[T], str],
        semantic_hash: Callable[[T], str],
    ) -> Result[T]: ...


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
