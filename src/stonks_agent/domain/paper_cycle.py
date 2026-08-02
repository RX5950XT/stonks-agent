"""Immutable references and progress state for the canonical paper fund cycle."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.job import JobLease
from stonks_contracts.common import (
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)


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


class PaperCyclePolicyHashes(BaseModel):
    """Exact immutable configuration authority for one paper cycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    research_profile_hash: Sha256
    model_policy_hash: Sha256
    tool_policy_hash: Sha256
    kronos_configuration_hash: Sha256
    portfolio_policy_hash: Sha256
    risk_policy_hash: Sha256
    execution_policy_hash: Sha256
    ledger_policy_hash: Sha256
    report_policy_hash: Sha256


class PaperCycleStageIdentity(BaseModel):
    """Deterministic identity assigned to one canonical cycle stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PaperCycleStage
    stage_id: UUID

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.stage_id.int == 0:
            raise ValueError("paper cycle stage ID cannot be zero")
        return self


class PaperFundCycleInput(BaseModel):
    """Complete frozen authority restored from the leased job payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    snapshot_id: UUID
    research_run_id: UUID
    research_artifact_id: UUID
    account_id: NonEmptyString
    owner_subject: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/+=-]{0,254}$",
    )
    instrument_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    as_of: UTCDateTime
    created_at: UTCDateTime
    deadline_at: UTCDateTime
    execution_mode: Literal["paper"] = "paper"
    execution_model_version: str = Field(pattern=r"^paper-v[0-9]+$")
    policy_hashes: PaperCyclePolicyHashes
    stage_ids: tuple[PaperCycleStageIdentity, ...] = Field(
        min_length=len(PaperCycleStage),
        max_length=len(PaperCycleStage),
    )

    @model_validator(mode="after")
    def validate_authority(self) -> Self:
        if not self.as_of <= self.created_at < self.deadline_at:
            raise ValueError("paper cycle input timeline is invalid")
        primary_ids = (
            self.run_id,
            self.snapshot_id,
            self.research_run_id,
            self.research_artifact_id,
            self.instrument_id,
        )
        if any(value.int == 0 for value in primary_ids):
            raise ValueError("paper cycle authority IDs cannot be zero")
        stages = tuple(item.stage for item in self.stage_ids)
        if stages != tuple(PaperCycleStage):
            raise ValueError("paper cycle stage IDs must follow canonical stage order")
        identities = tuple(item.stage_id for item in self.stage_ids)
        if len(identities) != len(set(identities)):
            raise ValueError("paper cycle stage IDs must be unique")
        return self

    @property
    def cycle_input_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))

    def stage_id(self, stage: PaperCycleStage) -> UUID:
        return self.stage_ids[tuple(PaperCycleStage).index(stage)].stage_id

    def derived_id(self, stage: PaperCycleStage, purpose: str) -> UUID:
        if (
            not purpose
            or purpose.strip() != purpose
            or len(purpose) > 64
            or any(
                not (character.isalnum() or character in "._-") for character in purpose
            )
        ):
            raise ValueError("paper cycle derived ID purpose is invalid")
        return uuid5(self.stage_id(stage), f"stonks:paper-cycle:{purpose}")


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
    cycle_input: PaperFundCycleInput

    @model_validator(mode="before")
    @classmethod
    def restore_cycle_input(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        lease = value.get("lease")
        payload_value: object
        if isinstance(lease, JobLease):
            payload_value = lease.payload
        elif isinstance(lease, Mapping):
            payload_value = lease.get("payload")
        else:
            raise ValueError("paper cycle lease is invalid")
        if not isinstance(payload_value, Mapping):
            raise ValueError("paper cycle lease payload is invalid")
        payload = payload_value
        candidate = payload.get("cycle_input")
        if not isinstance(candidate, Mapping):
            raise ValueError("paper cycle lease payload has no exact input")
        supplied = value.get("cycle_input")
        if supplied is not None:
            supplied_json = (
                supplied.model_dump(mode="json")
                if isinstance(supplied, PaperFundCycleInput)
                else supplied
            )
            if supplied_json != candidate:
                raise ValueError(
                    "paper cycle supplied input differs from lease payload"
                )
        return {**value, "cycle_input": candidate}

    @model_validator(mode="after")
    def validate_authority(self) -> Self:
        if self.lease.job_type != "paper_fund_cycle":
            raise ValueError("paper cycle requires the canonical job type")
        expected_payload = {
            "cycle_input": self.cycle_input.model_dump(mode="json"),
            "cycle_input_hash": self.cycle_input_hash,
        }
        if set(self.lease.payload) != set(expected_payload):
            raise ValueError("paper cycle lease payload is ambiguous")
        if self.lease.payload != expected_payload:
            raise ValueError("paper cycle input differs from leased command")
        if self.cycle_input.run_id != self.lease.run_id:
            raise ValueError("paper cycle run identity differs from leased command")
        if self.cycle_input.deadline_at != self.lease.deadline_at:
            raise ValueError("paper cycle deadline differs from leased command")
        return self

    @property
    def cycle_input_hash(self) -> str:
        return self.cycle_input.cycle_input_hash


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
