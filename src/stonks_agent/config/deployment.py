"""Fail-closed deployment settings with secret-file database credentials."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import URL

from stonks_agent.domain.errors import ErrorCode, StructuredError

_MAX_PASSWORD_BYTES = 257
_DEPLOYMENT_PREFIX = "STONKS_DEPLOYMENT_"
_ENTRYPOINT_DEPLOYMENT_KEYS = frozenset({"STONKS_DEPLOYMENT_ROOT"})
_DATABASE_KEYS = frozenset(
    {
        "STONKS_DB_HOST",
        "STONKS_DB_PORT",
        "STONKS_DB_NAME",
        "STONKS_DB_USER",
        "STONKS_DB_PASSWORD_FILE",
        "STONKS_DB_CONNECT_TIMEOUT_SECONDS",
        "STONKS_DB_POOL_SIZE",
        "STONKS_DB_MAX_OVERFLOW",
    }
)
_PROHIBITED_DATABASE_KEYS = frozenset(
    {
        "DATABASE_URL",
        "SQLALCHEMY_URL",
        "STONKS_DATABASE_URL",
        "PGPASSWORD",
        "PGPASSFILE",
        "PGSERVICE",
        "PGSERVICEFILE",
    }
)
_RUNTIME_ROLE_KEYS = frozenset(
    {
        "STONKS_RUNTIME_DB_USER",
        "STONKS_RUNTIME_DB_PASSWORD_FILE",
    }
)


class DeploymentDatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$",
    )
    port: int = Field(ge=1, le=65_535)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    user: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    password_file: Path
    connect_timeout_seconds: int = Field(ge=1, le=10)
    pool_size: int = Field(ge=1, le=32)
    max_overflow: int = Field(ge=0, le=64)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if ".." in value or value.endswith("."):
            raise ValueError("database host is not canonical")
        return value

    def validate_password_file(self) -> None:
        _read_password(self.password_file)

    def sqlalchemy_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.user,
            password=_read_password(self.password_file),
            host=self.host,
            port=self.port,
            database=self.name,
        )


class DeploymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Literal["staging", "production"]
    execution_mode: Literal["paper"]
    build_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    deployment_root: Literal["/opt/stonks"]
    server_host: Literal["0.0.0.0", "127.0.0.1"]
    server_port: int = Field(ge=1, le=65_535)
    database: DeploymentDatabaseSettings


class RuntimeDatabaseRoleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    login_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    group_role: Literal["stonks_app"] = "stonks_app"
    password_file: Path
    owner_user: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")

    @model_validator(mode="after")
    def validate_separation(self) -> Self:
        if self.login_name in {self.owner_user, self.group_role}:
            raise ValueError("runtime database login is not separated")
        return self

    def validate_password_file(self) -> None:
        _read_password(self.password_file)

    def reveal_password(self) -> str:
        return _read_password(self.password_file)


class DeploymentConfigurationError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Deployment configuration is invalid")


def load_deployment_settings(
    environment: Mapping[str, str],
) -> DeploymentSettings:
    """Load only the closed deployment surface; never accept a raw DSN."""

    try:
        if _PROHIBITED_DATABASE_KEYS & environment.keys():
            raise ValueError("ambient database authority is prohibited")
        if any(
            name.startswith("STONKS_DB_") and name not in _DATABASE_KEYS
            for name in environment
        ):
            raise ValueError("unknown database setting")
        if any(
            name.startswith(_DEPLOYMENT_PREFIX)
            and name not in _ENTRYPOINT_DEPLOYMENT_KEYS
            for name in environment
        ):
            raise ValueError("unknown deployment setting")
        settings = DeploymentSettings.model_validate(
            {
                "environment": _required(environment, "STONKS_ENVIRONMENT"),
                "execution_mode": _required(environment, "STONKS_EXECUTION_MODE"),
                "build_revision": _required(environment, "STONKS_BUILD_REVISION"),
                "deployment_root": _required(environment, "STONKS_DEPLOYMENT_ROOT"),
                "server_host": _required(environment, "STONKS_SERVER_HOST"),
                "server_port": _required(environment, "STONKS_SERVER_PORT"),
                "database": {
                    "host": _required(environment, "STONKS_DB_HOST"),
                    "port": _required(environment, "STONKS_DB_PORT"),
                    "name": _required(environment, "STONKS_DB_NAME"),
                    "user": _required(environment, "STONKS_DB_USER"),
                    "password_file": _required(environment, "STONKS_DB_PASSWORD_FILE"),
                    "connect_timeout_seconds": _required(
                        environment,
                        "STONKS_DB_CONNECT_TIMEOUT_SECONDS",
                    ),
                    "pool_size": _required(environment, "STONKS_DB_POOL_SIZE"),
                    "max_overflow": _required(environment, "STONKS_DB_MAX_OVERFLOW"),
                },
            }
        )
        settings.database.validate_password_file()
        return settings
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise DeploymentConfigurationError(_configuration_error()) from error


def load_runtime_role_settings(
    environment: Mapping[str, str],
    *,
    owner_user: str,
) -> RuntimeDatabaseRoleSettings:
    try:
        if any(
            name.startswith("STONKS_RUNTIME_DB_") and name not in _RUNTIME_ROLE_KEYS
            for name in environment
        ):
            raise ValueError("unknown runtime database role setting")
        role = RuntimeDatabaseRoleSettings(
            login_name=_required(environment, "STONKS_RUNTIME_DB_USER"),
            password_file=Path(
                _required(environment, "STONKS_RUNTIME_DB_PASSWORD_FILE")
            ),
            owner_user=owner_user,
        )
        role.validate_password_file()
        return role
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise DeploymentConfigurationError(_configuration_error()) from error


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value or value.strip() != value:
        raise ValueError("required deployment setting is missing")
    return value


def _read_password(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("database password file is invalid")
    payload = path.read_bytes()
    if len(payload) > _MAX_PASSWORD_BYTES:
        raise ValueError("database password is too large")
    if payload.endswith(b"\n"):
        payload = payload[:-1]
        if payload.endswith(b"\r"):
            payload = payload[:-1]
    value = payload.decode("utf-8")
    if (
        not 1 <= len(value) <= 256
        or value.strip() != value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("database password is invalid")
    return value


def _configuration_error() -> StructuredError:
    return StructuredError(
        code=ErrorCode.CONFIGURATION_INVALID,
        message="Deployment configuration is invalid",
        details={"component": "deployment"},
    )
