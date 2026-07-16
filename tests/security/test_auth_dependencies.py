from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stonks_agent.adapters.auth.local_token import DenyAllAuthenticator
from stonks_agent.domain.auth import LocalPrincipal, Permission, Role
from stonks_agent.domain.errors import Success
from stonks_agent.entrypoints.api.dependencies.auth import (
    ReadPrincipal,
    ResearchPrincipal,
    install_authentication,
)
from stonks_agent.ports.authentication import AuthenticationRequest


class RecordingAuthenticator:
    def __init__(self, principal: LocalPrincipal) -> None:
        self.principal = principal
        self.requests: list[AuthenticationRequest] = []

    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Success[LocalPrincipal]:
        self.requests.append(request)
        return Success(self.principal)


def _app(
    authenticator: RecordingAuthenticator | DenyAllAuthenticator,
    permission: Permission = Permission.READ,
    *,
    called: list[str] | None = None,
) -> FastAPI:
    app = FastAPI()
    install_authentication(app, authenticator)

    def response(principal: LocalPrincipal) -> dict[str, str]:
        if called is not None:
            called.append(principal.subject)
        return {"subject": principal.subject}

    if permission is Permission.READ:

        @app.get("/protected")
        def protected(principal: ReadPrincipal) -> dict[str, str]:
            return response(principal)

    else:

        @app.get("/protected")
        def protected_research(principal: ResearchPrincipal) -> dict[str, str]:
            return response(principal)

    return app


def test_dependency_authenticates_and_passes_server_principal() -> None:
    authenticator = RecordingAuthenticator(
        LocalPrincipal(subject="user:viewer", roles=frozenset({Role.VIEWER}))
    )

    response = TestClient(_app(authenticator)).get(
        "/protected",
        headers={"Authorization": "Bearer opaque-credential"},
    )

    assert response.status_code == 200
    assert response.json() == {"subject": "user:viewer"}
    assert len(authenticator.requests) == 1
    assert authenticator.requests[0].authorization == "Bearer opaque-credential"
    assert authenticator.requests[0].client_host == "testclient"


def test_missing_authentication_uses_structured_401_and_bearer_challenge() -> None:
    called: list[str] = []

    response = TestClient(_app(DenyAllAuthenticator(), called=called)).get("/protected")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "success": False,
        "status": 401,
        "data": None,
        "error": {
            "code": "unauthorized",
            "message": "Authentication failed",
            "details": {},
        },
        "metadata": {
            "pagination": None,
            "request_id": None,
            "trace_id": None,
        },
    }
    assert called == []


def test_oversized_authorization_is_generic_401_without_credential_echo() -> None:
    credential = "sensitive-" + "x" * 4096

    response = TestClient(_app(DenyAllAuthenticator())).get(
        "/protected",
        headers={"Authorization": credential},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert credential not in response.text


def test_permission_denial_is_structured_403_without_bearer_challenge() -> None:
    authenticator = RecordingAuthenticator(
        LocalPrincipal(subject="user:viewer", roles=frozenset({Role.VIEWER}))
    )
    called: list[str] = []

    response = TestClient(
        _app(authenticator, Permission.RUN_RESEARCH, called=called)
    ).get("/protected", headers={"Authorization": "Bearer valid"})

    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert response.json()["error"] == {
        "code": "forbidden",
        "message": "Permission denied",
        "details": {"permission": "run_research"},
    }
    assert called == []


def test_dependency_factory_has_no_credential_in_repr() -> None:
    authenticator = RecordingAuthenticator(
        LocalPrincipal(subject="user:viewer", roles=frozenset({Role.VIEWER}))
    )
    dependency: Callable[..., LocalPrincipal] = install_authentication(
        FastAPI(), authenticator
    ).require(Permission.READ)

    assert "credential" not in repr(dependency).lower()
