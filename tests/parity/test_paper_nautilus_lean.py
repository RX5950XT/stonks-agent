from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.application.evaluation.engine_parity import (
    EngineParityPolicy,
    EngineParityRequest,
    load_engine_parity_policy,
    run_engine_parity,
)
from stonks_agent.domain.engine_parity import (
    EngineParityDimension,
    EngineParityStatus,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_contracts.backtest import (
    BacktestEngineKind,
    BacktestJob,
    BacktestResult,
)

from .fixtures.canonical import (
    DEADLINE,
    REQUESTED_AT,
    job,
    jobs,
    result,
)

POLICY_PATH = "config/policies/engine_parity_v1.yaml"
EVALUATION_ID = UUID("30000000-0000-4000-8000-000000000001")


class StaticEngine:
    def __init__(
        self,
        response: BacktestResult | Callable[[BacktestJob], BacktestResult],
    ) -> None:
        self.response = response
        self.jobs: list[BacktestJob] = []

    def run(self, selected_job: BacktestJob) -> Result[BacktestResult]:
        self.jobs.append(selected_job)
        if callable(self.response):
            return Success(self.response(selected_job))
        return Success(self.response)


class RaisingEngine:
    def run(self, selected_job: BacktestJob) -> Result[BacktestResult]:
        del selected_job
        raise RuntimeError("private engine failure")


def policy() -> EngineParityPolicy:
    return load_engine_parity_policy(POLICY_PATH)


def request(selected_policy: EngineParityPolicy | None = None) -> EngineParityRequest:
    active = selected_policy or policy()
    return EngineParityRequest(
        evaluation_id=EVALUATION_ID,
        jobs=jobs(),
        policy_hash=active.policy_hash,
        requested_at=REQUESTED_AT,
        deadline=DEADLINE,
    )


def engines(
    *,
    warnings: dict[BacktestEngineKind, tuple[str, ...]] | None = None,
) -> dict[BacktestEngineKind, StaticEngine]:
    selected = warnings or {}
    return {
        engine: StaticEngine(
            lambda selected_job, engine=engine: result(
                selected_job,
                warnings=selected.get(engine, ()),
            )
        )
        for engine in BacktestEngineKind
    }


def test_policy_is_frozen_exact_and_content_hashed() -> None:
    selected = policy()

    assert selected.required_engines == tuple(BacktestEngineKind)
    assert selected.canonical_mismatch_threshold == 0
    assert selected.warning_mismatch_threshold == 0
    assert selected.policy_hash == policy().policy_hash
    with pytest.raises(ValidationError):
        EngineParityPolicy.model_validate(
            selected.model_dump(mode="json") | {"unexpected": True}
        )
    with pytest.raises(ValidationError):
        selected.warning_mismatch_threshold = 1  # type: ignore[misc]


def test_malformed_policy_is_rejected_without_raw_parser_error(tmp_path) -> None:
    target = tmp_path / "parity.yaml"
    target.write_text("required_engines: [", encoding="utf-8")

    with pytest.raises(ValueError, match="could not be loaded") as raised:
        load_engine_parity_policy(target)

    assert "ParserError" not in str(raised.value)


def test_request_requires_exact_engines_input_policy_and_timeline() -> None:
    active = policy()
    original = request(active)

    with pytest.raises(ValidationError, match="stable engine order"):
        EngineParityRequest.model_validate(
            original.model_dump(mode="json")
            | {"jobs": list(reversed(original.model_dump(mode="json")["jobs"]))}
        )
    with pytest.raises(ValidationError, match=r"at least 3|exact engine set"):
        EngineParityRequest.model_validate(
            original.model_dump(mode="json")
            | {"jobs": original.model_dump(mode="json")["jobs"][:-1]}
        )
    with pytest.raises(ValueError, match="policy hash"):
        EngineParityRequest.model_validate(
            original.model_dump(mode="json") | {"policy_hash": "f" * 64}
        ).validate_policy(active)
    with pytest.raises(ValidationError, match="timeline"):
        EngineParityRequest.model_validate(
            original.model_dump(mode="json")
            | {"deadline": original.requested_at.isoformat()}
        )


def test_exact_results_are_canonical_parity_despite_provenance_differences() -> None:
    active = policy()
    selected_request = request(active)

    first = run_engine_parity(
        selected_request,
        active,
        engines(),
        clock=lambda: REQUESTED_AT + timedelta(minutes=2),
    )
    second = run_engine_parity(
        selected_request,
        active,
        dict(reversed(tuple(engines().items()))),
        clock=lambda: REQUESTED_AT + timedelta(minutes=2),
    )

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value.status is EngineParityStatus.CANONICAL_PARITY
    assert first.value.claim_scope == "fixture_canonical_semantics_only"
    assert first.value.normalization_scope == "adapter_normalized_not_native_matching"
    assert first.value.parity_hash == second.value.parity_hash
    assert first.value.evidence == second.value.evidence
    assert all(item.within_threshold for item in first.value.comparisons)
    assert tuple(item.engine for item in first.value.evidence) == tuple(
        BacktestEngineKind
    )
    assert len({item.result_hash for item in first.value.evidence}) == 3
    assert len({item.semantic_hash for item in first.value.evidence}) == 1
    assert all(item.fill_count == 1 for item in first.value.evidence)
    assert len({item.fill_provenance_hash for item in first.value.evidence}) == 3


def test_warning_difference_is_engine_specific_without_raw_warning_text() -> None:
    active = policy()
    warning = "native lifecycle normalized private detail"
    response = run_engine_parity(
        request(active),
        active,
        engines(warnings={BacktestEngineKind.NAUTILUS: (warning,)}),
        clock=lambda: REQUESTED_AT + timedelta(minutes=2),
    )

    assert isinstance(response, Success)
    assert response.value.status is EngineParityStatus.ENGINE_SPECIFIC
    warning_checks = tuple(
        item
        for item in response.value.comparisons
        if item.dimension is EngineParityDimension.WARNINGS
    )
    assert any(not item.within_threshold for item in warning_checks)
    assert warning not in response.value.model_dump_json()


def test_nonzero_warning_threshold_has_inclusive_boundary() -> None:
    active = policy().model_copy(update={"warning_mismatch_threshold": 1})
    active = EngineParityPolicy.model_validate(active.model_dump(mode="json"))
    response = run_engine_parity(
        request(active),
        active,
        engines(
            warnings={
                BacktestEngineKind.NAUTILUS: ("warning-a",),
            }
        ),
        clock=lambda: REQUESTED_AT + timedelta(minutes=2),
    )

    assert isinstance(response, Success)
    assert response.value.status is EngineParityStatus.CANONICAL_PARITY


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        (lambda: REQUESTED_AT.replace(tzinfo=None), ErrorCode.INVALID_INPUT),
        (
            lambda: (_ for _ in ()).throw(RuntimeError("private clock failure")),
            ErrorCode.TOOL_FAILED,
        ),
    ],
)
def test_invalid_or_failed_clock_returns_structured_failure(
    clock: Callable[[], object], expected: ErrorCode
) -> None:
    active = policy()

    response = run_engine_parity(
        request(active),
        active,
        engines(),
        clock=clock,  # type: ignore[arg-type]
    )

    assert isinstance(response, Failure)
    assert response.error.code is expected


