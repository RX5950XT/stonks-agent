from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)
ARTIFACT_HASH = "a" * 64
EVIDENCE_HASH = "b" * 64
MANIFEST_HASH = "c" * 64
EVIDENCE_ID = UUID("10000000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("20000000-0000-4000-8000-000000000001")
RUN_ID = UUID("30000000-0000-4000-8000-000000000001")


def test_snapshot_rejects_evidence_available_after_its_as_of(
    clean_database: Engine,
) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(connection, available_at=NOW + timedelta(seconds=1))
        _insert_snapshot(connection, as_of=NOW)

    with (
        pytest.raises(DBAPIError, match="snapshot cannot reference future evidence"),
        clean_database.begin() as connection,
    ):
        _link_snapshot_evidence(connection)


def test_run_rejects_snapshot_later_than_run_as_of(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(connection, available_at=NOW)
        _insert_snapshot(connection, as_of=NOW)
        _link_snapshot_evidence(connection)
        _insert_run(connection, as_of=NOW - timedelta(seconds=1))

    with (
        pytest.raises(DBAPIError, match="run cannot reference a future snapshot"),
        clean_database.begin() as connection,
    ):
        _link_run_snapshot(connection)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    available_offset=st.integers(min_value=-3_600, max_value=3_600),
    evidence_lag=st.integers(min_value=0, max_value=3_600),
    snapshot_offset=st.integers(min_value=-3_600, max_value=3_600),
)
def test_snapshot_evidence_pit_trigger_matches_temporal_order_property(
    clean_database: Engine,
    available_offset: int,
    evidence_lag: int,
    snapshot_offset: int,
) -> None:
    available_at = NOW + timedelta(seconds=available_offset)
    evidence_as_of = available_at + timedelta(seconds=evidence_lag)
    snapshot_as_of = NOW + timedelta(seconds=snapshot_offset)
    connection = clean_database.connect()
    transaction = connection.begin()
    try:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(
            connection,
            available_at=available_at,
            evidence_as_of=evidence_as_of,
        )
        _insert_snapshot(connection, as_of=snapshot_as_of)

        if available_at <= snapshot_as_of and evidence_as_of <= snapshot_as_of:
            _link_snapshot_evidence(connection)
        else:
            with pytest.raises(
                DBAPIError,
                match="snapshot cannot reference future evidence",
            ):
                _link_snapshot_evidence(connection)
    finally:
        transaction.rollback()
        connection.close()


def test_snapshot_rejects_evidence_as_of_after_snapshot_cutoff(
    clean_database: Engine,
) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(
            connection,
            available_at=NOW,
            evidence_as_of=NOW + timedelta(seconds=1),
        )
        _insert_snapshot(connection, as_of=NOW)

    with (
        pytest.raises(DBAPIError, match="snapshot cannot reference future evidence"),
        clean_database.begin() as connection,
    ):
        _link_snapshot_evidence(connection)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    snapshot_offset=st.integers(min_value=-3_600, max_value=3_600),
    run_offset=st.integers(min_value=-3_600, max_value=3_600),
)
def test_run_snapshot_pit_trigger_matches_temporal_order_property(
    clean_database: Engine,
    snapshot_offset: int,
    run_offset: int,
) -> None:
    snapshot_as_of = NOW + timedelta(seconds=snapshot_offset)
    run_as_of = NOW + timedelta(seconds=run_offset)
    connection = clean_database.connect()
    transaction = connection.begin()
    try:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(connection, available_at=NOW - timedelta(hours=2))
        _insert_snapshot(connection, as_of=snapshot_as_of)
        _link_snapshot_evidence(connection)
        _insert_run(connection, as_of=run_as_of)

        if snapshot_as_of <= run_as_of:
            _link_run_snapshot(connection)
        else:
            with pytest.raises(
                DBAPIError,
                match="run cannot reference a future snapshot",
            ):
                _link_run_snapshot(connection)
    finally:
        transaction.rollback()
        connection.close()


