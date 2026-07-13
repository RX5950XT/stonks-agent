from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from stonks_agent.adapters.postgres.models import Base

pytestmark = pytest.mark.postgres

EXPECTED_TABLES = {
    "instrument",
    "instrument_alias",
    "trading_calendar_version",
    "artifact_manifest",
    "evidence_item",
    "evidence_edge",
    "dataset_snapshot",
    "dataset_snapshot_evidence",
    "run",
    "run_dataset_snapshot",
    "run_event",
    "job",
    "outbox",
    "inbox",
    "provider_health",
    "usage_budget",
    "strategy_registry",
    "strategy_evaluation_report",
    "strategy_audit_event",
    "paper_account",
    "paper_account_event",
    "paper_cash_projection",
    "paper_position_projection",
    "portfolio_target",
    "risk_decision",
    "account_reservation",
    "reservation_event",
    "order_intent",
    "order_event",
    "paper_fill",
    "journal_transaction",
    "journal_posting",
    "paper_kill_switch",
}
ROLE_NAMES = {"stonks_app", "stonks_reader", "stonks_worker"}
APP_MUTABLE_TABLES = {
    "instrument",
    "instrument_alias",
    "trading_calendar_version",
    "provider_health",
    "usage_budget",
}
WORKER_APPEND_TABLES = {
    "artifact_manifest",
    "evidence_item",
    "evidence_edge",
    "dataset_snapshot",
    "dataset_snapshot_evidence",
    "run_dataset_snapshot",
    "run_event",
    "inbox",
}
QUEUE_UPDATE_COLUMNS = {
    "job": {
        "status",
        "not_before",
        "attempts",
        "attempt_generation",
        "attempt_nonce",
        "lease_owner",
        "lease_until",
        "result_artifact_hash",
        "last_error",
        "updated_at",
    },
    "outbox": {
        "not_before",
        "published_at",
        "attempts",
        "lease_owner",
        "lease_until",
        "lease_generation",
        "lease_nonce",
        "last_error",
    },
}
TRADING_UPDATE_COLUMNS = {
    "paper_account": {
        "aggregate_sequence",
        "portfolio_sequence",
        "ledger_sequence",
        "ledger_hash",
        "updated_at",
    },
    "paper_cash_projection": {
        "settled_amount",
        "reserved_amount",
        "updated_sequence",
        "updated_at",
    },
    "paper_position_projection": {
        "quantity",
        "sellable_quantity",
        "reserved_quantity",
        "updated_sequence",
        "updated_at",
    },
    "account_reservation": {
        "remaining_amount",
        "state",
        "updated_at",
        "event_sequence",
        "previous_event_hash",
        "event_hash",
    },
    "paper_kill_switch": {
        "active",
        "reason_code",
        "actor",
        "version",
        "updated_at",
    },
}
NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