def test_tampered_economics_fail_canonical_validation_before_comparison() -> None:
    active = policy()
    selected = engines()
    lean_job = job(BacktestEngineKind.LEAN)
    selected[BacktestEngineKind.LEAN] = StaticEngine(
        result(lean_job, price_delta=Decimal("0.01"))
    )

    response = run_engine_parity(
        request(active),
        active,
        selected,
        clock=lambda: REQUESTED_AT + timedelta(minutes=2),
    )

    assert isinstance(response, Failure)
    assert response.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_disabled_unavailable_and_late_engines_fail_without_report() -> None:
    active = policy()
    missing = engines()
    missing.pop(BacktestEngineKind.LEAN)
    disabled = run_engine_parity(
        request(active), active, missing, clock=lambda: REQUESTED_AT
    )
    unavailable = run_engine_parity(
        request(active),
        active,
        engines() | {BacktestEngineKind.LEAN: RaisingEngine()},
        clock=lambda: REQUESTED_AT,
    )
    late = run_engine_parity(
        request(active),
        active,
        engines(),
        clock=lambda: DEADLINE + timedelta(seconds=1),
    )

    assert isinstance(disabled, Failure)
    assert disabled.error.code is ErrorCode.CAPABILITY_DENIED
    assert isinstance(unavailable, Failure)
    assert unavailable.error.code is ErrorCode.TOOL_FAILED
    assert isinstance(late, Failure)
    assert late.error.code is ErrorCode.DEADLINE_EXCEEDED


def test_runtime_or_fence_drift_is_rejected_by_core_validation() -> None:
    active = policy()
    selected = engines()
    wrong_job = job(BacktestEngineKind.NAUTILUS).model_copy(
        update={"attempt_nonce": "wrong-nonce"}
    )
    selected[BacktestEngineKind.NAUTILUS] = StaticEngine(result(wrong_job))

    response = run_engine_parity(
        request(active), active, selected, clock=lambda: REQUESTED_AT
    )

    assert isinstance(response, Failure)
    assert response.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_report_is_frozen_and_hash_tamper_evident() -> None:
    active = policy()
    response = run_engine_parity(
        request(active), active, engines(), clock=lambda: REQUESTED_AT
    )
    assert isinstance(response, Success)

    with pytest.raises(ValidationError, match="parity hash"):
        response.value.model_validate(
            response.value.model_dump(mode="json") | {"parity_hash": "f" * 64}
        )
    with pytest.raises(ValidationError):
        response.value.status = EngineParityStatus.ENGINE_SPECIFIC  # type: ignore[misc]


def test_request_rejects_cross_input_jobs() -> None:
    active = policy()
    selected_jobs = list(jobs())
    lean = selected_jobs[-1]
    altered_dataset = lean.dataset.model_copy(
        update={"dataset_id": UUID("30000000-0000-4000-8000-000000000099")}
    )
    selected_jobs[-1] = lean.model_copy(
        update={
            "dataset": altered_dataset,
            "dataset_artifact_ref": f"sha256:{altered_dataset.payload_hash()}",
        }
    )

    with pytest.raises(ValidationError, match="same canonical input"):
        EngineParityRequest(
            evaluation_id=EVALUATION_ID,
            jobs=tuple(selected_jobs),
            policy_hash=active.policy_hash,
            requested_at=REQUESTED_AT,
            deadline=DEADLINE,
        )
