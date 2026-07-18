"""Typed production deployment commands for migrate, serve, and probes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Never

import httpx
import typer
import uvicorn
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError

from stonks_agent.config.deployment import (
    DeploymentSettings,
    RuntimeDatabaseRoleSettings,
    load_deployment_settings,
    load_runtime_role_settings,
)
from stonks_agent.domain.errors import Failure
from stonks_agent.entrypoints.api.deployment import (
    DatabaseReadinessProbe,
    create_deployment_app,
)
from stonks_agent.entrypoints.api.envelope import (
    success_envelope,
    unexpected_error_envelope,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)
_MIGRATION_LOCK_ID = 8_639_142_607_001


class DeploymentRuntimeError(RuntimeError):
    """Public-safe deployment failure."""

    def __init__(self) -> None:
        super().__init__("Deployment runtime failed")


@app.callback()
def main() -> None:
    """Stonks Agent production deployment commands."""


@app.command("serve")
def serve() -> None:
    """Run one hardened health/readiness process without proxy trust."""

    try:
        environment = dict(os.environ)
        settings = load_deployment_settings(environment)
        root = deployment_root(settings.deployment_root)
        expected = packaged_migration_head(
            root / "alembic.ini",
            script_location=root / "migrations",
        )
        engine = create_runtime_engine(settings)
        runtime_app = create_deployment_app(
            DatabaseReadinessProbe(engine, expected_revision=expected),
            build_revision=settings.build_revision,
        )
        uvicorn.run(
            runtime_app,
            host=settings.server_host,
            port=settings.server_port,
            workers=1,
            proxy_headers=False,
            forwarded_allow_ips="",
            server_header=False,
            date_header=False,
            access_log=False,
            backlog=128,
            limit_concurrency=128,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=30,
        )
    except Exception as error:
        _fail_deployment(error)
    finally:
        if "engine" in locals():
            engine.dispose()


@app.command("migrate")
def migrate() -> None:
    """Run an explicit one-shot migration and least-privilege login bootstrap."""

    try:
        environment = dict(os.environ)
        settings = load_deployment_settings(environment)
        role = load_runtime_role_settings(
            environment,
            owner_user=settings.database.user,
        )
        root = deployment_root(settings.deployment_root)
        expected = packaged_migration_head(
            root / "alembic.ini",
            script_location=root / "migrations",
        )
        engine = create_runtime_engine(settings)
        migrate_database(
            engine,
            database_url=settings.database.sqlalchemy_url(),
            alembic_path=root / "alembic.ini",
            script_location=root / "migrations",
            runtime_role=role,
            expected_revision=expected,
        )
        typer.echo(
            success_envelope(
                {
                    "migration_revision": expected,
                    "runtime_role": role.login_name,
                    "status": "ready",
                }
            ).model_dump_json()
        )
    except Exception as error:
        _fail_deployment(error)
    finally:
        if "engine" in locals():
            engine.dispose()


@app.command("probe")
def probe(
    target: Annotated[
        str,
        typer.Option(help="Probe target: live or ready"),
    ] = "ready",
) -> None:
    """Probe only the same container over loopback without ambient proxy state."""

    if target not in {"live", "ready"}:
        raise typer.BadParameter("target must be live or ready")
    try:
        port = int(os.environ["STONKS_SERVER_PORT"])
        if not 1 <= port <= 65_535:
            raise ValueError("server port is invalid")
        path = "healthz" if target == "live" else "readyz"
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(2.0),
        ) as client:
            response = client.get(f"http://127.0.0.1:{port}/{path}")
        if response.status_code != 200:
            raise typer.Exit(code=1)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        raise typer.Exit(code=1) from error


def deployment_root(raw: str) -> Path:
    if raw != "/opt/stonks":
        raise DeploymentRuntimeError()
    root = Path(raw)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise DeploymentRuntimeError()
    return root


def packaged_migration_head(
    alembic_path: Path,
    *,
    script_location: Path,
) -> str:
    try:
        if (
            not alembic_path.is_file()
            or not script_location.is_dir()
            or alembic_path.is_symlink()
            or script_location.is_symlink()
        ):
            raise ValueError("migration assets are invalid")
        config = Config(alembic_path)
        config.set_main_option("script_location", str(script_location))
        heads = ScriptDirectory.from_config(config).get_heads()
        if len(heads) != 1:
            raise ValueError("migration history must have one head")
        head = heads[0]
        if not head or len(head) > 64:
            raise ValueError("migration head is invalid")
        return head
    except Exception as error:
        raise DeploymentRuntimeError() from error


def create_runtime_engine(settings: DeploymentSettings) -> Engine:
    return create_engine(
        settings.database.sqlalchemy_url(),
        pool_pre_ping=True,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        connect_args={
            "application_name": "stonks-agent-core",
            "connect_timeout": settings.database.connect_timeout_seconds,
            "options": "-c statement_timeout=30000 -c lock_timeout=5000",
            "sslmode": "disable",
        },
        hide_parameters=True,
    )


def migrate_database(
    engine: Engine,
    *,
    database_url: URL,
    alembic_path: Path,
    script_location: Path,
    runtime_role: RuntimeDatabaseRoleSettings,
    expected_revision: str,
) -> None:
    try:
        with engine.connect() as lock_connection:
            acquired = lock_connection.scalar(
                text("select pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )
            if acquired is not True:
                raise ValueError("migration lock is unavailable")
            lock_connection.commit()
            try:
                _upgrade(
                    database_url,
                    alembic_path=alembic_path,
                    script_location=script_location,
                )
                provision_runtime_role(lock_connection, runtime_role)
                lock_connection.commit()
            finally:
                lock_connection.rollback()
                lock_connection.execute(
                    text("select pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _MIGRATION_LOCK_ID},
                )
                lock_connection.commit()
        checked = DatabaseReadinessProbe(
            engine,
            expected_revision=expected_revision,
        ).check()
        if isinstance(checked, Failure):
            raise ValueError("migrated database is not ready")
    except (OSError, SQLAlchemyError, TypeError, ValueError) as error:
        raise DeploymentRuntimeError() from error


def provision_runtime_role(
    connection: Connection,
    settings: RuntimeDatabaseRoleSettings,
) -> None:
    driver = connection.connection.driver_connection
    if driver is None:
        raise ValueError("database driver connection is unavailable")
    verifier = driver.pgconn.encrypt_password(
        settings.reveal_password().encode("utf-8"),
        settings.login_name.encode("ascii"),
        b"scram-sha-256",
    )
    if (
        not verifier.startswith(b"SCRAM-SHA-256$")
        or len(verifier) > 512
        or not verifier.isascii()
    ):
        raise ValueError("database password verifier is invalid")
    with driver.cursor() as cursor:
        cursor.execute(
            "select exists(select 1 from pg_roles where rolname = %s)",
            (settings.login_name,),
        )
        exists = cursor.fetchone()
        if exists is None or not isinstance(exists[0], bool):
            raise ValueError("database role state is invalid")
        role_statement = (
            sql.SQL(
                "alter role {} with login inherit nosuperuser "
                "nocreatedb nocreaterole noreplication nobypassrls "
                "connection limit 16 password {}"
            )
            if exists[0]
            else sql.SQL(
                "create role {} with login inherit nosuperuser "
                "nocreatedb nocreaterole noreplication nobypassrls "
                "connection limit 16 password {}"
            )
        )
        cursor.execute(
            role_statement.format(
                sql.Identifier(settings.login_name),
                sql.Literal(verifier.decode("ascii")),
            )
        )
        cursor.execute(
            sql.SQL("grant {} to {}").format(
                sql.Identifier(settings.group_role),
                sql.Identifier(settings.login_name),
            )
        )
        cursor.execute(
            sql.SQL("grant select on table alembic_version to {}").format(
                sql.Identifier(settings.group_role)
            )
        )
        cursor.execute(
            sql.SQL("alter role {} set statement_timeout = '30s'").format(
                sql.Identifier(settings.login_name)
            )
        )


def _upgrade(
    database_url: URL,
    *,
    alembic_path: Path,
    script_location: Path,
) -> None:
    config = Config(alembic_path)
    config.set_main_option("script_location", str(script_location))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    command.upgrade(config, "head")


def _fail_deployment(error: BaseException) -> Never:
    typer.echo(unexpected_error_envelope(error).model_dump_json(), err=True)
    raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
