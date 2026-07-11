from __future__ import annotations

import pytest

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.ports.authentication import AuthenticationRequest

TOKEN = "test-local-token-that-is-at-least-32-chars"


def authenticator() -> LocalTokenAuthenticator:
    return LocalTokenAuthenticator(
        token=TOKEN,
        subject="local-researcher",
        roles=frozenset({Role.RESEARCHER}),
    )


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


@pytest.mark.parametrize("token", ["", "short", "x" * 4097])
def test_local_auth_rejects_unsafe_configuration(token: str) -> None:
    with pytest.raises(ValueError):
        LocalTokenAuthenticator(
            token=token,
            subject="local-researcher",
            roles=frozenset({Role.RESEARCHER}),
        )
