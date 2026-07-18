"""Minimal deployment control surface for liveness and database readiness."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.entrypoints.api.api_security import install_api_security
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope

_MAX_HEALTH_REQUEST_BYTES = 4096


class DeploymentReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database: Literal[True]
    schema_current: Literal[True]
    execution_mode: Literal["paper"]
    migration_revision: str = Field(pattern=r"^[0-9a-z_]{1,64}$")


@runtime_checkable
class ReadinessProbe(Protocol):
    def check(self) -> Result[DeploymentReadiness]: ...


class DatabaseReadinessProbe:
    """Read-only bounded readiness check; optional services are not consulted."""

    def __init__(self, engine: Engine, *, expected_revision: str) -> None:
        if not expected_revision or len(expected_revision) > 64:
            raise ValueError("expected migration revision is invalid")
        self._engine = engine
        self._expected_revision = expected_revision

    def check(self) -> Result[DeploymentReadiness]:
        try:
            with self._engine.connect() as connection:
                connection.execute(text("select 1")).scalar_one()
                revisions = tuple(
                    connection.execute(
                        text("select version_num from alembic_version")
                    ).scalars()
                )
            if revisions != (self._expected_revision,):
                return _unavailable()
            return Success(
                DeploymentReadiness(
                    database=True,
                    schema_current=True,
                    execution_mode="paper",
                    migration_revision=self._expected_revision,
                )
            )
        except (SQLAlchemyError, TypeError, ValueError):
            return _unavailable()


def create_deployment_app(
    probe: ReadinessProbe,
    *,
    build_revision: str,
) -> FastAPI:
    app = FastAPI(
        title="Stonks Agent Deployment Health",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_api_security(app, max_request_bytes=_MAX_HEALTH_REQUEST_BYTES)
    app.add_api_route(
        "/healthz",
        _LivenessEndpoint(build_revision),
        methods=["GET"],
    )
    app.add_api_route("/readyz", _ReadinessEndpoint(probe), methods=["GET"])
    return app


class _LivenessEndpoint:
    def __init__(self, build_revision: str) -> None:
        self._build_revision = build_revision

    def __call__(self) -> JSONResponse:
        envelope = success_envelope(
            {
                "build_revision": self._build_revision,
                "execution_mode": "paper",
                "status": "alive",
            }
        )
        return JSONResponse(
            status_code=envelope.status,
            content=envelope.model_dump(mode="json"),
        )


class _ReadinessEndpoint:
    def __init__(self, probe: ReadinessProbe) -> None:
        self._probe = probe

    def __call__(self) -> JSONResponse:
        try:
            result = self._probe.check()
        except Exception:
            result = _unavailable()
        if isinstance(result, Failure):
            rejected = error_envelope(_not_ready_error())
            return JSONResponse(
                status_code=rejected.status,
                content=rejected.model_dump(mode="json"),
            )
        accepted = success_envelope(result.value)
        return JSONResponse(
            status_code=accepted.status,
            content=accepted.model_dump(mode="json"),
        )


def _unavailable() -> Failure:
    return Failure(_not_ready_error())


def _not_ready_error() -> StructuredError:
    return StructuredError(
        code=ErrorCode.DATA_UNAVAILABLE,
        message="Deployment is not ready",
        details={"component": "core"},
    )
