from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from threading import Barrier
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.adapters.postgres.strategy_repository import (
    PostgresStrategyRepository,
)
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.evaluation import (
    MANDATORY_EVALUATION_CHECKS,
    EvaluationCheck,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyKind,
    StrategyManifest,
    StrategyTransitionRequest,
)
from stonks_agent.entrypoints.api.routes.strategies import create_strategy_app
from stonks_agent.entrypoints.cli import app as cli_app
from stonks_contracts.common import ConfidenceCalibration

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 13, 2, tzinfo=UTC)
STRATEGY_ID = "kronos-return"
STRATEGY_VERSION = "1.0.0"
MANIFEST_ID = UUID("00000000-0000-4000-8000-000000000301")
REPORT_ID = UUID("00000000-0000-4000-8000-000000000302")
SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000303")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
HASH_9 = "9" * 64
TOKEN = "strategy-postgres-token-that-is-at-least-32-chars"


def manifest() -> StrategyManifest:
    return StrategyManifest(
        manifest_id=MANIFEST_ID,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        kind=StrategyKind.FORECAST_MAPPER,
        source_artifact_ref=f"sha256:{HASH_A}",
        runtime_hash=HASH_B,
        feature_spec_hash=HASH_C,
        label_spec_hash=HASH_D,
        universe_spec_hash=HASH_E,
        cost_model_hash=HASH_F,
        split_policy_hash=HASH_A,
        parameters_hash=HASH_B,
        owner="quant-research",
        deterministic=False,
        created_at=NOW,
    )


def evaluation(*, passed: bool = True) -> EvaluationReport:
    status = EvaluationCheckStatus.PASSED if passed else EvaluationCheckStatus.FAILED
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
            EvaluationCheck(kind=kind, status=status)
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
        passed=passed,
    )


def transition(
    current: PromotionState,
    target: PromotionState,
    version: int,
    *,
    with_evaluation: bool = False,
    requested_at: datetime = NOW,
) -> StrategyTransitionRequest:
    report = evaluation()
    return StrategyTransitionRequest(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        expected_version=version,
        current_state=current,
        target_state=target,
        evaluation_report_id=report.report_id if with_evaluation else None,
        evaluation_hash=report.evaluation_hash if with_evaluation else None,
        reason_code=f"move_to_{target.value}",
        actor="reviewer:test",
        requested_at=requested_at,
    )


def test_register_is_idempotent_and_creates_genesis_audit(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database, expire_on_commit=False) as session:
        repository = PostgresStrategyRepository(session)
        first = repository.register(manifest())
        second = repository.register(manifest())
        session.commit()

        assert isinstance(first, Success)
        assert isinstance(second, Success)
        assert first.value.entry == second.value.entry
        assert first.value.entry.state is PromotionState.DRAFT
        assert first.value.event == second.value.event
        events = repository.list_events(STRATEGY_ID, STRATEGY_VERSION)

    assert isinstance(events, Success)
    assert len(events.value) == 1
    assert events.value[0].sequence == 1
    assert events.value[0].previous_hash is None


def test_unit_of_work_exposes_strategy_repository_and_commits_atomically(
    strategy_database: Engine,
) -> None:
    with PostgresUnitOfWork(strategy_database) as unit_of_work:
        registered = unit_of_work.strategies.register(manifest())
        unit_of_work.commit()

    with Session(strategy_database) as session:
        persisted = PostgresStrategyRepository(session).get(
            STRATEGY_ID, STRATEGY_VERSION
        )

    assert isinstance(registered, Success)
    assert isinstance(persisted, Success)
    assert persisted.value == registered.value.entry


