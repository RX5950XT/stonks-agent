from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm
from pydantic import ValidationError

from stonks_agent.domain.auth import Permission, ResourceKind
from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceIdentity,
    ServiceOIDCSettings,
    ServicePermission,
    ServicePrincipal,
    ServiceReceiver,
    ServiceResourceKind,
    StaticOIDCServiceAuthenticator,
    authorize_service_dispatch,
    authorize_service_target,
    canonical_request_hash,
    exactly_one_authorization_header,
    load_static_oidc_service_authenticator,
    service_auth_source_hash,
    service_nonce_hash,
    validate_isolated_runtime_environment,
)

ISSUER = "https://identity.example.test"
AUDIENCE = "stonks-worker-ingress"
KID = "service-key-2026-07"
NOW = datetime.now(UTC).replace(microsecond=0)
JOB_ID = "00000000-0000-4000-8000-000000000991"
ATTEMPT_NONCE = "attempt-nonce-1"
REQUEST_HASH = canonical_request_hash({"job_id": JOB_ID})
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROOT = Path(__file__).resolve().parents[2]


def settings() -> ServiceOIDCSettings:
    return ServiceOIDCSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        allowed_algorithms=("RS256",),
        core_subject="service:core-runner",
        core_client_id="stonks-core-runner",
        receiver=ServiceReceiver.KRONOS,
        max_token_lifetime_seconds=300,
        clock_skew_seconds=0,
    )


def public_jwk(
    key: rsa.RSAPrivateKey = PRIVATE_KEY,
    *,
    kid: str = KID,
) -> dict[str, object]:
    value = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**value, "kid": kid, "alg": "RS256", "use": "sig"}


def claims(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "service:core-runner",
        "exp": int((NOW + timedelta(minutes=4)).timestamp()),
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
        "jti": "service-token-0001",
        "client_id": "stonks-core-runner",
        "azp": "stonks-core-runner",
        "stonks_service_identity": "core_runner",
        "stonks_receiver": "kronos",
        "stonks_permission": "dispatch_assigned_research",
        "stonks_attempt_generation": 1,
        "stonks_attempt_nonce_hash": service_nonce_hash(ATTEMPT_NONCE),
        "stonks_request_hash": REQUEST_HASH,
        "stonks_targets": [f"job:{JOB_ID}"],
    }
    payload.update(overrides)
    return payload


def token(
    payload: dict[str, object] | None = None,
    *,
    key: object = PRIVATE_KEY,
    kid: str = KID,
    algorithm: str = "RS256",
) -> str:
    return jwt.encode(
        payload or claims(),
        key,
        algorithm=algorithm,
        headers={"kid": kid, "typ": "at+jwt"},
    )


def authenticator() -> StaticOIDCServiceAuthenticator:
    return StaticOIDCServiceAuthenticator(
        settings=settings(),
        keys=(public_jwk(),),
        clock=lambda: NOW,
    )


def test_short_lived_core_runner_token_maps_only_exact_targets() -> None:
    principal = authenticator().authenticate(f"Bearer {token()}")

    assert principal == ServicePrincipal(
        subject="service:core-runner",
        identity=ServiceIdentity.CORE_RUNNER,
        receiver=ServiceReceiver.KRONOS,
        permission=ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
        targets=frozenset(
            {
                ServiceAccessTarget(
                    kind=ServiceResourceKind.JOB,
                    identifier=JOB_ID,
                )
            }
        ),
        attempt_generation=1,
        attempt_nonce_hash=service_nonce_hash(ATTEMPT_NONCE),
        request_hash=REQUEST_HASH,
        token_id="service-token-0001",
        issued_at=int(NOW.timestamp()),
        expires_at=int((NOW + timedelta(minutes=4)).timestamp()),
    )
    assert authorize_service_target(
        principal,
        ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
        ServiceAccessTarget(kind=ServiceResourceKind.JOB, identifier=JOB_ID),
    )
    assert not authorize_service_target(
        principal,
        ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
        ServiceAccessTarget(kind=ServiceResourceKind.JOB, identifier=f"{JOB_ID}-other"),
    )


def test_injected_clock_is_the_only_service_token_time_authority() -> None:
    historical_now = datetime(2020, 1, 1, tzinfo=UTC)
    verifier = StaticOIDCServiceAuthenticator(
        settings=settings(),
        keys=(public_jwk(),),
        clock=lambda: historical_now,
    )
    historical_claims = claims(
        iat=int(historical_now.timestamp()),
        nbf=int(historical_now.timestamp()),
        exp=int((historical_now + timedelta(minutes=4)).timestamp()),
    )

    assert verifier.authenticate(f"Bearer {token(historical_claims)}") is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": "service:research-worker"},
        {"client_id": "stonks-research-worker", "azp": "stonks-research-worker"},
        {"stonks_service_identity": "research_worker"},
        {"stonks_receiver": "quant_lab"},
        {"stonks_targets": []},
        {"stonks_targets": [f"account:{JOB_ID}"]},
        {"iss": "https://attacker.example.test"},
        {"aud": "stonks-other-ingress"},
        {"azp": "stonks-other-client"},
        {"jti": ""},
        {"iat": int((NOW + timedelta(minutes=1)).timestamp())},
        {"nbf": int((NOW + timedelta(minutes=1)).timestamp())},
        {"exp": int(NOW.timestamp())},
        {"exp": int((NOW + timedelta(minutes=6)).timestamp())},
    ],
)
def test_unknown_service_identity_scope_or_lifetime_fails_closed(
    overrides: dict[str, object],
) -> None:
    assert authenticator().authenticate(f"Bearer {token(claims(**overrides))}") is None


