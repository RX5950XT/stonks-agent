from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def postgres_url() -> str:
    value = os.environ.get("STONKS_TEST_DATABASE_URL")
    if not value:
        pytest.fail("STONKS_TEST_DATABASE_URL is required for PostgreSQL tests")
    return value


@pytest.fixture(scope="session")
def alembic_config(postgres_url: str) -> Config:
    config = Config(ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", postgres_url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def migrated_engine(
    postgres_url: str,
    alembic_config: Config,
) -> Iterator[Engine]:
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(text("drop schema if exists public cascade"))
        connection.execute(text("create schema public"))
        connection.execute(text("grant all on schema public to public"))
    command.upgrade(alembic_config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def clean_database(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                """
                truncate table
                    artifact_maintenance_event, artifact_maintenance_head,
                    paper_operator_action, paper_operator_audit_head,
                    paper_portfolio_valuation,
                    journal_posting, journal_transaction, paper_execution_receipt,
                    paper_fill,
                    order_event, order_intent, reservation_event,
                    account_reservation, risk_decision, portfolio_target,
                    paper_ledger_account_projection,
                    paper_account_opening_snapshot,
                    paper_cash_projection, paper_position_projection,
                    paper_account_event, paper_kill_switch, paper_account,
                    strategy_audit_event, strategy_evaluation_report,
                    strategy_registry,
                    evidence_edge, dataset_snapshot_evidence,
                    run_dataset_snapshot, run_event, job, outbox, inbox,
                    evidence_item, dataset_snapshot, instrument_alias,
                    instrument, trading_calendar_version, provider_health,
                    usage_budget, run, artifact_manifest
                cascade
                """
            )
        )
        connection.execute(
            text(
                """
                insert into artifact_maintenance_head
                    (head_id, sequence, event_hash, created_at, updated_at)
                values (1, 0, null, clock_timestamp(), clock_timestamp())
                """
            )
        )
        connection.execute(
            text(
                """
                insert into paper_operator_audit_head
                    (head_id, sequence, action_hash, created_at, updated_at)
                values (1, 0, null, clock_timestamp(), clock_timestamp())
                """
            )
        )
        connection.execute(
            text(
                """
                insert into paper_kill_switch
                    (switch_id, scope, account_id, active, reason_code, actor,
                     version, created_at, updated_at)
                values
                    ('46000000-0000-4000-8000-000000000000', 'global', null,
                     false, 'test_initialized', 'system:test', 1,
                     clock_timestamp(), clock_timestamp())
                """
            )
        )
    return migrated_engine
