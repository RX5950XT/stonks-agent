"""Central authentication and coarse permission dependency for FastAPI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from stonks_agent.domain.auth import LocalPrincipal, Permission, authorize
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError
from stonks_agent.entrypoints.api.envelope import error_envelope
from stonks_agent.ports.authentication import AuthenticationRequest, Authenticator

type PrincipalDependency = Callable[..., LocalPrincipal]


class AuthenticationFailure(Exception):
    """Safe exception carrying only an already-structured auth failure."""

    __slots__ = ("error",)

    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__(error.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.error.code.value!r})"


@dataclass(frozen=True, slots=True)
class AuthenticationDependencies:
    """Create exact route permission dependencies backed by app state."""

    def require(self, permission: Permission) -> PrincipalDependency:
        return _permission_dependency(permission)


def install_authentication(
    app: FastAPI,
    authenticator: Authenticator,
) -> AuthenticationDependencies:
    """Install the structured auth handler and return dependency factories."""

    app.state.stonks_authenticator = authenticator
    app.add_exception_handler(AuthenticationFailure, _authentication_failure)
    return AuthenticationDependencies()


def _permission_dependency(permission: Permission) -> PrincipalDependency:
    def dependency(
        request: Request,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization"),
        ] = None,
    ) -> LocalPrincipal:
        principal = _authenticate(request, authorization)
        granted = authorize(principal, permission)
        if isinstance(granted, Failure):
            raise AuthenticationFailure(granted.error)
        return principal

    return dependency


def _authenticate(request: Request, authorization: str | None) -> LocalPrincipal:
    try:
        incoming = AuthenticationRequest(
            authorization=authorization,
            client_host=request.client.host if request.client else None,
        )
    except ValidationError:
        raise AuthenticationFailure(_unauthorized()) from None
    authenticator = getattr(request.app.state, "stonks_authenticator", None)
    if not isinstance(authenticator, Authenticator):
        raise AuthenticationFailure(_unauthorized())
    result = authenticator.authenticate(incoming)
    if isinstance(result, Failure):
        raise AuthenticationFailure(_unauthorized())
    return result.value


ReadPrincipal = Annotated[
    LocalPrincipal,
    Depends(_permission_dependency(Permission.READ)),
]
ResearchPrincipal = Annotated[
    LocalPrincipal,
    Depends(_permission_dependency(Permission.RUN_RESEARCH)),
]
StrategyReviewerPrincipal = Annotated[
    LocalPrincipal,
    Depends(_permission_dependency(Permission.REVIEW_STRATEGY)),
]
PaperOperatorPrincipal = Annotated[
    LocalPrincipal,
    Depends(_permission_dependency(Permission.OPERATE_PAPER)),
]


async def _authentication_failure(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    del request
    if not isinstance(exception, AuthenticationFailure):
        exception = AuthenticationFailure(_unauthorized())
    envelope = error_envelope(exception.error)
    headers = {"WWW-Authenticate": "Bearer"} if envelope.status == 401 else None
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _unauthorized() -> StructuredError:
    return StructuredError(
        code=ErrorCode.UNAUTHORIZED,
        message="Authentication failed",
    )
