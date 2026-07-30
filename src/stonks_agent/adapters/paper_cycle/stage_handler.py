"""Exact, artifact-backed production adapter for the nine-stage paper cycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ValidationError

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.paper_cycle import (
    CanonicalCycleReference,
    PaperCycleStage,
    PaperCycleStageOutput,
    PaperCycleState,
    RunPaperCycle,
)
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.common import canonical_json, stable_payload_hash
from stonks_contracts.evidence import Sensitivity


@dataclass(frozen=True, slots=True)
class PaperCycleStageValue:
    """One typed canonical object produced by a trusted stage processor."""

    ref_type: str
    ref_id: str
    value: BaseModel

    def __post_init__(self) -> None:
        if (
            not self.ref_type
            or self.ref_type.strip() != self.ref_type
            or not self.ref_id
            or self.ref_id.strip() != self.ref_id
        ):
            raise ValueError("paper cycle stage reference identity is invalid")


type PaperCycleStageProcessor = Callable[
    [RunPaperCycle, PaperCycleState],
    Result[tuple[PaperCycleStageValue, ...]],
]
type PaperCycleClock = Callable[[], datetime]


class ArtifactBackedPaperCycleStageHandler:
    """Dispatch exact stage services and archive outputs before checkpointing.

    The processors are the composition boundary for deterministic portfolio,
    risk, reservation, paper execution, ledger, and reporting services. This
    adapter owns no strategy authority: a disabled or non-paper-eligible signal
    processor must return a typed failure and no later stage is invoked.
    """

    __slots__ = ("_artifacts", "_clock", "_processors")

    def __init__(
        self,
        *,
        processors: Mapping[PaperCycleStage, PaperCycleStageProcessor],
        artifacts: ArtifactStore,
        clock: PaperCycleClock,
    ) -> None:
        if set(processors) != set(PaperCycleStage):
            raise ValueError(
                "paper cycle processors must contain the exact canonical stage set"
            )
        self._processors = dict(processors)
        self._artifacts = artifacts
        self._clock = clock

    def advance(
        self,
        command: RunPaperCycle,
        stage: PaperCycleStage,
        state: PaperCycleState,
    ) -> Result[PaperCycleStageOutput]:
        binding_error = _state_binding_error(command, stage, state)
        if binding_error is not None:
            return binding_error
        now = self._clock()
        if now >= command.lease.lease_until or now >= command.cycle_input.deadline_at:
            return _failure(
                ErrorCode.DEADLINE_EXCEEDED,
                "Paper cycle stage lease is not active",
            )
        try:
            produced = self._processors[stage](command, state)
        except Exception:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Paper cycle stage processor failed",
            )
        if isinstance(produced, Failure):
            return produced
        return self._archive_output(
            command=command,
            stage=stage,
            values=produced.value,
            finalized_at=now,
        )

    def _archive_output(
        self,
        *,
        command: RunPaperCycle,
        stage: PaperCycleStage,
        values: tuple[PaperCycleStageValue, ...],
        finalized_at: datetime,
    ) -> Result[PaperCycleStageOutput]:
        references: list[CanonicalCycleReference] = []
        for value in values:
            archived = self._archive_value(
                command=command,
                stage=stage,
                value=value,
                finalized_at=finalized_at,
            )
            if isinstance(archived, Failure):
                return archived
            references.append(archived.value)
        try:
            return Success(
                PaperCycleStageOutput.create(
                    stage=stage,
                    references=tuple(references),
                )
            )
        except (TypeError, ValueError, ValidationError):
            return _failure(
                ErrorCode.CONFLICT,
                "Paper cycle stage output violates the canonical contract",
            )

    def _archive_value(
        self,
        *,
        command: RunPaperCycle,
        stage: PaperCycleStage,
        value: PaperCycleStageValue,
        finalized_at: datetime,
    ) -> Result[CanonicalCycleReference]:
        payload = value.value.model_dump(mode="json")
        content = canonical_json(payload).encode("utf-8")
        finalized = self._artifacts.finalize(
            content,
            metadata=ArtifactMetadata(
                media_type="application/json",
                license_tag="Apache-2.0",
                sensitivity=Sensitivity.INTERNAL,
                source="canonical-paper-cycle",
                attributes=tuple(
                    sorted(
                        (
                            ("ref_id", value.ref_id),
                            ("ref_type", value.ref_type),
                            ("run_id", str(command.lease.run_id)),
                            ("stage", stage.value),
                        )
                    )
                ),
            ),
            finalized_at=finalized_at,
        )
        if isinstance(finalized, Failure):
            return finalized
        if finalized.value.content_hash != stable_payload_hash(payload):
            return _failure(
                ErrorCode.CONFLICT,
                "Paper cycle artifact hash does not bind the typed object",
            )
        try:
            return Success(
                CanonicalCycleReference(
                    ref_type=value.ref_type,
                    ref_id=value.ref_id,
                    content_hash=finalized.value.content_hash,
                )
            )
        except (TypeError, ValueError, ValidationError):
            return _failure(
                ErrorCode.INVALID_INPUT,
                "Paper cycle stage reference is invalid",
            )


def _state_binding_error(
    command: RunPaperCycle,
    stage: PaperCycleStage,
    state: PaperCycleState,
) -> Failure | None:
    if (
        state.run_id != command.lease.run_id
        or state.cycle_input_hash != command.cycle_input_hash
        or state.state_hash != state.expected_state_hash()
        or state.next_stage is not stage
    ):
        return _failure(
            ErrorCode.CONFLICT,
            "Paper cycle stage does not match the active checkpoint",
        )
    return None


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