def test_unknown_claim_and_human_role_claim_fail_closed() -> None:
    for extra in ({"unexpected": "value"}, {"stonks_roles": ["stonks:admin"]}):
        assert authenticator().authenticate(f"Bearer {token(claims(**extra))}") is None


def test_invalid_or_missing_bearer_is_generic_and_not_retained() -> None:
    value = token()
    verifier = authenticator()

    assert verifier.authenticate(None) is None
    assert verifier.authenticate(value) is None
    assert verifier.authenticate(f"Bearer {value}x") is None
    assert value not in repr(verifier)


def test_wrong_key_algorithm_kid_and_signature_fail_closed() -> None:
    other_rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    verifier = authenticator()

    assert verifier.authenticate(f"Bearer {token(kid='unknown-key')}") is None
    assert verifier.authenticate(f"Bearer {token(key=other_rsa)}") is None
    assert (
        verifier.authenticate(
            f"Bearer {token(key=ec_key, algorithm='ES256', kid='ec-key')}"
        )
        is None
    )


def test_static_jwks_rotation_accepts_overlap_then_requires_restart_to_remove() -> None:
    next_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    next_kid = "service-key-2026-08"
    overlap = StaticOIDCServiceAuthenticator(
        settings=settings(),
        keys=(public_jwk(), public_jwk(next_key, kid=next_kid)),
        clock=lambda: NOW,
    )
    next_only = StaticOIDCServiceAuthenticator(
        settings=settings(),
        keys=(public_jwk(next_key, kid=next_kid),),
        clock=lambda: NOW,
    )
    old_token = token()
    next_token = token(key=next_key, kid=next_kid)

    assert overlap.authenticate(f"Bearer {old_token}") is not None
    assert overlap.authenticate(f"Bearer {next_token}") is not None
    assert next_only.authenticate(f"Bearer {old_token}") is None
    assert next_only.authenticate(f"Bearer {next_token}") is not None


def test_mounted_public_jwks_loader_is_bounded_and_fail_closed(tmp_path: Path) -> None:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [public_jwk()]}), encoding="utf-8")
    environment = {
        "STONKS_SERVICE_OIDC_ISSUER": ISSUER,
        "STONKS_SERVICE_OIDC_AUDIENCE": AUDIENCE,
        "STONKS_SERVICE_OIDC_CORE_SUBJECT": "service:core-runner",
        "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": "stonks-core-runner",
        "STONKS_SERVICE_OIDC_RECEIVER": "kronos",
        "STONKS_SERVICE_OIDC_JWKS_FILE": str(jwks_path),
        "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
    }

    loaded = load_static_oidc_service_authenticator(environment)
    assert "identity.example.test" in repr(loaded)

    jwks_path.write_bytes(b"x" * 65_537)
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_static_oidc_service_authenticator(environment)
    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        load_static_oidc_service_authenticator({})

    jwks_path.write_text('{"keys":[42]}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_static_oidc_service_authenticator(environment)

    directory_environment = {
        **environment,
        "STONKS_SERVICE_OIDC_JWKS_FILE": str(tmp_path),
    }
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_static_oidc_service_authenticator(directory_environment)


def test_service_models_are_frozen_and_reject_empty_assignments() -> None:
    with pytest.raises(ValidationError):
        ServicePrincipal(
            subject="service:core-runner",
            identity=ServiceIdentity.CORE_RUNNER,
            targets=frozenset(),
        )


@pytest.mark.parametrize(
    "private_jwk",
    [
        json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY)),
        json.loads(ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP256R1()))),
        json.loads(OKPAlgorithm.to_jwk(ed25519.Ed25519PrivateKey.generate())),
    ],
)
def test_private_jwk_material_is_rejected(private_jwk: dict[str, object]) -> None:
    key = {
        **private_jwk,
        "kid": KID,
        "alg": "RS256" if private_jwk["kty"] == "RSA" else "ES256",
        "use": "sig",
    }
    if private_jwk["kty"] == "OKP":
        key["alg"] = "EdDSA"

    with pytest.raises(ValueError, match="public shape"):
        StaticOIDCServiceAuthenticator(settings=settings(), keys=(key,))


