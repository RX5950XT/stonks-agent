from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from stonks_agent.adapters.auth.oidc import (
    OIDCAuthenticator,
    OIDCSettings,
    StaticJWKSetProvider,
)
from stonks_agent.config.rbac import load_rbac_policy
from stonks_agent.domain.auth import (
    AccessTarget,
    PrincipalKind,
    ResourceKind,
    Role,
    ServiceIdentity,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.ports.authentication import AuthenticationRequest

ROOT = Path(__file__).resolve().parents[2]
ISSUER = "https://identity.example.test"
AUDIENCE = "stonks-core-api"
KID = "key-2026-07"
NOW = datetime.now(UTC).replace(microsecond=0)
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def settings() -> OIDCSettings:
    return OIDCSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        allowed_algorithms=("RS256",),
        allowed_client_ids=(
            "stonks-web",
            "stonks-research-worker",
            "stonks-paper-executor",
        ),
        max_token_lifetime_seconds=900,
        clock_skew_seconds=30,
    )


def public_jwk(*, kid: str = KID) -> dict[str, object]:
    value = json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
    return {**value, "kid": kid, "alg": "RS256", "use": "sig"}


def authenticator() -> OIDCAuthenticator:
    return OIDCAuthenticator(
        settings=settings(),
        policy=load_rbac_policy(ROOT / "config" / "rbac.yaml"),
        keys=StaticJWKSetProvider((public_jwk(),)),
        clock=lambda: NOW,
    )


def claims(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user:viewer-one",
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
        "iat": int(NOW.timestamp()),
        "nbf": int((NOW - timedelta(seconds=1)).timestamp()),
        "jti": "token-00000001",
        "client_id": "stonks-web",
        "azp": "stonks-web",
        "stonks_roles": ["stonks:viewer"],
        "stonks_targets": ["account:paper-main", "strategy:pead"],
    }
    values.update(overrides)
    return values


def token(
    payload: dict[str, object] | None = None,
    *,
    key: object = PRIVATE_KEY,
    headers: dict[str, object] | None = None,
    algorithm: str = "RS256",
) -> str:
    selected_headers = {"kid": KID, "typ": "at+jwt"}
    if headers:
        selected_headers.update(headers)
    return jwt.encode(
        payload or claims(),
        key,
        algorithm=algorithm,
        headers=selected_headers,
    )


def request(value: str) -> AuthenticationRequest:
    return AuthenticationRequest(
        authorization=f"Bearer {value}", client_host="203.0.113.10"
    )


def test_valid_asymmetric_access_token_maps_allowlisted_role_and_targets() -> None:
    result = authenticator().authenticate(request(token()))

    assert isinstance(result, Success)
    assert result.value.principal_kind is PrincipalKind.HUMAN
    assert result.value.roles == frozenset({Role.VIEWER})
    assert result.value.service_identity is None
    assert result.value.targets == frozenset(
        {
            AccessTarget(kind=ResourceKind.ACCOUNT, identifier="paper-main"),
            AccessTarget(kind=ResourceKind.STRATEGY, identifier="pead"),
        }
    )


def test_service_identity_comes_from_exact_subject_and_client_policy() -> None:
    result = authenticator().authenticate(
        request(
            token(
                claims(
                    sub="service:research-worker",
                    client_id="stonks-research-worker",
                    azp="stonks-research-worker",
                    stonks_roles=[],
                    stonks_service_identity="research_worker",
                    stonks_targets=[
                        "research_run:00000000-0000-4000-8000-000000000001"
                    ],
                )
            )
        )
    )

    assert isinstance(result, Success)
    assert result.value.principal_kind is PrincipalKind.SERVICE
    assert result.value.service_identity is ServiceIdentity.RESEARCH_WORKER
    assert result.value.roles == frozenset()


Mutator = Callable[[dict[str, object], dict[str, object]], None]


def _wrong_issuer(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload["iss"] = "https://attacker.example"


def _wrong_audience(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload["aud"] = "other-api"


def _expired(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload["exp"] = int((NOW - timedelta(minutes=1)).timestamp())


def _future_nbf(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload["nbf"] = int((NOW + timedelta(minutes=2)).timestamp())


def _missing_jti(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload.pop("jti")


def _id_token_type(payload: dict[str, object], headers: dict[str, object]) -> None:
    del payload
    headers["typ"] = "JWT"


def _remote_key_header(payload: dict[str, object], headers: dict[str, object]) -> None:
    del payload
    headers["jku"] = "https://attacker.example/jwks"


def _raw_admin_role(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload["stonks_roles"] = ["admin"]


def _wildcard_target(payload: dict[str, object], headers: dict[str, object]) -> None:
    del headers
    payload["stonks_targets"] = ["account:*"]


@pytest.mark.parametrize(
    "mutate",
    [
        _wrong_issuer,
        _wrong_audience,
        _expired,
        _future_nbf,
        _missing_jti,
        _id_token_type,
        _remote_key_header,
        _raw_admin_role,
        _wildcard_target,
    ],
)
def test_invalid_token_or_claim_authority_returns_one_generic_failure(
    mutate: Mutator,
) -> None:
    payload = claims()
    headers: dict[str, object] = {}
    mutate(payload, headers)

    result = authenticator().authenticate(request(token(payload, headers=headers)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.UNAUTHORIZED
    assert result.error.message == "Authentication failed"
    assert result.error.details == {}


def test_wrong_signature_and_unknown_kid_fail_closed() -> None:
    wrong_signature = authenticator().authenticate(
        request(token(key=OTHER_PRIVATE_KEY))
    )
    unknown_key = authenticator().authenticate(
        request(token(headers={"kid": "unknown-key"}))
    )

    assert isinstance(wrong_signature, Failure)
    assert isinstance(unknown_key, Failure)
    assert wrong_signature.error.code is ErrorCode.UNAUTHORIZED
    assert unknown_key.error.code is ErrorCode.UNAUTHORIZED


def test_service_claim_cannot_select_another_configured_identity() -> None:
    result = authenticator().authenticate(
        request(
            token(
                claims(
                    sub="service:research-worker",
                    client_id="stonks-research-worker",
                    azp="stonks-research-worker",
                    stonks_roles=[],
                    stonks_service_identity="paper_executor",
                    stonks_targets=["account:paper-main"],
                )
            )
        )
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.UNAUTHORIZED


def test_authorization_credential_is_excluded_from_repr_and_dump() -> None:
    bearer = token()
    incoming = request(bearer)

    assert bearer not in repr(incoming)
    assert bearer not in str(incoming.model_dump())
    assert incoming.model_dump() == {"client_host": "203.0.113.10"}


def test_jwks_refresh_interval_cannot_exceed_cache_lifetime() -> None:
    payload = settings().model_dump(mode="json")
    payload.update({"jwks_cache_seconds": 30, "jwks_min_refresh_seconds": 31})

    with pytest.raises(ValueError, match="refresh interval"):
        OIDCSettings.model_validate(payload)


def test_static_jwks_rejects_private_and_weak_rsa_material() -> None:
    private = json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY))
    private.update({"kid": KID, "alg": "RS256", "use": "sig"})
    with pytest.raises(ValueError, match="public shape"):
        StaticJWKSetProvider((private,))

    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak = json.loads(RSAAlgorithm.to_jwk(weak_key.public_key()))
    weak.update({"kid": KID, "alg": "RS256", "use": "sig"})
    with pytest.raises(ValueError, match="too weak"):
        StaticJWKSetProvider((weak,))
