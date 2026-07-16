"""Fail-closed runtime composition for human and service API authentication."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import httpx
from pydantic import ValidationError

from stonks_agent.adapters.auth.local_token import (
    DenyAllAuthenticator,
    LocalTokenAuthenticator,
    LocalTokenConfigurationError,
)
from stonks_agent.adapters.auth.oidc import (
    HTTPSJWKSetProvider,
    OIDCAuthenticator,
    OIDCSettings,
)
from stonks_agent.adapters.auth.service_credentials import (
    load_rs256_service_credential_provider,
)
from stonks_agent.config.rbac import RBACPolicyLoadError, load_rbac_policy
from stonks_agent.config.settings import Settings
from stonks_agent.domain.auth import AccessTarget, ResourceKind, Role
from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.ports.authentication import Authenticator
from stonks_agent.ports.service_credentials import ServiceCredentialProvider

_LOCAL_ENVIRONMENTS = frozenset({"local", "development", "test"})
_OIDC_MODE = "oidc"
_LOCAL_TOKEN_MODE = "local_token"
_DENY_ALL_MODE = "deny_all"


class AuthenticationCompositionError(RuntimeError):
    """Raised when runtime auth cannot be composed without reducing authority."""

    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Authentication configuration is invalid")


def create_authenticator(
    settings: Settings,
    *,
    environment: Mapping[str, str] | None = None,
    http_client: httpx.Client | None = None,
) -> Authenticator:
    """Build one explicit auth strategy; deployed environments require OIDC."""

    variables = os.environ if environment is None else environment
    try:
        return _create_authenticator(settings, variables, http_client)
    except (
        LocalTokenConfigurationError,
        RBACPolicyLoadError,
        ValidationError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        raise AuthenticationCompositionError(_configuration_error()) from error


def create_service_credential_provider(
    settings: Settings,
    *,
    environment: Mapping[str, str] | None = None,
) -> ServiceCredentialProvider:
    """Compose the core-only asymmetric issuer for one deployed runtime."""

    variables = os.environ if environment is None else environment
    try:
        if settings.environment in _LOCAL_ENVIRONMENTS:
            raise ValueError("local runtime cannot mint production service credentials")
        if variables.get("STONKS_SERVICE_AUTH_MODE") != _OIDC_MODE:
            raise ValueError("deployed service authentication mode is required")
        return load_rs256_service_credential_provider(variables)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise AuthenticationCompositionError(_configuration_error()) from error


def _create_authenticator(
    settings: Settings,
    variables: Mapping[str, str],
    http_client: httpx.Client | None,
) -> Authenticator:
    mode = variables.get("STONKS_AUTH_MODE")
    is_local = settings.environment in _LOCAL_ENVIRONMENTS
    if mode is None:
        if is_local:
            return DenyAllAuthenticator()
        raise ValueError("deployed authentication mode is required")
    if mode == _OIDC_MODE:
        return _oidc_authenticator(variables, http_client)
    if mode == _LOCAL_TOKEN_MODE and is_local:
        return _local_token_authenticator(settings.environment, variables)
    if mode == _DENY_ALL_MODE and is_local:
        return DenyAllAuthenticator()
    raise ValueError("authentication mode is unavailable")


def _oidc_authenticator(
    variables: Mapping[str, str],
    http_client: httpx.Client | None,
) -> OIDCAuthenticator:
    oidc = OIDCSettings.model_validate(
        {
            "issuer": _required(variables, "STONKS_OIDC_ISSUER"),
            "audience": _required(variables, "STONKS_OIDC_AUDIENCE"),
            "jwks_url": _required(variables, "STONKS_OIDC_JWKS_URL"),
            "allowed_algorithms": _csv(
                _required(variables, "STONKS_OIDC_ALLOWED_ALGORITHMS"),
                maximum=3,
            ),
            "allowed_client_ids": _csv(
                _required(variables, "STONKS_OIDC_ALLOWED_CLIENT_IDS"),
                maximum=32,
            ),
            "max_token_lifetime_seconds": _integer(
                variables,
                "STONKS_OIDC_MAX_TOKEN_LIFETIME_SECONDS",
            ),
            "clock_skew_seconds": _integer(
                variables,
                "STONKS_OIDC_CLOCK_SKEW_SECONDS",
            ),
            "jwks_cache_seconds": _optional_integer(
                variables,
                "STONKS_OIDC_JWKS_CACHE_SECONDS",
                300,
            ),
            "jwks_min_refresh_seconds": _optional_integer(
                variables,
                "STONKS_OIDC_JWKS_MIN_REFRESH_SECONDS",
                10,
            ),
            "jwks_timeout_seconds": _optional_float(
                variables,
                "STONKS_OIDC_JWKS_TIMEOUT_SECONDS",
                5.0,
            ),
        }
    )
    policy = load_rbac_policy(Path(_required(variables, "STONKS_RBAC_POLICY_PATH")))
    client = http_client or httpx.Client()
    return OIDCAuthenticator(
        settings=oidc,
        policy=policy,
        keys=HTTPSJWKSetProvider(oidc, client),
    )


def _local_token_authenticator(
    environment: str,
    variables: Mapping[str, str],
) -> LocalTokenAuthenticator:
    roles = frozenset(
        Role(value)
        for value in _csv(
            _required(variables, "STONKS_LOCAL_AUTH_ROLES"),
            maximum=5,
        )
    )
    if not roles:
        raise ValueError("local roles are required")
    return LocalTokenAuthenticator(
        environment=environment,
        token=_required(variables, "STONKS_LOCAL_AUTH_TOKEN"),
        subject=_required(variables, "STONKS_LOCAL_AUTH_SUBJECT"),
        roles=roles,
        targets=_local_targets(variables.get("STONKS_LOCAL_AUTH_TARGETS")),
    )


def _local_targets(value: str | None) -> frozenset[AccessTarget]:
    if value is None:
        return frozenset()
    targets: set[AccessTarget] = set()
    for encoded in _csv(value, maximum=256):
        kind, separator, identifier = encoded.partition(":")
        if separator != ":" or not identifier:
            raise ValueError("local target is invalid")
        targets.add(AccessTarget(kind=ResourceKind(kind), identifier=identifier))
    return frozenset(targets)


def _required(variables: Mapping[str, str], name: str) -> str:
    value = variables.get(name)
    if value is None or not value or value.strip() != value:
        raise ValueError("required authentication setting is missing")
    return value


def _csv(value: str, *, maximum: int) -> tuple[str, ...]:
    values = tuple(value.split(","))
    if (
        not 1 <= len(values) <= maximum
        or len(values) != len(set(values))
        or any(not item or item.strip() != item for item in values)
    ):
        raise ValueError("authentication allowlist is invalid")
    return values


def _integer(variables: Mapping[str, str], name: str) -> int:
    return int(_required(variables, name))


def _optional_integer(
    variables: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    value = variables.get(name)
    return default if value is None else int(value)


def _optional_float(
    variables: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    value = variables.get(name)
    return default if value is None else float(value)


def _configuration_error() -> StructuredError:
    return StructuredError(
        code=ErrorCode.CONFIGURATION_INVALID,
        message="Authentication configuration is invalid",
        details={"component": "authentication"},
    )
