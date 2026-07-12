"""Fail-closed loopback bearer authentication for local development."""

from __future__ import annotations

import hashlib
import hmac

from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.authentication import AuthenticationRequest

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class LocalTokenAuthenticator:
    """Authenticate one configured local principal without storing raw tokens."""

    __slots__ = ("_allowed_hosts", "_principal", "_token_digest")

    def __init__(
        self,
        *,
        token: str,
        subject: str,
        roles: frozenset[Role],
        allowed_hosts: frozenset[str] = _LOOPBACK_HOSTS,
    ) -> None:
        _validate_token(token)
        if not allowed_hosts or any(
            not host or len(host) > 255 for host in allowed_hosts
        ):
            raise ValueError("allowed_hosts must contain bounded host names")
        self._token_digest = _digest(token)
        self._principal = LocalPrincipal(subject=subject, roles=roles)
        self._allowed_hosts = allowed_hosts

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(subject={self._principal.subject!r}, "
            f"allowed_hosts={sorted(self._allowed_hosts)!r})"
        )

    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Result[LocalPrincipal]:
        if request.client_host not in self._allowed_hosts:
            return _unauthorized()
        credential = _bearer_credential(request.authorization)
        if credential is None:
            return _unauthorized()
        if not hmac.compare_digest(_digest(credential), self._token_digest):
            return _unauthorized()
        return Success(self._principal)


class DenyAllAuthenticator:
    """Safe default when no authentication adapter was configured."""

    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Result[LocalPrincipal]:
        del request
        return _unauthorized()


def _validate_token(token: str) -> None:
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 4096
        or token.strip() != token
        or any(character.isspace() for character in token)
    ):
        raise ValueError("local token must be 32-4096 non-whitespace characters")


def _bearer_credential(value: str | None) -> str | None:
    if value is None or not value.startswith("Bearer "):
        return None
    credential = value.removeprefix("Bearer ")
    if not credential or any(character.isspace() for character in credential):
        return None
    return credential


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _unauthorized() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.UNAUTHORIZED,
            message="Authentication failed",
        )
    )