def test_weak_rsa_public_key_is_rejected() -> None:
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)

    with pytest.raises(ValueError, match="public key"):
        StaticOIDCServiceAuthenticator(
            settings=settings(),
            keys=(public_jwk(weak),),
        )


def test_authorization_header_parser_rejects_ambiguity_and_hostile_values() -> None:
    valid = [(b"authorization", b"Bearer credential")]

    assert exactly_one_authorization_header(valid) == "Bearer credential"
    assert exactly_one_authorization_header([]) is None
    assert exactly_one_authorization_header(valid * 2) is None
    assert exactly_one_authorization_header([(b"authorization", b"x" * 4104)]) is None
    assert (
        exactly_one_authorization_header([(b"authorization", b"Bearer \xff")]) is None
    )


def test_core_and_isolated_dispatch_authority_enums_cannot_drift() -> None:
    core_dispatch = {
        value.value for value in Permission if value.value.startswith("dispatch_")
    } | {Permission.PREFLIGHT_ASSIGNED_RESEARCH.value}
    isolated_dispatch = {value.value for value in ServicePermission}

    assert core_dispatch == isolated_dispatch
    assert {value.value for value in ResourceKind} >= {
        value.value for value in ServiceResourceKind
    }


def test_permission_resource_kind_crossing_is_denied() -> None:
    principal = authenticator().authenticate(f"Bearer {token()}")
    assert principal is not None

    assert not authorize_service_target(
        principal,
        ServicePermission.DISPATCH_ASSIGNED_MARKET_DATA,
        ServiceAccessTarget(kind=ServiceResourceKind.JOB, identifier=JOB_ID),
    )


def test_dispatch_binding_rejects_receiver_fence_payload_and_deadline_drift() -> None:
    principal = authenticator().authenticate(f"Bearer {token()}")
    assert principal is not None
    target = ServiceAccessTarget(kind=ServiceResourceKind.JOB, identifier=JOB_ID)
    deadline = NOW + timedelta(minutes=4)
    valid = {
        "principal": principal,
        "permission": ServicePermission.DISPATCH_ASSIGNED_RESEARCH,
        "target": target,
        "receiver": ServiceReceiver.KRONOS,
        "attempt_generation": 1,
        "attempt_nonce": ATTEMPT_NONCE,
        "request_payload": {"job_id": JOB_ID},
        "deadline": deadline,
    }

    assert authorize_service_dispatch(**valid)
    cases = (
        {"receiver": ServiceReceiver.QUANT_LAB},
        {"attempt_generation": 2},
        {"attempt_nonce": "late-attempt"},
        {"request_payload": {"job_id": f"{JOB_ID}-tampered"}},
        {"deadline": NOW + timedelta(minutes=3)},
    )
    for override in cases:
        assert not authorize_service_dispatch(**(valid | override))


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "PGPASSWORD",
        "REDIS_URL",
        "OPENAI_API_KEY",
        "STONKS_SERVICE_SIGNING_KEY_FILE",
        "STONKS_SERVICE_AUDIENCE_KRONOS",
        "STONKS_EXECUTION_BEARER_TOKEN",
    ],
)
def test_isolated_runtime_rejects_core_or_provider_credentials(name: str) -> None:
    with pytest.raises(RuntimeError, match="forbidden credential"):
        validate_isolated_runtime_environment({name: "secret"})


def test_isolated_runtime_allows_only_public_service_trust_configuration() -> None:
    validate_isolated_runtime_environment(
        {
            "STONKS_SERVICE_OIDC_ISSUER": ISSUER,
            "STONKS_SERVICE_OIDC_AUDIENCE": AUDIENCE,
            "STONKS_SERVICE_OIDC_JWKS_FILE": "/run/secrets/service-jwks.json",
            "STONKS_SERVICE_OIDC_RECEIVER": "kronos",
        }
    )
    value = service_auth_source_hash()
    assert len(value) == 64
    assert value == value.lower()
    int(value, 16)


@pytest.mark.parametrize(
    ("relative_path", "heavy_boundary"),
    [
        ("workers/kronos/runtime_app.py", "_loader.warm()"),
        ("workers/tradingagents/runtime_app.py", "artifact_client = httpx.Client()"),
        ("workers/quant_lab/runtime_app.py", "_settings = load_settings("),
        (
            "sidecars/nautilus/runtime_app.py",
            '"sidecars.nautilus.engine"',
        ),
        (
            "sidecars/lean/runtime_app.py",
            'importlib.import_module("sidecars.lean.engine")',
        ),
        (
            "sidecars/openbb/app.py",
            'importlib.import_module("openbb_core.api.rest_api")',
        ),
    ],
)
def test_runtime_validates_public_trust_before_heavy_initialization(
    relative_path: str,
    heavy_boundary: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert source.index("validate_isolated_runtime_environment(os.environ)") < (
        source.index("load_static_oidc_service_authenticator(os.environ)")
    )
    assert source.index("load_static_oidc_service_authenticator(os.environ)") < (
        source.index(heavy_boundary)
    )