def test_valid_run_snapshot_evidence_chain_is_inserted(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(connection, available_at=NOW - timedelta(seconds=1))
        _insert_snapshot(connection, as_of=NOW)
        _link_snapshot_evidence(connection)
        _insert_run(connection, as_of=NOW)
        _link_run_snapshot(connection)

        linked = connection.execute(
            text(
                """
                select evidence_id
                from dataset_snapshot_evidence
                where snapshot_id = :snapshot_id
                """
            ),
            {"snapshot_id": SNAPSHOT_ID},
        ).scalar_one()

    assert linked == EVIDENCE_ID


def test_snapshot_rejects_non_strict_or_unproven_evidence(
    clean_database: Engine,
) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(
            connection,
            available_at=NOW,
            certainty="unknown",
            strict=False,
        )
        _insert_snapshot(connection, as_of=NOW)

    with (
        pytest.raises(DBAPIError, match="snapshot cannot reference future evidence"),
        clean_database.begin() as connection,
    ):
        _link_snapshot_evidence(connection)


def test_snapshot_links_are_append_only(clean_database: Engine) -> None:
    with clean_database.begin() as connection:
        _insert_artifact(connection, ARTIFACT_HASH)
        _insert_artifact(connection, MANIFEST_HASH)
        _insert_evidence(connection, available_at=NOW)
        _insert_snapshot(connection, as_of=NOW)
        _link_snapshot_evidence(connection)

    with (
        pytest.raises(DBAPIError, match="append-only"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text(
                """
                update dataset_snapshot_evidence
                set created_at = :later
                where snapshot_id = :snapshot_id and evidence_id = :evidence_id
                """
            ),
            {
                "later": NOW + timedelta(seconds=1),
                "snapshot_id": SNAPSHOT_ID,
                "evidence_id": EVIDENCE_ID,
            },
        )


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "update run set as_of = as_of + interval '1 second'",
        "update run set input_hash = repeat('f', 64)",
        "update run set policy_id = 'rogue/1'",
        "update run set run_type = 'rogue'",
        "update run set idempotency_key = 'rogue'",
        "update run set created_at = created_at + interval '1 second'",
    ),
)
def test_linked_run_authority_fields_are_immutable(
    clean_database: Engine,
    tamper_sql: str,
) -> None:
    with clean_database.begin() as connection:
        _insert_valid_chain(connection)

    with (
        pytest.raises(DBAPIError, match="linked run authority is immutable"),
        clean_database.begin() as connection,
    ):
        connection.execute(text(tamper_sql))


def test_linked_run_allows_status_version_and_timestamp_transition(
    clean_database: Engine,
) -> None:
    with clean_database.begin() as connection:
        _insert_valid_chain(connection)
        connection.execute(
            text(
                """
                update run
                set status = 'succeeded', version = version + 1,
                    updated_at = updated_at + interval '1 second'
                where run_id = :run_id
                """
            ),
            {"run_id": RUN_ID},
        )
        row = connection.execute(
            text("select status, version, updated_at from run where run_id = :run_id"),
            {"run_id": RUN_ID},
        ).one()

    assert row.status == "succeeded"
    assert row.version == 2
    assert row.updated_at == NOW + timedelta(seconds=1)