def test_register_rejects_same_identity_with_different_manifest(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        repository = PostgresStrategyRepository(session)
        assert isinstance(repository.register(manifest()), Success)
        changed = manifest().model_copy(update={"runtime_hash": HASH_F})
        result = repository.register(changed)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_evaluation_requires_exact_strategy_snapshot_and_artifact_binding(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        repository = PostgresStrategyRepository(session)
        assert isinstance(repository.register(manifest()), Success)
        accepted = repository.register_evaluation(evaluation())
        replay = repository.register_evaluation(evaluation())
        mismatch = repository.register_evaluation(
            evaluation().model_copy(update={"runtime_hash": HASH_F})
        )
        session.commit()

    assert isinstance(accepted, Success)
    assert isinstance(replay, Success)
    assert isinstance(mismatch, Failure)
    assert mismatch.error.code is ErrorCode.CONFLICT


def test_registered_evaluation_is_read_back_with_verified_registry_binding(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        repository = PostgresStrategyRepository(session)
        assert isinstance(repository.register(manifest()), Success)
        assert isinstance(repository.register_evaluation(evaluation()), Success)
        session.commit()
        result = repository.get_evaluation(REPORT_ID)

    assert isinstance(result, Success)
    assert result.value == evaluation()
    assert result.value.evaluation_hash == evaluation().evaluation_hash


def test_promotion_uses_database_clock_and_builds_verified_hash_chain(
    strategy_database: Engine,
) -> None:
    caller_future = NOW + timedelta(days=3650)
    with Session(strategy_database, expire_on_commit=False) as session:
        repository = PostgresStrategyRepository(session)
        registered = repository.register(manifest())
        assert isinstance(registered, Success)
        evaluating = repository.transition(
            transition(
                PromotionState.DRAFT,
                PromotionState.EVALUATING,
                1,
                requested_at=caller_future,
            )
        )
        assert isinstance(evaluating, Success)
        assert isinstance(repository.register_evaluation(evaluation()), Success)
        shadow = repository.transition(
            transition(
                PromotionState.EVALUATING,
                PromotionState.SHADOW,
                2,
                with_evaluation=True,
                requested_at=caller_future,
            )
        )
        session.commit()
        events = repository.list_events(STRATEGY_ID, STRATEGY_VERSION)

    assert isinstance(shadow, Success)
    assert shadow.value.entry.state is PromotionState.SHADOW
    assert shadow.value.entry.updated_at < caller_future
    assert isinstance(events, Success)
    assert [event.sequence for event in events.value] == [1, 2, 3]
    assert all(
        current.previous_hash == previous.event_hash
        for previous, current in pairwise(events.value)
    )


def test_invalid_or_unregistered_evaluation_cannot_promote(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        repository = PostgresStrategyRepository(session)
        assert isinstance(repository.register(manifest()), Success)
        assert isinstance(
            repository.transition(
                transition(PromotionState.DRAFT, PromotionState.EVALUATING, 1)
            ),
            Success,
        )
        missing = repository.transition(
            transition(
                PromotionState.EVALUATING,
                PromotionState.SHADOW,
                2,
                with_evaluation=True,
            )
        )
        assert isinstance(
            repository.register_evaluation(evaluation(passed=False)), Success
        )
        failed = repository.transition(
            transition(
                PromotionState.EVALUATING,
                PromotionState.SHADOW,
                2,
                with_evaluation=True,
            )
        )

    assert isinstance(missing, Failure)
    assert missing.error.code is ErrorCode.NOT_FOUND
    assert isinstance(failed, Failure)
    assert failed.error.code is ErrorCode.CONFLICT


def test_concurrent_cas_allows_exactly_one_paper_state_mutation(
    strategy_database: Engine,
) -> None:
    _promote_to_paper(strategy_database)
    barrier = Barrier(2)

    def mutate(target: PromotionState) -> object:
        with Session(strategy_database, expire_on_commit=False) as session:
            repository = PostgresStrategyRepository(session)
            barrier.wait(timeout=5)
            result = repository.transition(
                transition(PromotionState.PAPER_ELIGIBLE, target, 4)
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(mutate, (PromotionState.SUSPENDED, PromotionState.RETIRED))
        )

    assert sum(isinstance(result, Success) for result in results) == 1
    assert (
        sum(
            isinstance(result, Failure) and result.error.code is ErrorCode.CONFLICT
            for result in results
        )
        == 1
    )
    with Session(strategy_database) as session:
        events = PostgresStrategyRepository(session).list_events(
            STRATEGY_ID, STRATEGY_VERSION
        )
    assert isinstance(events, Success)
    assert [event.sequence for event in events.value] == [1, 2, 3, 4, 5]


def test_suspend_then_retire_each_append_audit_and_stale_cas_appends_nothing(
    strategy_database: Engine,
) -> None:
    _promote_to_paper(strategy_database)
    with Session(strategy_database) as session:
        repository = PostgresStrategyRepository(session)
        suspended = repository.transition(
            transition(PromotionState.PAPER_ELIGIBLE, PromotionState.SUSPENDED, 4)
        )
        stale = repository.transition(
            transition(PromotionState.SUSPENDED, PromotionState.RETIRED, 4)
        )
        retired = repository.transition(
            transition(PromotionState.SUSPENDED, PromotionState.RETIRED, 5)
        )
        session.commit()
        events = repository.list_events(STRATEGY_ID, STRATEGY_VERSION)

    assert isinstance(suspended, Success)
    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT
    assert isinstance(retired, Success)
    assert isinstance(events, Success)
    assert [event.to_state for event in events.value[-2:]] == [
        PromotionState.SUSPENDED,
        PromotionState.RETIRED,
    ]
    assert [event.sequence for event in events.value] == [1, 2, 3, 4, 5, 6]


def test_strategy_api_and_cli_share_postgres_cas_and_verified_audit(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        assert isinstance(
            PostgresStrategyRepository(session).register(manifest()), Success
        )
        session.commit()
    client = TestClient(
        create_strategy_app(
            lambda: PostgresUnitOfWork(strategy_database),
            LocalTokenAuthenticator(
                token=TOKEN,
                subject="reviewer:postgres",
                roles=frozenset({Role.STRATEGY_REVIEWER}),
                allowed_hosts=frozenset({"testclient"}),
            ),
            clock=lambda: NOW + timedelta(minutes=1),
        )
    )
    body = {
        "expected_version": 1,
        "current_state": "draft",
        "target_state": "evaluating",
        "evaluation_report_id": None,
        "evaluation_hash": None,
        "reason_code": "begin_evaluation",
    }

    promoted = client.post(
        f"/v1/strategies/{STRATEGY_ID}/versions/{STRATEGY_VERSION}/transitions",
        headers={"authorization": f"Bearer {TOKEN}"},
        json=body,
    )
    stale = client.post(
        f"/v1/strategies/{STRATEGY_ID}/versions/{STRATEGY_VERSION}/transitions",
        headers={"authorization": f"Bearer {TOKEN}"},
        json=body,
    )
    cli = CliRunner().invoke(
        cli_app,
        [
            "strategy",
            "events",
            "--strategy-id",
            STRATEGY_ID,
            "--strategy-version",
            STRATEGY_VERSION,
            "--database-url",
            str(strategy_database.url),
        ],
    )

    assert promoted.status_code == 200
    assert promoted.json()["data"]["entry"]["state"] == "evaluating"
    assert stale.status_code == 409
    assert cli.exit_code == 0, cli.output
    events = json.loads(cli.stdout)["data"]
    assert [event["sequence"] for event in events] == [1, 2]
    assert events[-1]["actor"] == "reviewer:postgres"


def test_registry_identity_and_audit_rows_are_database_immutable(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        assert isinstance(
            PostgresStrategyRepository(session).register(manifest()), Success
        )
        session.commit()

    with (
        pytest.raises(DBAPIError, match="immutable"),
        strategy_database.begin() as connection,
    ):
        connection.execute(
            text(
                "update strategy_registry set runtime_hash = :hash "
                "where strategy_id = :strategy_id and strategy_version = :version"
            ),
            {"hash": HASH_F, "strategy_id": STRATEGY_ID, "version": STRATEGY_VERSION},
        )
    with (
        pytest.raises(DBAPIError, match="append-only"),
        strategy_database.begin() as connection,
    ):
        connection.execute(text("delete from strategy_audit_event"))


def test_app_role_cannot_bypass_audit_or_mutate_evaluation(
    strategy_database: Engine,
) -> None:
    with Session(strategy_database) as session:
        repository = PostgresStrategyRepository(session)
        assert isinstance(repository.register(manifest()), Success)
        assert isinstance(repository.register_evaluation(evaluation()), Success)
        session.commit()

    with (
        pytest.raises(DBAPIError, match="matching immutable audit"),
        strategy_database.begin() as connection,
    ):
        connection.execute(text("set local role stonks_app"))
        connection.execute(
            text(
                """
                update strategy_registry
                set state = 'evaluating', version = 2,
                    updated_at = clock_timestamp()
                where strategy_id = :strategy_id
                  and strategy_version = :version
                """
            ),
            {"strategy_id": STRATEGY_ID, "version": STRATEGY_VERSION},
        )

    with (
        pytest.raises(DBAPIError, match="append-only"),
        strategy_database.begin() as connection,
    ):
        connection.execute(
            text(
                "update strategy_evaluation_report set data_hash = :hash "
                "where report_id = :report_id"
            ),
            {"hash": HASH_F, "report_id": REPORT_ID},
        )


def _promote_to_paper(engine: Engine) -> None:
    with Session(engine) as session:
        repository = PostgresStrategyRepository(session)
        assert isinstance(repository.register(manifest()), Success)
        assert isinstance(
            repository.transition(
                transition(PromotionState.DRAFT, PromotionState.EVALUATING, 1)
            ),
            Success,
        )
        assert isinstance(repository.register_evaluation(evaluation()), Success)
        assert isinstance(
            repository.transition(
                transition(
                    PromotionState.EVALUATING,
                    PromotionState.SHADOW,
                    2,
                    with_evaluation=True,
                )
            ),
            Success,
        )
        assert isinstance(
            repository.transition(
                transition(
                    PromotionState.SHADOW,
                    PromotionState.PAPER_ELIGIBLE,
                    3,
                    with_evaluation=True,
                )
            ),
            Success,
        )
        session.commit()


@pytest.fixture
def strategy_database(clean_database: Engine) -> Engine:
    with clean_database.begin() as connection:
        for content_hash in (HASH_A, HASH_D, HASH_9):
            _insert_artifact(connection, content_hash)
        connection.execute(
            text(
                """
                insert into dataset_snapshot
                    (snapshot_id, as_of, cutoff_at, provider_policy_id,
                     manifest_artifact_hash, content_hash, manifest, created_at)
                values
                    (:snapshot_id, :as_of, :as_of, 'test-policy',
                     :artifact_hash, :content_hash, '{}'::jsonb, :as_of)
                """
            ),
            {
                "snapshot_id": SNAPSHOT_ID,
                "as_of": NOW,
                "artifact_hash": HASH_9,
                "content_hash": HASH_C,
            },
        )
    return clean_database


def _insert_artifact(connection: object, content_hash: str) -> None:
    connection.execute(  # type: ignore[attr-defined]
        text(
            """
            insert into artifact_manifest
                (content_hash, size_bytes, media_type, license_tag, sensitivity,
                 source, finalized_at, storage_uri, metadata)
            values
                (:hash, 1, 'application/json', 'test-only', 'internal',
                 'fixture', :now, :uri, '{}'::jsonb)
            """
        ),
        {
            "hash": content_hash,
            "now": NOW,
            "uri": f"artifact://sha256/{content_hash}",
        },
    )
