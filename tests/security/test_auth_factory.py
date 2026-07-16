from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from stonks_agent.adapters.auth.factory import (
    AuthenticationCompositionError,
    create_authenticator,
    create_service_credential_provider,
)
from stonks_agent.adapters.auth.local_token import (
    DenyAllAuthenticator,
    LocalTokenAuthenticator,
)
from stonks_agent.adapters.auth.oidc import OIDCAuthenticator
from stonks_agent.config.settings import Settings
from stonks_agent.domain.auth import AccessTarget, ResourceKind, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.ports.authentication import AuthenticationRequest
from stonks_agent.ports.service_credentials import ServiceReceiver

ROOT = Path(__file__).resolve().parents[2]
LOCAL_TOKEN = "local-composition-token-at-least-32-characters"


def _oidc_environment() -> dict[str, str]:
    return {
        "STONKS_AUTH_MODE": "oidc",
        "STONKS_OIDC_ISSUER": "https://identity.example.test",
        "STONKS_OIDC_AUDIENCE": "stonks-core-api",
        "STONKS_OIDC_JWKS_URL": ("https://identity.example.test/.well-known/jwks.json"),
        "STONKS_OIDC_ALLOWED_ALGORITHMS": "RS256",
        "STONKS_OIDC_ALLOWED_CLIENT_IDS": "stonks-web,stonks-core-runner",
        "STONKS_OIDC_MAX_TOKEN_LIFETIME_SECONDS": "900",
        "STONKS_OIDC_CLOCK_SKEW_SECONDS": "30",
        "STONKS_RBAC_POLICY_PATH": str(ROOT / "config" / "rbac.yaml"),
    }


def _client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request)
        )
    )


def test_production_composition_builds_only_oidc_authenticator() -> None:
    with _client() as client:
        authenticator = create_authenticator(
            Settings(environment="production"),
            environment=_oidc_environment(),
            http_client=client,
        )

    assert isinstance(authenticator, OIDCAuthenticator)
    assert "identity.example.test" in repr(authenticator)


def test_production_composition_builds_core_only_service_issuer(
    tmp_path: Path,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "service-signing-key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    variables = {
        "STONKS_SERVICE_AUTH_MODE": "oidc",
        "STONKS_SERVICE_ISSUER": "https://identity.example.test",
        "STONKS_SERVICE_CORE_SUBJECT": "service:core-runner",
        "STONKS_SERVICE_CORE_CLIENT_ID": "stonks-core-runner",
        "STONKS_SERVICE_SIGNING_KEY_ID": "core-key-2026-07",
        "STONKS_SERVICE_SIGNING_KEY_FILE": str(key_file.resolve()),
        **{
            f"STONKS_SERVICE_AUDIENCE_{receiver.value.upper()}": (
                f"stonks-{receiver.value}-ingress"
            )
            for receiver in ServiceReceiver
        },
    }

    provider = create_service_credential_provider(
        Settings(environment="production"),
        environment=variables,
    )

    assert "identity.example.test" in repr(provider)


@pytest.mark.parametrize("environment", ["local", "development", "test"])
def test_local_runtime_cannot_mint_production_service_credentials(
    environment: str,
) -> None:
    with pytest.raises(AuthenticationCompositionError):
        create_service_credential_provider(
            Settings(environment=environment),
            environment={"STONKS_SERVICE_AUTH_MODE": "oidc"},
        )


@pytest.mark.parametrize("environment", ["staging", "production", "preview"])
def test_deployed_environment_requires_explicit_complete_oidc_configuration(
    environment: str,
) -> None:
    with _client() as client, pytest.raises(AuthenticationCompositionError) as raised:
        create_authenticator(
            Settings(environment=environment),
            environment={},
            http_client=client,
        )

    assert raised.value.error.code is ErrorCode.CONFIGURATION_INVALID
    assert raised.value.error.details == {"component": "authentication"}


def test_deployed_environment_cannot_select_local_token_authentication() -> None:
    variables = {
        "STONKS_AUTH_MODE": "local_token",
        "STONKS_LOCAL_AUTH_TOKEN": LOCAL_TOKEN,
        "STONKS_LOCAL_AUTH_SUBJECT": "local-researcher",
        "STONKS_LOCAL_AUTH_ROLES": "researcher",
    }

    with _client() as client, pytest.raises(AuthenticationCompositionError) as raised:
        create_authenticator(
            Settings(environment="production"),
            environment=variables,
            http_client=client,
        )

    rendered = str(raised.value) + repr(raised.value.error)
    assert LOCAL_TOKEN not in rendered
    assert raised.value.error.code is ErrorCode.CONFIGURATION_INVALID


def test_local_default_is_deny_all_until_auth_mode_is_explicit() -> None:
    with _client() as client:
        authenticator = create_authenticator(
            Settings(environment="local"),
            environment={},
            http_client=client,
        )

    result = authenticator.authenticate(
        AuthenticationRequest(
            authorization="Bearer attacker-controlled",
            client_host="127.0.0.1",
        )
    )
    assert isinstance(authenticator, DenyAllAuthenticator)
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.UNAUTHORIZED


def test_explicit_local_token_composition_maps_bounded_roles_and_targets() -> None:
    variables = {
        "STONKS_AUTH_MODE": "local_token",
        "STONKS_LOCAL_AUTH_TOKEN": LOCAL_TOKEN,
        "STONKS_LOCAL_AUTH_SUBJECT": "local-researcher",
        "STONKS_LOCAL_AUTH_ROLES": "researcher",
        "STONKS_LOCAL_AUTH_TARGETS": "snapshot:snapshot-one,market:US",
    }

    with _client() as client:
        authenticator = create_authenticator(
            Settings(environment="development"),
            environment=variables,
            http_client=client,
        )
    result = authenticator.authenticate(
        AuthenticationRequest(
            authorization=f"Bearer {LOCAL_TOKEN}",
            client_host="127.0.0.1",
        )
    )

    assert isinstance(authenticator, LocalTokenAuthenticator)
    assert isinstance(result, Success)
    assert result.value.roles == frozenset({Role.RESEARCHER})
    assert result.value.targets == frozenset(
        {
            AccessTarget(kind=ResourceKind.SNAPSHOT, identifier="snapshot-one"),
            AccessTarget(kind=ResourceKind.MARKET, identifier="US"),
        }
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STONKS_AUTH_MODE", "unknown"),
        ("STONKS_OIDC_ALLOWED_ALGORITHMS", "HS256"),
        ("STONKS_OIDC_MAX_TOKEN_LIFETIME_SECONDS", "not-an-integer"),
    ],
)
def test_unknown_or_malformed_auth_configuration_fails_closed(
    name: str,
    value: str,
) -> None:
    variables = _oidc_environment()
    variables[name] = value

    with _client() as client, pytest.raises(AuthenticationCompositionError):
        create_authenticator(
            Settings(environment="production"),
            environment=variables,
            http_client=client,
        )
