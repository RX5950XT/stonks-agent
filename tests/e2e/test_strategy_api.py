from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.application.strategies.manage import transition_strategy
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evaluation import (
    MANDATORY_EVALUATION_CHECKS,
    EvaluationCheck,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.signal import AlphaSignal, SignalDirection, SignalSource
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyAuditEvent,
    StrategyKind,
    StrategyManifest,
    StrategyMutationResult,
    StrategyRegistryEntry,
    StrategyTransitionRequest,
    can_transition,
)
from stonks_agent.entrypoints.api.routes.strategies import create_strategy_app
from stonks_agent.entrypoints.cli import app as cli_app
from stonks_agent.entrypoints.cli_commands import strategy as strategy_cli
from stonks_contracts.common import ConfidenceCalibration, stable_payload_hash

NOW = datetime(2026, 7, 13, 7, tzinfo=UTC)
TOKEN = "strategy-api-token-that-is-at-least-32-chars"
STRATEGY_ID = "kronos-return"
STRATEGY_VERSION = "1.0.0"
REPORT_ID = UUID("37000000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("37000000-0000-4000-8000-000000000002")
SIGNAL_ID = UUID("37000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("37000000-0000-4000-8000-000000000004")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def manifest() -> StrategyManifest:
    return StrategyManifest(
        manifest_id=UUID("37000000-0000-4000-8000-000000000005"),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        kind=StrategyKind.FORECAST_MAPPER,
        source_artifact_ref=f"sha256:{HASH_A}",
        runtime_hash=HASH_B,
        feature_spec_hash=HASH_C,
        label_spec_hash=HASH_D,
        universe_spec_hash=HASH_E,
        cost_model_hash=HASH_A,
        split_policy_hash=HASH_B,
        parameters_hash=HASH_C,
        owner="quant-research",
        deterministic=False,
        created_at=NOW,
    )


def evaluation() -> EvaluationReport:
    return EvaluationReport(
        report_id=REPORT_ID,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        strategy_manifest_hash=manifest().manifest_hash,
        dataset_snapshot_id=SNAPSHOT_ID,
        data_hash=HASH_C,
        runtime_hash=HASH_B,
        evaluation_policy_hash=HASH_E,
        as_of=NOW,
        window_start=NOW - timedelta(days=365),
        window_end=NOW - timedelta(days=1),
        checks=tuple(
            EvaluationCheck(kind=kind, status=EvaluationCheckStatus.PASSED)
            for kind in sorted(MANDATORY_EVALUATION_CHECKS, key=str)
        ),
        metrics=(
            EvaluationMetric(name="net_alpha", value=Decimal("0.01"), unit="return"),
        ),
        calibration=ConfidenceCalibration.CALIBRATED,
        baseline_ids=("last-value/1.0.0",),
        report_artifact_ref=f"sha256:{HASH_D}",
        valid_until=NOW + timedelta(days=90),
        created_at=NOW,
        passed=True,
    )


def signal(**changes: object) -> AlphaSignal:
    report = evaluation()
    payload: dict[str, object] = {
        "signal_id": SIGNAL_ID,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "instrument_id": INSTRUMENT_ID,
        "as_of": NOW,
        "generated_at": NOW + timedelta(seconds=1),
        "stale_at": NOW + timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=2),
        "horizon": "5 sessions",
        "value": Decimal("0.4"),
        "confidence": Decimal("0.7"),
        "calibration": ConfidenceCalibration.CALIBRATED,
        "direction": SignalDirection.LONG,
        "source": SignalSource.FORECAST,
        "strategy_manifest_hash": manifest().manifest_hash,
        "dataset_snapshot_id": SNAPSHOT_ID,
        "data_hash": HASH_C,
        "runtime_hash": HASH_B,
        "evaluation_policy_hash": HASH_E,
        "raw_output_artifact_ref": f"sha256:{HASH_E}",
        "evaluation_report_id": REPORT_ID,
        "evaluation_hash": report.evaluation_hash,
        "forecast_refs": (UUID("37000000-0000-4000-8000-000000000006"),),
    }
    return AlphaSignal.model_validate(payload | changes)


class Repository:
    def __init__(self) -> None:
        report = evaluation()
        self.entry = StrategyRegistryEntry(
            manifest=manifest(),
            state=PromotionState.PAPER_ELIGIBLE,
            evaluation_report_id=report.report_id,
            evaluation_hash=report.evaluation_hash,
            version=4,
            created_at=NOW,
            updated_at=NOW,
        )
        self.report = report
        self.events: tuple[StrategyAuditEvent, ...] = ()
        self.explode = False

    def get(
        self, strategy_id: str, strategy_version: str
    ) -> Result[StrategyRegistryEntry]:
        self._maybe_explode()
        if (strategy_id, strategy_version) != (STRATEGY_ID, STRATEGY_VERSION):
            return _failure(ErrorCode.NOT_FOUND, "Strategy was not found")
        return Success(self.entry)

    def get_evaluation(self, report_id: UUID) -> Result[EvaluationReport]:
        self._maybe_explode()
        if report_id != self.report.report_id:
            return _failure(ErrorCode.NOT_FOUND, "Evaluation was not found")
        return Success(self.report)

    def list_events(
        self, strategy_id: str, strategy_version: str
    ) -> Result[tuple[StrategyAuditEvent, ...]]:
        self._maybe_explode()
        if (strategy_id, strategy_version) != (STRATEGY_ID, STRATEGY_VERSION):
            return _failure(ErrorCode.NOT_FOUND, "Strategy was not found")
        return Success(self.events)

    def transition(
        self, request: StrategyTransitionRequest
    ) -> Result[StrategyMutationResult]:
        self._maybe_explode()
        current = self.entry
        if (
            request.expected_version != current.version
            or request.current_state is not current.state
            or not can_transition(request.current_state, request.target_state)
        ):
            return _failure(ErrorCode.CONFLICT, "Strategy CAS precondition failed")
        event = self._event(request)
        self.entry = current.model_copy(
            update={
                "state": request.target_state,
                "version": current.version + 1,
                "updated_at": request.requested_at,
            }
        )
        self.events = (*self.events, event)
        return Success(StrategyMutationResult(entry=self.entry, event=event))

    def _event(self, request: StrategyTransitionRequest) -> StrategyAuditEvent:
        previous_hash = self.events[-1].event_hash if self.events else HASH_A
        payload = request.model_dump(mode="json") | {"sequence": self.entry.version + 1}
        return StrategyAuditEvent(
            event_id=uuid4(),
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            sequence=self.entry.version + 1,
            event_type=f"strategy.{request.target_state.value}",
            from_state=request.current_state,
            to_state=request.target_state,
            reason_code=request.reason_code,
            actor=request.actor,
            evaluation_report_id=self.entry.evaluation_report_id,
            evaluation_hash=self.entry.evaluation_hash,
            occurred_at=request.requested_at,
            previous_hash=previous_hash,
            event_hash=stable_payload_hash(payload),
        )

    def _maybe_explode(self) -> None:
        if self.explode:
            raise RuntimeError("database_password=must-not-leak")


class UnitOfWork:
    def __init__(self, state: State) -> None:
        self.strategies = state.repository
        self._state = state

    def __enter__(self) -> UnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def commit(self) -> None:
        self._state.commits += 1

    def rollback(self) -> None:
        pass


class State:
    def __init__(self) -> None:
        self.repository = Repository()
        self.commits = 0

    def factory(self) -> UnitOfWork:
        return UnitOfWork(self)


def test_reviewer_can_suspend_then_retire_with_server_derived_audit_actor() -> None:
    state = State()
    client = TestClient(app(state))

    suspended = client.post(
        strategy_path("transitions"),
        headers=authorization(),
        json=transition_body(4, "paper_eligible", "suspended"),
    )
    retired = client.post(
        strategy_path("transitions"),
        headers=authorization(),
        json=transition_body(5, "suspended", "retired"),
    )

    assert suspended.status_code == 200
    assert retired.status_code == 200
    assert retired.json()["data"]["entry"]["state"] == "retired"
    assert [event.actor for event in state.repository.events] == [
        "local-reviewer",
        "local-reviewer",
    ]
    assert [event.to_state for event in state.repository.events] == [
        PromotionState.SUSPENDED,
        PromotionState.RETIRED,
    ]
    assert state.commits == 2


def test_viewer_can_read_registry_evaluation_and_audit_but_cannot_promote() -> None:
    state = State()
    client = TestClient(app(state, roles=frozenset({Role.VIEWER})))

    registry = client.get(strategy_path(), headers=authorization())
    report = client.get(f"/v1/evaluations/{REPORT_ID}", headers=authorization())
    events = client.get(strategy_path("events"), headers=authorization())
    denied = client.post(
        strategy_path("transitions"),
        headers=authorization(),
        json=transition_body(4, "paper_eligible", "suspended"),
    )

    assert registry.status_code == report.status_code == events.status_code == 200
    assert registry.json()["data"]["state"] == "paper_eligible"
    assert report.json()["data"]["evaluation_hash"] == evaluation().evaluation_hash
    assert events.json()["data"] == []
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert state.commits == 0


def test_signal_eligibility_resolves_exact_registry_and_evaluation_provenance() -> None:
    state = State()
    client = TestClient(app(state, roles=frozenset({Role.VIEWER})))

    eligible = client.post(
        "/v1/signals/eligibility",
        headers=authorization(),
        json={"signal": signal().model_dump(mode="json")},
    )
    mismatched = client.post(
        "/v1/signals/eligibility",
        headers=authorization(),
        json={"signal": signal(strategy_manifest_hash=HASH_A).model_dump(mode="json")},
    )

    assert eligible.status_code == 200
    assert eligible.json()["data"] == {
        "eligible": True,
        "weight": "0.7",
        "reason_codes": ["eligible"],
    }
    assert mismatched.status_code == 200
    assert mismatched.json()["data"]["eligible"] is False
    assert mismatched.json()["data"]["weight"] == "0"
    assert mismatched.json()["data"]["reason_codes"] == ["strategy_binding_mismatch"]
    assert state.commits == 0


def test_signal_eligibility_fails_closed_for_missing_strategy_or_evaluation() -> None:
    state = State()
    client = TestClient(app(state, roles=frozenset({Role.VIEWER})))

    unregistered = client.post(
        "/v1/signals/eligibility",
        headers=authorization(),
        json={"signal": signal(strategy_id="unknown-strategy").model_dump(mode="json")},
    )
    missing_report = client.post(
        "/v1/signals/eligibility",
        headers=authorization(),
        json={
            "signal": signal(
                evaluation_report_id=uuid4(), evaluation_hash=HASH_A
            ).model_dump(mode="json")
        },
    )

    assert unregistered.json()["data"]["reason_codes"] == ["strategy_unregistered"]
    assert missing_report.json()["data"]["reason_codes"] == ["evaluation_not_passed"]
    assert state.commits == 0


def test_live_execution_shaped_and_stale_cas_requests_fail_closed() -> None:
    state = State()
    client = TestClient(app(state))
    invalid_payloads = (
        transition_body(4, "paper_eligible", "live"),
        transition_body(4, "paper_eligible", "suspended") | {"order_side": "buy"},
    )

    invalid = [
        client.post(strategy_path("transitions"), headers=authorization(), json=body)
        for body in invalid_payloads
    ]
    stale = client.post(
        strategy_path("transitions"),
        headers=authorization(),
        json=transition_body(3, "paper_eligible", "suspended"),
    )
    invalid_path = client.get(
        f"/v1/strategies/INVALID/versions/{STRATEGY_VERSION}",
        headers=authorization(),
    )

    assert all(response.status_code == 400 for response in invalid)
    assert invalid_path.status_code == 400
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "conflict"
    assert state.repository.events == ()
    assert state.commits == 0


def test_strategy_api_defaults_to_deny_and_redacts_unexpected_errors() -> None:
    state = State()
    denied_client = TestClient(create_strategy_app(state.factory, clock=lambda: NOW))
    denied = denied_client.get(strategy_path(), headers=authorization())
    denied_evaluation = denied_client.get(
        f"/v1/evaluations/{REPORT_ID}", headers=authorization()
    )
    denied_signal = denied_client.post(
        "/v1/signals/eligibility",
        headers=authorization(),
        json={"signal": signal().model_dump(mode="json")},
    )
    state.repository.explode = True
    failed = TestClient(app(state), raise_server_exceptions=False).get(
        strategy_path(), headers=authorization()
    )

    assert denied.status_code == denied_evaluation.status_code == 401
    assert denied_signal.status_code == 401
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "internal_error"
    assert "database_password" not in failed.text


def test_application_rejects_forged_transition_actor_before_repository_call() -> None:
    state = State()
    principal = LocalPrincipal(
        subject="reviewer:actual", roles=frozenset({Role.STRATEGY_REVIEWER})
    )
    request = StrategyTransitionRequest(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        expected_version=4,
        current_state=PromotionState.PAPER_ELIGIBLE,
        target_state=PromotionState.SUSPENDED,
        reason_code="risk_drift",
        actor="reviewer:forged",
        requested_at=NOW + timedelta(minutes=1),
    )

    result = transition_strategy(principal, request, state.factory)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert state.repository.events == ()
    assert state.commits == 0


def test_strategy_cli_executes_reads_transition_and_conflict_via_same_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = State()

    def run_database(database_url: str, operation: Any) -> Any:
        assert database_url == "postgresql+psycopg://fixture"
        return operation(state.factory)

    monkeypatch.setattr(strategy_cli, "_run_database", run_database)
    runner = CliRunner()
    base = [
        "--strategy-id",
        STRATEGY_ID,
        "--strategy-version",
        STRATEGY_VERSION,
        "--database-url",
        "postgresql+psycopg://fixture",
    ]

    shown = runner.invoke(cli_app, ["strategy", "show", *base])
    report = runner.invoke(
        cli_app,
        [
            "strategy",
            "evaluation",
            "--report-id",
            str(REPORT_ID),
            "--database-url",
            "postgresql+psycopg://fixture",
        ],
    )
    transitioned = runner.invoke(
        cli_app,
        [
            "strategy",
            "transition",
            *base,
            "--expected-version",
            "4",
            "--current-state",
            "paper_eligible",
            "--target-state",
            "suspended",
            "--reason-code",
            "risk_drift",
        ],
    )
    stale = runner.invoke(
        cli_app,
        [
            "strategy",
            "transition",
            *base,
            "--expected-version",
            "4",
            "--current-state",
            "paper_eligible",
            "--target-state",
            "suspended",
            "--reason-code",
            "risk_drift",
        ],
    )
    events = runner.invoke(cli_app, ["strategy", "events", *base])

    assert shown.exit_code == report.exit_code == transitioned.exit_code == 0
    assert shown.stdout and '"state": "paper_eligible"' in shown.stdout
    assert evaluation().evaluation_hash in report.stdout
    assert '"state": "suspended"' in transitioned.stdout
    assert stale.exit_code == 2
    assert '"code": "conflict"' in stale.stdout
    assert '"event_type": "strategy.suspended"' in events.stdout


def app(
    state: State,
    *,
    roles: frozenset[Role] = frozenset({Role.STRATEGY_REVIEWER}),
) -> object:
    return create_strategy_app(
        state.factory,
        LocalTokenAuthenticator(
            token=TOKEN,
            subject="local-reviewer",
            roles=roles,
            allowed_hosts=frozenset({"testclient"}),
        ),
        clock=lambda: NOW + timedelta(minutes=1),
    )


def strategy_path(suffix: str = "") -> str:
    base = f"/v1/strategies/{STRATEGY_ID}/versions/{STRATEGY_VERSION}"
    return f"{base}/{suffix}" if suffix else base


def transition_body(
    version: int, current_state: str, target_state: str
) -> dict[str, object]:
    return {
        "expected_version": version,
        "current_state": current_state,
        "target_state": target_state,
        "evaluation_report_id": None,
        "evaluation_hash": None,
        "reason_code": f"move_to_{target_state}",
    }


def authorization() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
