from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.adapters.auth.local_token import LocalTokenAuthenticator
from stonks_agent.domain.auth import Role
from stonks_agent.entrypoints.api.dependencies.auth import (
    ReadPrincipal,
    install_authentication,
)
from stonks_agent.entrypoints.api.web_protection import (
    AUTH_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    CookieAuthPolicy,
    install_web_protection,
)

ORIGIN = "https://testserver"
AUTH_TOKEN = "opaque-cookie-credential-" + "a" * 32
CSRF_TOKEN = "csrf-proof-" + "b" * 32


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^[A-Z]{1,12}$")


def _app(*, cookie_auth: CookieAuthPolicy | None = None) -> FastAPI:
    app = FastAPI()
    install_authentication(
        app,
        LocalTokenAuthenticator(
            environment="test",
            token=AUTH_TOKEN,
            subject="user:viewer",
            roles=frozenset({Role.VIEWER}),
            allowed_hosts=frozenset({"testclient"}),
        ),
    )
    install_web_protection(app, cookie_auth=cookie_auth)

    @app.api_route("/protected", methods=["GET", "POST"])
    def protected(principal: ReadPrincipal) -> dict[str, str]:
        return {"subject": principal.subject}

    @app.post("/validate")
    def validate(body: RequestBody) -> dict[str, str]:
        return {"symbol": body.symbol}

    @app.get("/request-headers")
    def request_headers(
        request: Request,
        principal: ReadPrincipal,
    ) -> dict[str, str | None]:
        return {
            "subject": principal.subject,
            "cookie": request.headers.get("cookie"),
        }

    @app.get("/http-error/{status_code}")
    def http_error(status_code: int) -> None:
        raise HTTPException(
            status_code=status_code,
            detail=f"Bearer {AUTH_TOKEN}",
            headers={"X-Internal-Authorization": AUTH_TOKEN},
        )

    @app.get("/internal-error")
    def internal_error() -> None:
        raise RuntimeError(f"Bearer {AUTH_TOKEN}\nTraceback: internal/path.py:12")

    @app.get("/html", response_class=HTMLResponse)
    def html() -> HTMLResponse:
        return HTMLResponse("<!doctype html><p>safe</p>")

    return app


def _assert_security_headers(response: object) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    csp = headers["content-security-policy"]
    assert csp == (
        "default-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'; script-src 'none'; "
        "style-src 'self'"
    )
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["permissions-policy"] == "camera=(), geolocation=(), microphone=()"


def test_bearer_only_mode_rejects_ambient_auth_cookie_without_echo() -> None:
    client = TestClient(_app(), base_url=ORIGIN)
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)

    response = client.get("/protected")

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "unauthorized",
        "message": "Authentication failed",
        "details": {},
    }
    assert AUTH_TOKEN not in response.text
    _assert_security_headers(response)


def test_cookie_auth_is_explicit_and_injected_as_bearer_after_same_origin_check() -> (
    None
):
    policy = CookieAuthPolicy(public_origin=ORIGIN)
    client = TestClient(_app(cookie_auth=policy), base_url=ORIGIN)
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)

    response = client.get(
        "/protected",
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 200
    assert response.json() == {"subject": "user:viewer"}
    _assert_security_headers(response)


def test_cookie_auth_safe_get_allows_missing_origin_but_rejects_mismatch() -> None:
    client = TestClient(
        _app(cookie_auth=CookieAuthPolicy(public_origin=ORIGIN)),
        base_url=ORIGIN,
    )
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)

    missing = client.get("/protected")
    mismatched = client.get(
        "/protected",
        headers={"Origin": "https://attacker.invalid"},
    )

    assert missing.status_code == 200
    assert missing.json() == {"subject": "user:viewer"}
    assert mismatched.status_code == 403
    assert mismatched.json()["error"]["code"] == "forbidden"


def test_cookie_auth_safe_head_allows_missing_origin_with_exact_target() -> None:
    app = _app(cookie_auth=CookieAuthPolicy(public_origin=ORIGIN))

    @app.head("/head-protected")
    def head_protected(principal: ReadPrincipal) -> None:
        del principal

    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)

    accepted = client.head("/head-protected")
    wrong_target = TestClient(app, base_url="https://other.test")
    wrong_target.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)
    rejected = wrong_target.head("/head-protected")

    assert accepted.status_code == 200
    assert rejected.status_code == 403


