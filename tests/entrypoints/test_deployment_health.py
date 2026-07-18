from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.entrypoints.api.deployment import (
    DatabaseReadinessProbe,
    DeploymentReadiness,
    create_deployment_app,
)


class Ready:
    def check(self) -> Success[DeploymentReadiness]:
        return Success(
            DeploymentReadiness(
                database=True,
                schema_current=True,
                execution_mode="paper",
                migration_revision="0017",
            )
        )


class Unready:
    def check(self) -> Failure:
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="Deployment is not ready",
            )
        )


class Exploding:
    def check(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("postgresql://root:secret@database")


def test_liveness_is_db_independent_and_security_headers_are_present() -> None:
    client = TestClient(create_deployment_app(Unready(), build_revision="97f08f5"))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "build_revision": "97f08f5",
        "execution_mode": "paper",
        "status": "alive",
    }
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_readiness_returns_exact_safe_envelope() -> None:
    ready = TestClient(create_deployment_app(Ready(), build_revision="97f08f5"))
    unavailable = TestClient(create_deployment_app(Unready(), build_revision="97f08f5"))

    accepted = ready.get("/readyz")
    rejected = unavailable.get("/readyz")

    assert accepted.status_code == 200
    assert accepted.json()["data"]["migration_revision"] == "0017"
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == ErrorCode.DATA_UNAVAILABLE.value
    assert rejected.json()["data"] is None


def test_readiness_exception_never_leaks_database_details() -> None:
    client = TestClient(create_deployment_app(Exploding(), build_revision="97f08f5"))

    response = client.get("/readyz")

    assert response.status_code == 503
    rendered = response.text
    assert "root" not in rendered
    assert "secret" not in rendered
    assert "postgresql" not in rendered


def test_forwarded_identity_is_rejected_on_health_surface() -> None:
    client = TestClient(create_deployment_app(Ready(), build_revision="97f08f5"))

    response = client.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_INPUT.value


def test_database_readiness_requires_exact_single_migration_head(
    tmp_path: Path,
) -> None:
    database = tmp_path / "readiness.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("create table alembic_version (version_num text)"))
        connection.execute(
            text("insert into alembic_version (version_num) values ('0017')")
        )

    exact = DatabaseReadinessProbe(engine, expected_revision="0017").check()
    stale = DatabaseReadinessProbe(engine, expected_revision="0018").check()
    with engine.begin() as connection:
        connection.execute(
            text("insert into alembic_version (version_num) values ('other')")
        )
    multiple = DatabaseReadinessProbe(engine, expected_revision="0017").check()
    engine.dispose()

    assert isinstance(exact, Success)
    assert exact.value.schema_current is True
    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.DATA_UNAVAILABLE
    assert isinstance(multiple, Failure)
    assert multiple.error.code is ErrorCode.DATA_UNAVAILABLE


def test_database_readiness_fails_closed_when_database_is_unavailable() -> None:
    engine = create_engine("sqlite:///Z:/missing-parent/readiness.sqlite")

    result = DatabaseReadinessProbe(engine, expected_revision="0017").check()

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.DATA_UNAVAILABLE
