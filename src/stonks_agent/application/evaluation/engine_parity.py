"""Run exact, explainable parity checks across canonical backtest engines."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.application.evaluation.backtest import run_backtest
from stonks_agent.domain.engine_parity import (
    REQUIRED_PARITY_ENGINES,
    EngineParityComparison,
    EngineParityDimension,
    EngineParityReport,
    EngineParityStatus,
    EngineResultEvidence,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.backtest_engine import BacktestEnginePort
from stonks_contracts.backtest import (
    BacktestEngineKind,
    BacktestFill,
    BacktestJob,
    BacktestResult,
)
from stonks_contracts.common import Sha256, UTCDateTime, stable_payload_hash


class EngineParityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[0-9]+$")
    required_engines: tuple[BacktestEngineKind, ...] = Field(
        min_length=3,
        max_length=3,
    )
    canonical_mismatch_threshold: Literal[0] = 0
    warning_mismatch_threshold: int = Field(default=0, ge=0, le=128)

    @model_validator(mode="after")
    def validate_engines(self) -> Self:
        if self.required_engines != REQUIRED_PARITY_ENGINES:
            raise ValueError("parity policy requires the exact stable engine order")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


class EngineParityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: UUID
    jobs: tuple[BacktestJob, ...] = Field(min_length=3, max_length=3)
    policy_hash: Sha256
    requested_at: UTCDateTime
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        engines = tuple(item.runtime.engine for item in self.jobs)
        if set(engines) != set(REQUIRED_PARITY_ENGINES):
            raise ValueError("parity request requires the exact engine set")
        if engines != REQUIRED_PARITY_ENGINES:
            raise ValueError("parity request requires stable engine order")
        if len({item.job_id for item in self.jobs}) != len(self.jobs) or len(
            {item.attempt_nonce for item in self.jobs}
        ) != len(self.jobs):
            raise ValueError("parity jobs require unique operational identities")
        lineage = {
            (item.request_id, item.run_id, item.attempt_generation)
            for item in self.jobs
        }
        if len(lineage) != 1:
            raise ValueError("parity jobs require one operational lineage")
        if len({item.input_hash for item in self.jobs}) != 1:
            raise ValueError("parity jobs must use the same canonical input")
        if not self.requested_at < self.deadline or any(
            item.requested_at != self.requested_at or item.deadline != self.deadline
            for item in self.jobs
        ):
            raise ValueError("parity request timeline does not match its jobs")
        return self

    def validate_policy(self, policy: EngineParityPolicy) -> None:
        if (
            self.policy_hash != policy.policy_hash
            or policy.required_engines != REQUIRED_PARITY_ENGINES
        ):
            raise ValueError("parity request policy hash changed")


def load_engine_parity_policy(path: str | Path) -> EngineParityPolicy:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return EngineParityPolicy.model_validate(payload)
    except (OSError, TypeError, ValidationError, yaml.YAMLError) as error:
        raise ValueError("engine parity policy could not be loaded") from error


def run_engine_parity(
    request: EngineParityRequest,
    policy: EngineParityPolicy,
    engines: Mapping[BacktestEngineKind, BacktestEnginePort],
    *,
    clock: Callable[[], datetime],
) -> Result[EngineParityReport]:
    initial_time = _read_clock(clock)
    if isinstance(initial_time, Failure):
        return initial_time
    binding_failure = _validate_run_binding(request, policy, engines, initial_time)
    if binding_failure is not None:
        return binding_failure
    results: list[BacktestResult] = []
    evaluated_at = initial_time
    for job in request.jobs:
        engine = engines[job.runtime.engine]
        try:
            response = run_backtest(job, engine)
        except Exception:
            return Failure(
                StructuredError(
                    code=ErrorCode.TOOL_FAILED,
                    message="Backtest engine failed during parity evaluation",
                )
            )
        if isinstance(response, Failure):
            return response
        observed_time = _read_clock(clock)
        if isinstance(observed_time, Failure):
            return observed_time
        evaluated_at = observed_time
        if evaluated_at > request.deadline:
            return _deadline_failure()
        results.append(response.value)
    return Success(_build_report(request, policy, tuple(results), evaluated_at))


def _read_clock(clock: Callable[[], datetime]) -> datetime | Failure:
    try:
        observed = clock()
    except Exception:
        return Failure(
            StructuredError(
                code=ErrorCode.TOOL_FAILED,
                message="Engine parity clock failed",
            )
        )
    if observed.tzinfo is None or observed.utcoffset() is None:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Engine parity clock must be timezone-aware",
            )
        )
    return observed


def _validate_run_binding(
    request: EngineParityRequest,
    policy: EngineParityPolicy,
    engines: Mapping[BacktestEngineKind, BacktestEnginePort],
    now: datetime,
) -> Failure | None:
    try:
        request.validate_policy(policy)
    except ValueError:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Engine parity request does not match policy",
            )
        )
    missing = tuple(
        engine for engine in policy.required_engines if engine not in engines
    )
    if missing:
        return Failure(
            StructuredError(
                code=ErrorCode.CAPABILITY_DENIED,
                message="Required backtest engine is disabled",
            )
        )
    if now < request.requested_at:
        return Failure(
            StructuredError(
                code=ErrorCode.INVALID_INPUT,
                message="Engine parity evaluation is not ready",
            )
        )
    if now > request.deadline:
        return _deadline_failure()
    return None


def _deadline_failure() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.DEADLINE_EXCEEDED,
            message="Engine parity evaluation deadline expired",
        )
    )


def _build_report(
    request: EngineParityRequest,
    policy: EngineParityPolicy,
    results: tuple[BacktestResult, ...],
    evaluated_at: datetime,
) -> EngineParityReport:
    reference = results[0]
    evidence = tuple(_evidence(item) for item in results)
    comparisons = tuple(
        sorted(
            (
                comparison
                for candidate in results[1:]
                for comparison in _compare(reference, candidate, policy)
            ),
            key=lambda item: (item.candidate_engine.value, item.dimension.value),
        )
    )
    status = (
        EngineParityStatus.CANONICAL_PARITY
        if all(item.within_threshold for item in comparisons)
        else EngineParityStatus.ENGINE_SPECIFIC
    )
    return EngineParityReport.create(
        evaluation_id=request.evaluation_id,
        input_hash=reference.input_hash,
        dataset_hash=reference.dataset_hash,
        calendar_hash=reference.calendar_hash,
        cost_model_hash=reference.cost_model_hash,
        policy_hash=policy.policy_hash,
        evidence=evidence,
        comparisons=comparisons,
        status=status,
        evaluated_at=evaluated_at,
    )


def _evidence(result: BacktestResult) -> EngineResultEvidence:
    return EngineResultEvidence(
        engine=result.runtime.engine,
        runtime=result.runtime,
        job_hash=result.job_hash,
        result_hash=result.result_hash,
        semantic_hash=result.semantic_hash,
        fill_count=len(result.fills),
        fill_provenance_hash=stable_payload_hash(
            {
                "fills": [
                    {
                        "fill_id": str(item.fill_id),
                        "external_ref": item.external_ref,
                    }
                    for item in result.fills
                ]
            }
        ),
        warning_count=len(result.warnings),
        warnings_hash=stable_payload_hash({"warnings": list(result.warnings)}),
    )


def _compare(
    reference: BacktestResult,
    candidate: BacktestResult,
    policy: EngineParityPolicy,
) -> tuple[EngineParityComparison, ...]:
    result: list[EngineParityComparison] = []
    for dimension in EngineParityDimension:
        left, right = (
            _dimension_tokens(reference, dimension),
            _dimension_tokens(candidate, dimension),
        )
        count, keys = _multiset_difference(left, right)
        threshold = (
            policy.warning_mismatch_threshold
            if dimension is EngineParityDimension.WARNINGS
            else policy.canonical_mismatch_threshold
        )
        result.append(
            EngineParityComparison(
                candidate_engine=candidate.runtime.engine,
                dimension=dimension,
                difference_count=count,
                threshold=threshold,
                within_threshold=count <= threshold,
                evidence_keys=keys,
            )
        )
    return tuple(result)


def _dimension_tokens(
    result: BacktestResult,
    dimension: EngineParityDimension,
) -> tuple[str, ...]:
    if dimension is EngineParityDimension.SEMANTIC_HASH:
        return (result.semantic_hash,)
    if dimension is EngineParityDimension.ORDER_OUTCOMES:
        return _hash_payloads(
            item.model_dump(mode="json") for item in result.order_outcomes
        )
    if dimension is EngineParityDimension.FILL_SCHEDULE:
        return _hash_payloads(_fill_key(item) for item in result.fills)
    if dimension is EngineParityDimension.FILL_QUANTITY:
        return _hash_payloads(
            _fill_key(item) | {"quantity": str(item.quantity)} for item in result.fills
        )
    if dimension is EngineParityDimension.FILL_PRICE:
        return _hash_payloads(
            _fill_key(item) | {"price": str(item.price)} for item in result.fills
        )
    if dimension is EngineParityDimension.FILL_FEES:
        return _hash_payloads(
            _fill_key(item) | {"fees": str(item.fees)} for item in result.fills
        )
    if dimension is EngineParityDimension.FILL_SLIPPAGE:
        return _hash_payloads(
            _fill_key(item) | {"slippage": str(item.slippage)} for item in result.fills
        )
    if dimension is EngineParityDimension.FINAL_CASH:
        return _hash_payloads(
            item.model_dump(mode="json") for item in result.final_cash
        )
    if dimension is EngineParityDimension.FINAL_POSITIONS:
        return _hash_payloads(
            item.model_dump(mode="json") for item in result.final_positions
        )
    if dimension is EngineParityDimension.TOTAL_FEES:
        return (stable_payload_hash({"total_fees": str(result.total_fees)}),)
    return _hash_payloads({"warning": item} for item in result.warnings)


def _fill_key(fill: BacktestFill) -> dict[str, str]:
    return {
        "order_id": str(fill.order_id),
        "instrument_id": str(fill.instrument_id),
        "side": fill.side.value,
        "occurred_at": fill.occurred_at.isoformat(),
        "source_bar_id": str(fill.source_bar_id),
    }


def _hash_payloads(payloads: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    return tuple(sorted(stable_payload_hash(payload) for payload in payloads))


def _multiset_difference(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> tuple[int, tuple[str, ...]]:
    left_counter, right_counter = Counter(left), Counter(right)
    difference = (left_counter - right_counter) + (right_counter - left_counter)
    count = sum(difference.values())
    keys = tuple(sorted(difference))[:64]
    return count, keys
