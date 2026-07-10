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
                    evidence_edge, run_event, job, outbox, inbox,
                    evidence_item, dataset_snapshot, instrument_alias,
                    instrument, trading_calendar_version, provider_health,
                    usage_budget, run, artifact_manifest
                cascade
                """
            )
        )
    return migrated_engine
