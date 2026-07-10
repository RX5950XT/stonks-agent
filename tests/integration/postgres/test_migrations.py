from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

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
    "run",
    "run_event",
    "job",
    "outbox",
    "inbox",
    "provider_health",
    "usage_budget",
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
            text("update artifact_manifest set size_bytes = 2 where content_hash = :hash"),
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
                select grantee, privilege_type
                from information_schema.role_table_grants
                where table_schema = 'public'
                  and table_name = 'artifact_manifest'
                  and grantee in ('stonks_app', 'stonks_worker', 'stonks_reader')
                """
            )
        ).all()

    grants = {(row.grantee, row.privilege_type) for row in rows}
    assert ("stonks_app", "SELECT") in grants
    assert ("stonks_app", "INSERT") in grants
    assert ("stonks_reader", "SELECT") in grants
    assert ("stonks_app", "UPDATE") not in grants
    assert ("stonks_app", "DELETE") not in grants


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
                 license_tag, redistribution_tag, payload)
            values
                (:id, 'AAPL', 'market_data', :now, :now,
                 :available_at, :observed_at, :now, 'fixture', 'replay',
                 :content_hash, :raw_hash, 'available', 'internal',
                 'test-only', 'none', '{}'::jsonb)
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