@pytest.mark.parametrize("table", ("evidence_item", "dataset_snapshot"))
def test_snapshot_authority_rows_reject_as_of_mutation(
    clean_database: Engine,
    table: str,
) -> None:
    with clean_database.begin() as connection:
        _insert_valid_chain(connection)

    with (
        pytest.raises(DBAPIError, match="append-only"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text(f"update {table} set as_of = as_of - interval '1 second'")
        )


def test_worker_has_only_required_snapshot_completion_grants(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select table_name, privilege_type
                from information_schema.role_table_grants
                where table_schema = 'public'
                  and grantee = 'stonks_worker'
                  and table_name in (
                      'evidence_item', 'dataset_snapshot',
                      'dataset_snapshot_evidence', 'run_dataset_snapshot', 'run'
                  )
                """
            )
        ).all()
        run_updates = (
            connection.execute(
                text(
                    """
                select column_name
                from information_schema.column_privileges
                where table_schema = 'public' and table_name = 'run'
                  and grantee = 'stonks_worker' and privilege_type = 'UPDATE'
                """
                )
            )
            .scalars()
            .all()
        )

    grants = {(row.table_name, row.privilege_type) for row in rows}
    assert ("evidence_item", "INSERT") in grants
    assert ("dataset_snapshot", "INSERT") in grants
    assert ("dataset_snapshot_evidence", "INSERT") in grants
    assert ("run_dataset_snapshot", "INSERT") in grants
    assert ("run", "UPDATE") not in grants
    assert set(run_updates) == {"status", "updated_at", "version"}
    assert ("evidence_item", "UPDATE") not in grants
    assert ("dataset_snapshot", "UPDATE") not in grants


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
    available_at: datetime,
    evidence_as_of: datetime | None = None,
    certainty: str = "proven",
    strict: bool = True,
) -> None:
    connection.execute(
        text(
            """
            insert into evidence_item
                (evidence_id, subject, kind, event_time, published_at,
                 available_at, observed_at, as_of, source, provider,
                 availability_certainty, strict_point_in_time,
                 content_hash, raw_artifact_hash, quality_state, sensitivity,
                 license_tag, redistribution_tag, payload, quality)
            values
                (:id, 'AAPL', 'market_data', :now, :now,
                 :available_at, :observed_at, :evidence_as_of, 'fixture', 'replay',
                 :certainty, :strict,
                 :content_hash, :raw_hash, 'available', 'internal',
                 'test-only', 'synthetic', '{}'::jsonb,
                 '{"schema_version":"1.0.0","status":"available","completeness":"1","warnings":[],"fallback_chain":[]}'::jsonb)
            """
        ),
        {
            "id": EVIDENCE_ID,
            "now": NOW,
            "available_at": available_at,
            "observed_at": available_at,
            "evidence_as_of": evidence_as_of or available_at,
            "certainty": certainty,
            "strict": strict,
            "content_hash": EVIDENCE_HASH,
            "raw_hash": ARTIFACT_HASH,
        },
    )


def _insert_snapshot(connection: Connection, *, as_of: datetime) -> None:
    connection.execute(
        text(
            """
            insert into dataset_snapshot
                (snapshot_id, as_of, cutoff_at, provider_policy_id,
                 manifest_artifact_hash, content_hash, manifest, created_at)
            values
                (:id, :as_of, :as_of, 'replay/1', :manifest_hash,
                 :content_hash, '{}'::jsonb, :as_of)
            """
        ),
        {
            "id": SNAPSHOT_ID,
            "as_of": as_of,
            "manifest_hash": MANIFEST_HASH,
            "content_hash": "d" * 64,
        },
    )


def _link_snapshot_evidence(connection: Connection) -> None:
    connection.execute(
        text(
            """
            insert into dataset_snapshot_evidence
                (snapshot_id, evidence_id, created_at)
            values (:snapshot_id, :evidence_id, :now)
            """
        ),
        {
            "snapshot_id": SNAPSHOT_ID,
            "evidence_id": EVIDENCE_ID,
            "now": NOW,
        },
    )


def _insert_run(connection: Connection, *, as_of: datetime) -> None:
    connection.execute(
        text(
            """
            insert into run
                (run_id, run_type, status, as_of, policy_id, idempotency_key,
                 input_hash, owner_subject, version, created_at, updated_at)
            values
                (:id, 'research', 'pending', :as_of, 'paper/1', 'pit-run',
                 :input_hash, 'test:pit', 1, :as_of, :as_of)
            """
        ),
        {"id": RUN_ID, "as_of": as_of, "input_hash": "e" * 64},
    )


def _link_run_snapshot(connection: Connection) -> None:
    connection.execute(
        text(
            """
            insert into run_dataset_snapshot (run_id, snapshot_id, created_at)
            values (:run_id, :snapshot_id, :now)
            """
        ),
        {"run_id": RUN_ID, "snapshot_id": SNAPSHOT_ID, "now": NOW},
    )


def _insert_valid_chain(connection: Connection) -> None:
    _insert_artifact(connection, ARTIFACT_HASH)
    _insert_artifact(connection, MANIFEST_HASH)
    _insert_evidence(connection, available_at=NOW)
    _insert_snapshot(connection, as_of=NOW)
    _link_snapshot_evidence(connection)
    _insert_run(connection, as_of=NOW)
    _link_run_snapshot(connection)