def test_cookie_auth_unsafe_method_requires_same_origin_double_submit_csrf() -> None:
    client = TestClient(
        _app(cookie_auth=CookieAuthPolicy(public_origin=ORIGIN)),
        base_url=ORIGIN,
    )
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    missing = client.post(
        "/protected",
        headers={"Origin": ORIGIN},
    )
    wrong_origin = client.post(
        "/protected",
        headers={"Origin": "https://attacker.invalid", "X-CSRF-Token": CSRF_TOKEN},
    )
    mismatch = client.post(
        "/protected",
        headers={"Origin": ORIGIN, "X-CSRF-Token": f"{CSRF_TOKEN}x"},
    )
    trace_without_csrf = client.request(
        "TRACE",
        "/protected",
        headers={"Origin": ORIGIN},
    )
    accepted = client.post(
        "/protected",
        headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
    )

    rejected = (missing, wrong_origin, mismatch, trace_without_csrf)
    assert [response.status_code for response in rejected] == [403, 403, 403, 403]
    assert all(item.json()["error"]["code"] == "forbidden" for item in rejected)
    assert accepted.status_code == 200
    assert accepted.json() == {"subject": "user:viewer"}


def test_cookie_auth_rejects_ambiguous_bearer_and_cookie_credentials() -> None:
    client = TestClient(
        _app(cookie_auth=CookieAuthPolicy(public_origin=ORIGIN)),
        base_url=ORIGIN,
    )
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}", "Origin": ORIGIN},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Request is invalid"
    assert AUTH_TOKEN not in response.text


def test_cookie_credentials_are_removed_before_application_processing() -> None:
    client = TestClient(
        _app(cookie_auth=CookieAuthPolicy(public_origin=ORIGIN)),
        base_url=ORIGIN,
    )
    client.cookies.set(AUTH_COOKIE_NAME, AUTH_TOKEN)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)
    client.cookies.set("preference", "dark")

    response = client.get("/request-headers", headers={"Origin": ORIGIN})

    assert response.status_code == 200
    assert response.json() == {
        "subject": "user:viewer",
        "cookie": "preference=dark",
    }
    assert AUTH_TOKEN not in response.text
    assert CSRF_TOKEN not in response.text


def test_html_response_gets_a_strict_csp_without_unsafe_script_directives() -> None:
    response = TestClient(_app(), base_url=ORIGIN).get("/html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    _assert_security_headers(response)


def test_validation_error_is_uniform_and_does_not_reflect_body_or_secret() -> None:
    secret_shaped = "sk-" + "proj-" + "s" * 24

    response = TestClient(_app(), base_url=ORIGIN).post(
        "/validate",
        content=f'{{"symbol":"{secret_shaped}","raw_body":"Traceback"}}',
        headers={"Content-Type": "application/json", "X-Secret": AUTH_TOKEN},
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_input",
        "message": "Request is invalid",
        "details": {},
    }
    assert secret_shaped not in response.text
    assert AUTH_TOKEN not in response.text
    assert "Traceback" not in response.text
    _assert_security_headers(response)


def test_http_exception_is_uniform_and_discards_detail_and_sensitive_headers() -> None:
    response = TestClient(_app(), base_url=ORIGIN).get("/http-error/401")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "x-internal-authorization" not in response.headers
    assert response.json()["error"] == {
        "code": "unauthorized",
        "message": "Authentication failed",
        "details": {},
    }
    assert AUTH_TOKEN not in response.text
    _assert_security_headers(response)


def test_unhandled_exception_is_uniform_without_message_stack_or_secret() -> None:
    response = TestClient(
        _app(),
        base_url=ORIGIN,
        raise_server_exceptions=False,
    ).get("/internal-error")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "Internal server error",
        "details": {},
    }
    assert AUTH_TOKEN not in response.text
    assert "Traceback" not in response.text
    _assert_security_headers(response)


def test_installation_is_idempotent_only_for_the_same_policy() -> None:
    app = FastAPI()
    policy = CookieAuthPolicy(public_origin=ORIGIN)

    install_web_protection(app, cookie_auth=policy)
    install_web_protection(app, cookie_auth=policy)

    assert len(app.user_middleware) == 3


def test_cookie_policy_rejects_insecure_or_non_origin_configuration() -> None:
    invalid = (
        "http://example.test",
        "https://example.test/path",
        "https://user@example.test",
        "https://example.test?query=yes",
        "https://例子.test",
        "https://example.test.",
        "https://example.test:0",
        "https://example.test:443",
        "https://[0:0:0:0:0:0:0:1]:8443",
    )

    for origin in invalid:
        try:
            CookieAuthPolicy(public_origin=origin)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid origin: {origin}")


def test_cookie_policy_accepts_canonical_bracketed_ipv6_origin() -> None:
    policy = CookieAuthPolicy(public_origin="https://[::1]:8443")

    assert policy.public_origin == "https://[::1]:8443"