def test_migration_downgrade_and_reupgrade(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    assert set(inspect(migrated_engine).get_table_names()) >= EXPECTED_TABLES

    command.downgrade(alembic_config, "base")
    assert not EXPECTED_TABLES & set(inspect(migrated_engine).get_table_names())

    command.upgrade(alembic_config, "head")
    assert set(inspect(migrated_engine).get_table_names()) >= EXPECTED_TABLES


def test_sqlalchemy_metadata_matches_migration(migrated_engine: Engine) -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert set(inspect(migrated_engine).get_table_names()) >= EXPECTED_TABLES


def test_append_only_artifact_rejects_update_and_delete(
    migrated_engine: Engine,
) -> None:
    content_hash = "a" * 64
    with migrated_engine.begin() as connection:
        _insert_artifact(connection, content_hash)

    with (
        pytest.raises(DBAPIError, match="append-only"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "update artifact_manifest set size_bytes = 2 where content_hash = :hash"
            ),
            {"hash": content_hash},
        )

    with (
        pytest.raises(DBAPIError, match="append-only"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(
            text("delete from artifact_manifest where content_hash = :hash"),
            {"hash": content_hash},
        )


def test_evidence_requires_finalized_artifact_and_no_future_data(
    migrated_engine: Engine,
) -> None:
    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        _insert_evidence(
            connection,
            raw_artifact_hash="b" * 64,
            available_at=NOW,
        )

    artifact_hash = "c" * 64
    with migrated_engine.begin() as connection:
        _insert_artifact(connection, artifact_hash)

    with (
        pytest.raises(IntegrityError, match="evidence_available_by_as_of"),
        migrated_engine.begin() as connection,
    ):
        _insert_evidence(
            connection,
            raw_artifact_hash=artifact_hash,
            available_at=datetime(2026, 1, 2, 21, 0, 1, tzinfo=UTC),
        )


def test_database_roles_have_least_privilege_grants(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select table_name, grantee, privilege_type
                from information_schema.table_privileges
                where table_schema = 'public'
                  and grantee in (
                      'PUBLIC', 'stonks_app', 'stonks_worker', 'stonks_reader'
                  )
                """
            )
        ).all()

    grants = {
        (row.table_name, row.grantee, row.privilege_type)
        for row in rows
        if row.table_name in EXPECTED_TABLES
    }
    assert grants == _expected_table_grants()


def test_database_roles_have_exact_schema_privileges(migrated_engine: Engine) -> None:
    assert _schema_grants(migrated_engine) == {(role, "USAGE") for role in ROLE_NAMES}


def test_run_updates_are_limited_to_transition_columns(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select grantee, column_name, privilege_type
                from information_schema.column_privileges
                where table_schema = 'public' and table_name = 'run'
                  and grantee in ('stonks_app', 'stonks_worker')
                  and privilege_type = 'UPDATE'
                """
            )
        ).all()

    assert {(row.grantee, row.column_name, row.privilege_type) for row in rows} == {
        (role, column, "UPDATE")
        for role in ("stonks_app", "stonks_worker")
        for column in ("status", "updated_at", "version")
    }


def test_queue_updates_are_limited_to_transition_columns(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select table_name, grantee, column_name, privilege_type
                from information_schema.column_privileges
                where table_schema = 'public'
                  and table_name in ('job', 'outbox')
                  and grantee in ('stonks_app', 'stonks_worker')
                  and privilege_type = 'UPDATE'
                """
            )
        ).all()

    assert {
        (row.table_name, row.grantee, row.column_name, row.privilege_type)
        for row in rows
    } == {
        (table, role, column, "UPDATE")
        for table, columns in QUEUE_UPDATE_COLUMNS.items()
        for role in ("stonks_app", "stonks_worker")
        for column in columns
    }


def test_strategy_updates_are_app_only_and_column_scoped(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select grantee, column_name, privilege_type
                from information_schema.column_privileges
                where table_schema = 'public' and table_name = 'strategy_registry'
                  and grantee in ('stonks_app', 'stonks_worker')
                  and privilege_type = 'UPDATE'
                """
            )
        ).all()

    assert {(row.grantee, row.column_name, row.privilege_type) for row in rows} == {
        ("stonks_app", column, "UPDATE")
        for column in (
            "state",
            "evaluation_report_id",
            "evaluation_hash",
            "version",
            "updated_at",
        )
    }


def test_trading_updates_are_app_only_and_column_scoped(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select table_name, grantee, column_name, privilege_type
                from information_schema.column_privileges
                where table_schema = 'public'
                  and table_name = any(:tables)
                  and grantee in ('stonks_app', 'stonks_worker')
                  and privilege_type = 'UPDATE'
                """
            ),
            {"tables": list(TRADING_UPDATE_COLUMNS)},
        ).all()

    assert {
        (row.table_name, row.grantee, row.column_name, row.privilege_type)
        for row in rows
    } == {
        (table, "stonks_app", column, "UPDATE")
        for table, columns in TRADING_UPDATE_COLUMNS.items()
        for column in columns
    }


def test_strategy_registry_migration_downgrade_and_reupgrade(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    command.downgrade(alembic_config, "0008")
    try:
        tables = set(inspect(migrated_engine).get_table_names())
        assert (
            not {
                "strategy_registry",
                "strategy_evaluation_report",
                "strategy_audit_event",
            }
            & tables
        )
    finally:
        command.upgrade(alembic_config, "head")

    assert _trigger_exists(migrated_engine, "trg_strategy_registry_immutable")


def test_trading_migration_downgrade_and_reupgrade(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    trading_tables = set(TRADING_UPDATE_COLUMNS) | {
        "paper_account_event",
        "portfolio_target",
        "risk_decision",
        "reservation_event",
        "order_intent",
        "order_event",
        "paper_fill",
        "journal_transaction",
        "journal_posting",
    }
    command.downgrade(alembic_config, "0009")
    try:
        assert not trading_tables & set(inspect(migrated_engine).get_table_names())
    finally:
        command.upgrade(alembic_config, "head")

    for trigger in (
        "trg_paper_account_mutation_has_event",
        "trg_order_event_chain",
        "trg_reservation_event_chain",
        "trg_journal_transaction_chain",
        "trg_journal_transaction_balanced",
    ):
        assert _trigger_exists(migrated_engine, trigger)


def test_account_sequence_requires_matching_event(migrated_engine: Engine) -> None:
    account_id = f"paper-account-{uuid4()}"
    with migrated_engine.begin() as connection:
        _insert_paper_account(connection, account_id)

    with (
        pytest.raises(DBAPIError, match="requires matching event"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "update paper_account set aggregate_sequence = 1 "
                "where account_id = :account_id"
            ),
            {"account_id": account_id},
        )


def test_account_event_cannot_advance_without_account_cas(
    migrated_engine: Engine,
) -> None:
    account_id = f"paper-account-{uuid4()}"
    with migrated_engine.begin() as connection:
        _insert_paper_account(connection, account_id)

    with (
        pytest.raises(DBAPIError, match="does not match account sequence"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(
            text(
                """
                insert into paper_account_event
                    (event_id, account_id, sequence, event_type,
                     aggregate_ref_type, aggregate_ref_id, occurred_at,
                     previous_hash, event_hash)
                values
                    (:event_id, :account_id, 1, 'orphan.test',
                     'test', :aggregate_ref_id, :occurred_at, null, :event_hash)
                """
            ),
            {
                "event_id": uuid4(),
                "account_id": account_id,
                "aggregate_ref_id": uuid4(),
                "occurred_at": NOW,
                "event_hash": uuid4().hex * 2,
            },
        )


@pytest.mark.parametrize("role", ("stonks_app", "stonks_worker"))
@pytest.mark.parametrize(
    ("table", "protected_column"),
    (("job", "payload"), ("outbox", "payload")),
)
def test_queue_roles_cannot_update_canonical_payloads(
    migrated_engine: Engine,
    role: str,
    table: str,
    protected_column: str,
) -> None:
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(text(f"set local role {role}"))
        connection.execute(
            text(f"update {table} set {protected_column} = {protected_column}")
        )


@pytest.mark.parametrize("role", ("stonks_app", "stonks_worker"))
def test_queue_roles_can_apply_snapshot_retry_transition(
    migrated_engine: Engine,
    role: str,
) -> None:
    with migrated_engine.begin() as connection:
        connection.execute(text(f"set local role {role}"))
        connection.execute(
            text(
                """
                update job
                set status = 'queued', not_before = :now,
                    attempt_nonce = null, lease_owner = null,
                    lease_until = null, last_error = :last_error,
                    updated_at = :now
                where false
                """
            ),
            {
                "now": NOW,
                "last_error": '{"code":"retry"}',
            },
        )


def test_queue_column_grants_downgrade_and_reupgrade(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    command.downgrade(alembic_config, "0007")
    try:
        assert _table_update_roles(migrated_engine, "job") == {
            "stonks_app",
            "stonks_worker",
        }
        assert _table_update_roles(migrated_engine, "outbox") == {
            "stonks_app",
            "stonks_worker",
        }
    finally:
        command.upgrade(alembic_config, "head")

    assert _table_update_roles(migrated_engine, "job") == set()
    assert _table_update_roles(migrated_engine, "outbox") == set()


def test_pit_authority_migration_downgrade_and_reupgrade(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    command.downgrade(alembic_config, "0006")
    try:
        assert not _trigger_exists(
            migrated_engine,
            "trg_run_linked_authority_immutable",
        )
        assert _table_update_roles(migrated_engine, "run") == {
            "stonks_app",
            "stonks_worker",
        }
    finally:
        command.upgrade(alembic_config, "head")

    assert _trigger_exists(migrated_engine, "trg_run_linked_authority_immutable")
    assert _table_update_roles(migrated_engine, "run") == set()


def test_public_schema_acl_downgrade_and_reupgrade(
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    command.downgrade(alembic_config, "0005")
    try:
        assert _schema_grants(migrated_engine) == {
            *((role, "USAGE") for role in ROLE_NAMES),
            ("PUBLIC", "USAGE"),
        }
    finally:
        command.upgrade(alembic_config, "head")

    assert _schema_grants(migrated_engine) == {(role, "USAGE") for role in ROLE_NAMES}


def test_job_requires_run_and_deadline(clean_database: Engine) -> None:
    run_id = uuid4()
    with clean_database.begin() as connection:
        _insert_run(connection, run_id)

    with pytest.raises(IntegrityError), clean_database.begin() as connection:
        _insert_job(connection, run_id=None, deadline_at=NOW)

    with pytest.raises(IntegrityError), clean_database.begin() as connection:
        _insert_job(connection, run_id=run_id, deadline_at=None)


def test_job_deadline_constraint_is_unconditional(migrated_engine: Engine) -> None:
    constraints = inspect(migrated_engine).get_check_constraints("job")
    deadline = next(
        item for item in constraints if item["name"] == "job_deadline_after_not_before"
    )
    normalized = " ".join(str(deadline["sqltext"]).lower().split())

    assert normalized == "deadline_at > not_before"


def _expected_table_grants() -> set[tuple[str, str, str]]:
    expected = {(table, "stonks_reader", "SELECT") for table in EXPECTED_TABLES}
    expected.update(
        (table, "stonks_app", privilege)
        for table in EXPECTED_TABLES
        for privilege in ("SELECT", "INSERT")
    )
    expected.update((table, "stonks_app", "UPDATE") for table in APP_MUTABLE_TABLES)
    expected.update(
        (table, "stonks_worker", privilege)
        for table in WORKER_APPEND_TABLES
        for privilege in ("SELECT", "INSERT")
    )
    expected.update(
        (table, "stonks_worker", privilege)
        for table in QUEUE_UPDATE_COLUMNS
        for privilege in ("SELECT", "INSERT")
    )
    expected.update(
        {
            ("run", "stonks_worker", "SELECT"),
        }
    )
    return expected


def _schema_grants(engine: Engine) -> set[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select coalesce(role.rolname, 'PUBLIC') as grantee,
                       acl.privilege_type
                from pg_namespace namespace
                cross join lateral aclexplode(
                    coalesce(namespace.nspacl, acldefault('n', namespace.nspowner))
                ) acl
                left join pg_roles role on role.oid = acl.grantee
                where namespace.nspname = 'public'
                  and (acl.grantee = 0 or role.rolname in (
                      'stonks_app', 'stonks_worker', 'stonks_reader'
                  ))
                """
            )
        ).all()
    return {(row.grantee, row.privilege_type) for row in rows}


