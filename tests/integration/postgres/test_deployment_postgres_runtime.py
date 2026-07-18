from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from stonks_agent.config.deployment import RuntimeDatabaseRoleSettings
from stonks_agent.domain.errors import Success
from stonks_agent.entrypoints.api.deployment import DatabaseReadinessProbe
from stonks_agent.entrypoints.deployment import (
    migrate_database,
    packaged_migration_head,
)

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[3]
LOGIN = "stonks_deployment_test"
PASSWORD = "deployment-runtime-secret"


def test_migration_bootstraps_least_privilege_runtime_and_is_idempotent(
    clean_database: Engine,
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "runtime-password"
    password_file.write_text(PASSWORD, encoding="utf-8")
    role = RuntimeDatabaseRoleSettings(
        login_name=LOGIN,
        password_file=password_file,
        owner_user=clean_database.url.username or "postgres",
    )
    head = packaged_migration_head(
        ROOT / "alembic.ini",
        script_location=ROOT / "migrations",
    )
    runtime_url = clean_database.url.set(username=LOGIN, password=PASSWORD)

    try:
        for _ in range(2):
            migrate_database(
                clean_database,
                database_url=clean_database.url,
                alembic_path=ROOT / "alembic.ini",
                script_location=ROOT / "migrations",
                runtime_role=role,
                expected_revision=head,
            )

        runtime = create_engine(runtime_url, pool_pre_ping=True)
        try:
            readiness = DatabaseReadinessProbe(
                runtime,
                expected_revision=head,
            ).check()
            assert isinstance(readiness, Success)
            with runtime.connect() as connection:
                identity = connection.execute(
                    text(
                        """
                        select current_user,
                               pg_has_role(current_user, 'stonks_app', 'member'),
                               rolsuper, rolcreatedb, rolcreaterole,
                               rolreplication, rolbypassrls
                          from pg_roles
                         where rolname = current_user
                        """
                    )
                ).one()
                assert identity == (LOGIN, True, False, False, False, False, False)
                with pytest.raises(DBAPIError):
                    connection.execute(
                        text("create table forbidden_deployment_write(x int)")
                    )
                connection.rollback()
                with pytest.raises(DBAPIError):
                    connection.execute(text("set role postgres"))
                connection.rollback()
        finally:
            runtime.dispose()
    finally:
        with clean_database.begin() as connection:
            connection.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where usename = :login and pid <> pg_backend_pid()"
                ),
                {"login": LOGIN},
            )
            connection.execute(text("drop role if exists stonks_deployment_test"))
