"""Immutable references and progress state for the canonical paper fund cycle."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.job import JobLease
from stonks_contracts.common import NonEmptyString, Sha256, stable_payload_hash


class PaperCycleStage(StrEnum):
    EVIDENCE = "evidence"
    RESEARCH_OPINION = "research_opinion"
    SIGNAL = "signal"
    PORTFOLIO_TARGET = "portfolio_target"
    RISK_DECISION = "risk_decision"
    ORDER_INTENT = "order_intent"
    EXECUTION_RECEIPT = "execution_receipt"
    LEDGER = "ledger"
    REPORT = "report"


class PaperCycleRunStatus(StrEnum):
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class CanonicalCycleReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref_type: NonEmptyString
    ref_id: NonEmptyString
    content_hash: Sha256


_REFERENCE_RULES: dict[PaperCycleStage, tuple[frozenset[str], frozenset[str]]] = {
    PaperCycleStage.EVIDENCE: (frozenset({"evidence"}), frozenset({"evidence"})),
    PaperCycleStage.RESEARCH_OPINION: (
        frozenset({"research_artifact", "agent_opinion"}),
        frozenset({"research_artifact"}),
    ),
    PaperCycleStage.SIGNAL: (
        frozenset({"alpha_signal"}),
        frozenset({"alpha_signal"}),
    ),
    PaperCycleStage.PORTFOLIO_TARGET: (
        frozenset({"portfolio_target"}),
        frozenset({"portfolio_target"}),
    ),
    PaperCycleStage.RISK_DECISION: (
        frozenset({"risk_decision"}),
        frozenset({"risk_decision"}),
    ),
    PaperCycleStage.ORDER_INTENT: (frozenset({"order_intent"}), frozenset()),
    PaperCycleStage.EXECUTION_RECEIPT: (
        frozenset({"execution_receipt"}),
        frozenset(),
    ),
    PaperCycleStage.LEDGER: (
        frozenset({"ledger_projection"}),
        frozenset({"ledger_projection"}),
    ),
    PaperCycleStage.REPORT: (
        frozenset({"analysis_report"}),
        frozenset({"analysis_report"}),
    ),
}
_SINGLE_REFERENCE_STAGES = frozenset(
    {
        PaperCycleStage.PORTFOLIO_TARGET,
        PaperCycleStage.RISK_DECISION,
        PaperCycleStage.LEDGER,
        PaperCycleStage.REPORT,
    }
)


class PaperCycleStageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PaperCycleStage
    references: tuple[CanonicalCycleReference, ...] = Field(max_length=10_000)
    output_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        stage: PaperCycleStage,
        references: tuple[CanonicalCycleReference, ...],
    ) -> PaperCycleStageOutput:
        ordered = tuple(
            sorted(
                references,
                key=lambda item: (item.ref_type, item.ref_id, item.content_hash),
            )
        )
        values = {"stage": stage, "references": ordered}
        return cls.model_validate(
            values | {"output_hash": stable_payload_hash(_jsonable(values))}
        )

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        keys = tuple((item.ref_type, item.ref_id) for item in self.references)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("cycle references must be unique and stably sorted")
        actual = frozenset(item.ref_type for item in self.references)
        allowed, required = _REFERENCE_RULES[self.stage]
        if not actual <= allowed or not required <= actual:
            raise ValueError("cycle stage reference types are invalid")
        if self.stage in _SINGLE_REFERENCE_STAGES and len(self.references) != 1:
            raise ValueError("cycle stage requires exactly one reference")
        if self.output_hash != self.expected_output_hash():
            raise ValueError("cycle output hash does not match payload")
        return self

    def expected_output_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"output_hash"})
        )


class PaperCycleState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    cycle_input_hash: Sha256
    outputs: tuple[PaperCycleStageOutput, ...] = Field(max_length=len(PaperCycleStage))
    state_hash: Sha256

    @classmethod
    def genesis(cls, run_id: UUID, cycle_input_hash: str) -> PaperCycleState:
        return cls.create(run_id=run_id, cycle_input_hash=cycle_input_hash, outputs=())

    @classmethod
    def create(
        cls,
        *,
        run_id: UUID,
        cycle_input_hash: str,
        outputs: tuple[PaperCycleStageOutput, ...],
    ) -> PaperCycleState:
        values = {
            "run_id": run_id,
            "cycle_input_hash": cycle_input_hash,
            "outputs": outputs,
        }
        return cls.model_validate(
            values | {"state_hash": stable_payload_hash(_jsonable(values))}
        )

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        expected = tuple(PaperCycleStage)[: len(self.outputs)]
        if self.completed_stages != expected:
            raise ValueError("cycle outputs must be the canonical stage prefix")
        if self.state_hash != self.expected_state_hash():
            raise ValueError("cycle state hash does not match payload")
        self._validate_execution_cardinality()
        return self

    @property
    def completed_stages(self) -> tuple[PaperCycleStage, ...]:
        return tuple(item.stage for item in self.outputs)

    @property
    def next_stage(self) -> PaperCycleStage | None:
        stages = tuple(PaperCycleStage)
        return stages[len(self.outputs)] if len(self.outputs) < len(stages) else None

    @property
    def complete(self) -> bool:
        return self.next_stage is None

    def advance(self, output: PaperCycleStageOutput) -> PaperCycleState:
        if output.stage is not self.next_stage:
            raise ValueError("cycle output is not the next canonical stage")
        return self.create(
            run_id=self.run_id,
            cycle_input_hash=self.cycle_input_hash,
            outputs=(*self.outputs, output),
        )

    def expected_state_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"state_hash"}))

    def _validate_execution_cardinality(self) -> None:
        if len(self.outputs) <= tuple(PaperCycleStage).index(
            PaperCycleStage.EXECUTION_RECEIPT
        ):
            return
        orders = self.outputs[
            tuple(PaperCycleStage).index(PaperCycleStage.ORDER_INTENT)
        ]
        receipts = self.outputs[
            tuple(PaperCycleStage).index(PaperCycleStage.EXECUTION_RECEIPT)
        ]
        if len(orders.references) != len(receipts.references):
            raise ValueError("execution receipt count must match order count")


class RunPaperCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease: JobLease
    cycle_input_hash: Sha256

    @model_validator(mode="after")
    def validate_authority(self) -> Self:
        if self.lease.job_type != "paper_fund_cycle":
            raise ValueError("paper cycle requires the canonical job type")
        if self.lease.payload.get("cycle_input_hash") != self.cycle_input_hash:
            raise ValueError("paper cycle input hash differs from leased command")
        return self


class CancelPaperCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    expected_version: int = Field(ge=1)
    actor: NonEmptyString
    reason_code: NonEmptyString


class PaperCycleRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: PaperCycleRunStatus
    state: PaperCycleState
    result_artifact_hash: Sha256 | None
    error_code: NonEmptyString | None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.run_id != self.state.run_id:
            raise ValueError("cycle result run identity changed")
        succeeded = self.status is PaperCycleRunStatus.SUCCEEDED
        if succeeded != (self.result_artifact_hash is not None):
            raise ValueError("only a successful cycle has a result artifact")
        if succeeded and (not self.state.complete or self.error_code is not None):
            raise ValueError("successful cycle result is incomplete")
        if not succeeded and self.error_code is None:
            raise ValueError("non-successful cycle result requires an error code")
        return self


def _jsonable(values: Mapping[str, object]) -> dict[str, object]:
    return {
        key: (
            [item.model_dump(mode="json") for item in value]
            if isinstance(value, tuple)
            else str(value)
            if isinstance(value, UUID)
            else value
        )
        for key, value in values.items()
    }