def _trigger_exists(engine: Engine, trigger_name: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    """
                    select exists(
                        select 1 from pg_trigger
                        where tgname = :trigger_name and not tgisinternal
                    )
                    """
                ),
                {"trigger_name": trigger_name},
            ).scalar_one()
        )


def _table_update_roles(engine: Engine, table: str) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select grantee
                from information_schema.table_privileges
                where table_schema = 'public' and table_name = :table
                  and grantee in ('stonks_app', 'stonks_worker')
                  and privilege_type = 'UPDATE'
                """
            ),
            {"table": table},
        ).all()
    return {row.grantee for row in rows}


def _insert_artifact(connection: Connection, content_hash: str) -> None:
    connection.execute(
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


def _insert_evidence(
    connection: Connection,
    *,
    raw_artifact_hash: str,
    available_at: datetime,
) -> None:
    connection.execute(
        text(
            """
            insert into evidence_item
                (evidence_id, subject, kind, event_time, published_at,
                 available_at, observed_at, as_of, source, provider,
                 content_hash, raw_artifact_hash, quality_state, sensitivity,
                 license_tag, redistribution_tag, payload, quality)
            values
                (:id, 'AAPL', 'market_data', :now, :now,
                 :available_at, :observed_at, :now, 'fixture', 'replay',
                 :content_hash, :raw_hash, 'available', 'internal',
                 'test-only', 'none', '{}'::jsonb,
                 '{"schema_version":"1.0.0","status":"available","completeness":"1","warnings":[],"fallback_chain":[]}'::jsonb)
            """
        ),
        {
            "id": uuid4(),
            "now": NOW,
            "available_at": available_at,
            "observed_at": max(available_at, NOW),
            "content_hash": "d" * 64,
            "raw_hash": raw_artifact_hash,
        },
    )


def _insert_run(connection: Connection, run_id: UUID) -> None:
    connection.execute(
        text(
            """
            insert into run
                (run_id, run_type, status, as_of, policy_id, idempotency_key,
                 input_hash, created_at, updated_at)
            values
                (:run_id, 'ingestion', 'queued', :now, 'policy/1',
                 :idempotency_key, :input_hash, :now, :now)
            """
        ),
        {
            "run_id": run_id,
            "now": NOW,
            "idempotency_key": f"migration-run-{run_id}",
            "input_hash": "e" * 64,
        },
    )


def _insert_paper_account(connection: Connection, account_id: str) -> None:
    connection.execute(
        text(
            """
            insert into paper_account
                (account_id, base_currency, aggregate_sequence,
                 portfolio_sequence, ledger_sequence, ledger_hash,
                 created_at, updated_at)
            values (:account_id, 'USD', 0, 0, 0, null, :now, :now)
            """
        ),
        {"account_id": account_id, "now": NOW},
    )


def _insert_job(
    connection: Connection,
    *,
    run_id: UUID | None,
    deadline_at: datetime | None,
) -> None:
    connection.execute(
        text(
            """
            insert into job
                (job_id, run_id, job_type, payload, payload_hash, status,
                 idempotency_key, not_before, deadline_at, max_attempts,
                 created_at, updated_at)
            values
                (:job_id, :run_id, 'ingestion', '{}'::jsonb, :payload_hash,
                 'queued', :idempotency_key, :not_before, :deadline_at, 3,
                 :created_at, :created_at)
            """
        ),
        {
            "job_id": uuid4(),
            "run_id": run_id,
            "payload_hash": "f" * 64,
            "idempotency_key": f"migration-job-{uuid4()}",
            "not_before": NOW - timedelta(seconds=1),
            "deadline_at": deadline_at,
            "created_at": NOW,
        },
    )
