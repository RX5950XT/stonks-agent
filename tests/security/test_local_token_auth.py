from __future__ import annotations

import pytest

from stonks_agent.adapters.auth.local_token import (
    LocalTokenAuthenticator,
    LocalTokenConfigurationError,
)
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.ports.authentication import AuthenticationRequest

TOKEN = "test-local-token-that-is-at-least-32-chars"


def authenticator() -> LocalTokenAuthenticator:
    return LocalTokenAuthenticator(
        environment="test",
        token=TOKEN,
        subject="local-researcher",
        roles=frozenset({Role.RESEARCHER}),
    )


@pytest.mark.parametrize("environment", ["local", "development", "test"])
def test_local_auth_allows_only_explicit_local_environments(
    environment: str,
) -> None:
    value = LocalTokenAuthenticator(
        environment=environment,
        token=TOKEN,
        subject="local-researcher",
        roles=frozenset({Role.RESEARCHER}),
    )

    assert environment in repr(value)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_local_auth_rejects_deployed_environments_before_peer_checks(
    environment: str,
) -> None:
    with pytest.raises(LocalTokenConfigurationError) as raised:
        LocalTokenAuthenticator(
            environment=environment,
            token=TOKEN,
            subject="local-researcher",
            roles=frozenset({Role.RESEARCHER}),
            allowed_hosts=frozenset({"127.0.0.1"}),
        )

    assert raised.value.error.code is ErrorCode.CONFIGURATION_INVALID
    assert raised.value.error.details == {"authenticator": "local_token"}
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value.error)


def test_local_auth_requires_loopback_and_exact_bearer_token() -> None:
    local = authenticator().authenticate(
        AuthenticationRequest(
            authorization=f"Bearer {TOKEN}",
            client_host="127.0.0.1",
        )
    )
    remote = authenticator().authenticate(
        AuthenticationRequest(
            authorization=f"Bearer {TOKEN}",
            client_host="198.51.100.10",
        )
    )
    wrong = authenticator().authenticate(
        AuthenticationRequest(
            authorization="Bearer incorrect-token-that-is-at-least-32",
            client_host="127.0.0.1",
        )
    )

    assert isinstance(local, Success)
    assert local.value.subject == "local-researcher"
    assert isinstance(remote, Failure)
    assert remote.error.code is ErrorCode.UNAUTHORIZED
    assert isinstance(wrong, Failure)
    assert wrong.error.code is ErrorCode.UNAUTHORIZED


def test_local_auth_never_exposes_the_token() -> None:
    value = authenticator()
    result = value.authenticate(
        AuthenticationRequest(
            authorization=f"Bearer {TOKEN}-wrong",
            client_host="127.0.0.1",
        )
    )

    assert TOKEN not in repr(value)
    assert isinstance(result, Failure)
    assert TOKEN not in result.error.message
    assert TOKEN not in repr(result.error.details)


def test_local_auth_compares_only_fixed_length_token_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[bytes, bytes]] = []

    def compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return True

    monkeypatch.setattr(
        "stonks_agent.adapters.auth.local_token.hmac.compare_digest",
        compare_digest,
    )

    result = authenticator().authenticate(
        AuthenticationRequest(
            authorization="Bearer another-token-that-is-at-least-32-chars",
            client_host="127.0.0.1",
        )
    )

    assert isinstance(result, Success)
    assert len(compared) == 1
    assert all(isinstance(value, bytes) and len(value) == 32 for value in compared[0])
    assert TOKEN.encode() not in compared[0]


@pytest.mark.parametrize("token", ["", "short", "x" * 4097])
def test_local_auth_rejects_unsafe_configuration(token: str) -> None:
    with pytest.raises(ValueError):
        LocalTokenAuthenticator(
            environment="test",
            token=token,
            subject="local-researcher",
            roles=frozenset({Role.RESEARCHER}),
        )
