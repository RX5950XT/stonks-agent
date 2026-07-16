from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretBytes, ValidationError

from stonks_agent.adapters.auth.service_credentials import (
    ReceiverAudience,
    RS256ServiceCredentialProvider,
    ServiceIssuerSettings,
    load_rs256_service_credential_provider,
)
from stonks_agent.domain.auth import AccessTarget, Permission, ResourceKind
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.ports.service_credentials import (
    ServiceCredentialRequest,
    ServiceReceiver,
)
from stonks_service_auth import (
    ServiceOIDCSettings,
    ServicePermission,
    StaticOIDCServiceAuthenticator,
    canonical_request_hash,
    service_nonce_hash,
)
from stonks_service_auth import (
    ServiceReceiver as IngressReceiver,
)

NOW = datetime.now(UTC).replace(microsecond=0)
ISSUER = "https://identity.example.test"
SUBJECT = "service:core-runner"
CLIENT_ID = "stonks-core-runner"
KID = "core-service-key-2026-07"
JOB_ID = UUID("00000000-0000-4000-8000-000000000991")
RUN_ID = UUID("00000000-0000-4000-8000-000000000992")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000993")
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def audience_map() -> dict[ServiceReceiver, str]:
    return {
        receiver: f"stonks-{receiver.value}-ingress" for receiver in ServiceReceiver
    }


def audiences() -> tuple[ReceiverAudience, ...]:
    return tuple(
        ReceiverAudience(receiver=receiver, audience=audience)
        for receiver, audience in audience_map().items()
    )


def settings() -> ServiceIssuerSettings:
    return ServiceIssuerSettings(
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
        key_id=KID,
        audiences=audiences(),
        max_token_lifetime_seconds=120,
    )


def private_pem() -> bytes:
    return PRIVATE_KEY.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_jwk() -> dict[str, object]:
    value = json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
    return {**value, "kid": KID, "alg": "RS256", "use": "sig"}


def credential_request() -> ServiceCredentialRequest:
    payload = {"job_id": str(JOB_ID), "attempt_generation": 3}
    return ServiceCredentialRequest(
        receiver=ServiceReceiver.KRONOS,
        permission=Permission.DISPATCH_ASSIGNED_RESEARCH,
        target=AccessTarget(kind=ResourceKind.JOB, identifier=str(JOB_ID)),
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        attempt_generation=3,
        attempt_nonce_hash=service_nonce_hash("nonce-3"),
        request_hash=canonical_request_hash(payload),
        expires_no_later_than=NOW + timedelta(minutes=1),
    )


def verifier(
    receiver: IngressReceiver = IngressReceiver.KRONOS,
) -> StaticOIDCServiceAuthenticator:
    return StaticOIDCServiceAuthenticator(
        settings=ServiceOIDCSettings(
            issuer=ISSUER,
            audience=audience_map()[ServiceReceiver(receiver.value)],
            allowed_algorithms=("RS256",),
            core_subject=SUBJECT,
            core_client_id=CLIENT_ID,
            receiver=receiver,
            max_token_lifetime_seconds=120,
            clock_skew_seconds=0,
        ),
        keys=(public_jwk(),),
        clock=lambda: NOW,
    )


def test_issuer_and_ingress_verify_one_exact_signed_dispatch_binding() -> None:
    issuer = RS256ServiceCredentialProvider(
        settings=settings(),
        private_key_pem=SecretBytes(private_pem()),
        clock=lambda: NOW,
        token_id=lambda: "service-jti-0001",
    )

    issued = issuer.issue(credential_request())
    assert isinstance(issued, Success)
    principal = verifier().authenticate(issued.value.authorization_header())

    assert principal is not None
    assert principal.receiver is IngressReceiver.KRONOS
    assert principal.permission is ServicePermission.DISPATCH_ASSIGNED_RESEARCH
    assert principal.attempt_generation == 3
    assert principal.token_id == "service-jti-0001"
    assert private_pem().decode("ascii") not in repr(issuer)


def test_receiver_specific_audience_prevents_cross_worker_replay() -> None:
    issuer = RS256ServiceCredentialProvider(
        settings=settings(),
        private_key_pem=SecretBytes(private_pem()),
        clock=lambda: NOW,
    )
    issued = issuer.issue(credential_request())
    assert isinstance(issued, Success)

    assert (
        verifier(IngressReceiver.QUANT_LAB).authenticate(
            issued.value.authorization_header()
        )
        is None
    )


@pytest.mark.parametrize("receiver", tuple(ServiceReceiver))
def test_every_receiver_round_trips_only_its_exact_audience(
    receiver: ServiceReceiver,
) -> None:
    leased = receiver is not ServiceReceiver.OPENBB
    permission = {
        ServiceReceiver.KRONOS: Permission.DISPATCH_ASSIGNED_RESEARCH,
        ServiceReceiver.TRADINGAGENTS: Permission.DISPATCH_ASSIGNED_RESEARCH,
        ServiceReceiver.QUANT_LAB: Permission.DISPATCH_ASSIGNED_RESEARCH,
        ServiceReceiver.NAUTILUS: Permission.DISPATCH_ASSIGNED_BACKTEST,
        ServiceReceiver.LEAN: Permission.DISPATCH_ASSIGNED_BACKTEST,
        ServiceReceiver.OPENBB: Permission.DISPATCH_ASSIGNED_MARKET_DATA,
    }[receiver]
    kind = (
        ResourceKind.MARKET
        if receiver is ServiceReceiver.OPENBB
        else ResourceKind.BACKTEST_JOB
        if receiver in {ServiceReceiver.NAUTILUS, ServiceReceiver.LEAN}
        else ResourceKind.JOB
    )
    identifier = "US/AAPL" if kind is ResourceKind.MARKET else str(JOB_ID)
    request_hash = canonical_request_hash(
        {"receiver": receiver.value, "target": identifier}
    )
    request = ServiceCredentialRequest(
        receiver=receiver,
        permission=permission,
        target=AccessTarget(kind=kind, identifier=identifier),
        request_id=REQUEST_ID if leased else None,
        run_id=RUN_ID if leased else None,
        attempt_generation=3 if leased else 0,
        attempt_nonce_hash=(
            service_nonce_hash("attempt-3") if leased else request_hash
        ),
        request_hash=request_hash,
        expires_no_later_than=NOW + timedelta(minutes=1),
    )
    issuer = RS256ServiceCredentialProvider(
        settings=settings(),
        private_key_pem=SecretBytes(private_pem()),
        clock=lambda: NOW,
    )

    issued = issuer.issue(request)

    assert isinstance(issued, Success)
    principal = verifier(IngressReceiver(receiver.value)).authenticate(
        issued.value.authorization_header()
    )
    assert principal is not None
    assert principal.receiver.value == receiver.value
    assert principal.permission.value == permission.value
    assert {(target.kind.value, target.identifier) for target in principal.targets} == {
        (kind.value, identifier)
    }


def test_expired_deadline_and_receiver_permission_drift_fail_closed() -> None:
    issuer = RS256ServiceCredentialProvider(
        settings=settings(),
        private_key_pem=SecretBytes(private_pem()),
        clock=lambda: NOW,
    )
    expired = credential_request().model_copy(
        update={"expires_no_later_than": NOW - timedelta(seconds=1)}
    )

    assert isinstance(issuer.issue(expired), Failure)
    with pytest.raises(ValidationError, match="receiver authority"):
        ServiceCredentialRequest.model_validate(
            {
                **credential_request().model_dump(mode="json"),
                "receiver": ServiceReceiver.LEAN,
            }
        )


def test_subsecond_deadline_and_invalid_token_id_fail_closed() -> None:
    subsecond = credential_request().model_copy(
        update={"expires_no_later_than": NOW + timedelta(microseconds=500_000)}
    )
    issuer = RS256ServiceCredentialProvider(
        settings=settings(),
        private_key_pem=SecretBytes(private_pem()),
        clock=lambda: NOW,
        token_id=lambda: "bad token id",
    )

    assert isinstance(issuer.issue(subsecond), Failure)
    assert isinstance(issuer.issue(credential_request()), Failure)


def test_mounted_signing_key_loader_is_bounded_and_fail_closed(tmp_path: Path) -> None:
    key_file = tmp_path / "service-key.pem"
    key_file.write_bytes(private_pem())
    key_file.chmod(0o600)
    environment = {
        "STONKS_SERVICE_ISSUER": ISSUER,
        "STONKS_SERVICE_CORE_SUBJECT": SUBJECT,
        "STONKS_SERVICE_CORE_CLIENT_ID": CLIENT_ID,
        "STONKS_SERVICE_SIGNING_KEY_ID": KID,
        "STONKS_SERVICE_SIGNING_KEY_FILE": str(key_file.resolve()),
        **{
            f"STONKS_SERVICE_AUDIENCE_{receiver.value.upper()}": audience
            for receiver, audience in audience_map().items()
        },
    }

    loaded = load_rs256_service_credential_provider(environment)
    assert "identity.example.test" in repr(loaded)

    key_file.write_bytes(b"x" * 16_385)
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_rs256_service_credential_provider(environment)
    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        load_rs256_service_credential_provider({})


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-key mode enforcement")
def test_mounted_signing_key_loader_rejects_group_readable_key(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "service-key.pem"
    key_file.write_bytes(private_pem())
    key_file.chmod(0o644)
    environment = {
        "STONKS_SERVICE_ISSUER": ISSUER,
        "STONKS_SERVICE_CORE_SUBJECT": SUBJECT,
        "STONKS_SERVICE_CORE_CLIENT_ID": CLIENT_ID,
        "STONKS_SERVICE_SIGNING_KEY_ID": KID,
        "STONKS_SERVICE_SIGNING_KEY_FILE": str(key_file.resolve()),
        **{
            f"STONKS_SERVICE_AUDIENCE_{receiver.value.upper()}": audience
            for receiver, audience in audience_map().items()
        },
    }

    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_rs256_service_credential_provider(environment)


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-file enforcement")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_mounted_signing_key_loader_rejects_links_and_special_files(
    tmp_path: Path,
    kind: str,
) -> None:
    target = tmp_path / "target.pem"
    target.write_bytes(private_pem())
    target.chmod(0o600)
    candidate = tmp_path / "candidate.pem"
    if kind == "symlink":
        candidate.symlink_to(target)
    else:
        os.mkfifo(candidate, mode=0o600)
    environment = {
        "STONKS_SERVICE_ISSUER": ISSUER,
        "STONKS_SERVICE_CORE_SUBJECT": SUBJECT,
        "STONKS_SERVICE_CORE_CLIENT_ID": CLIENT_ID,
        "STONKS_SERVICE_SIGNING_KEY_ID": KID,
        "STONKS_SERVICE_SIGNING_KEY_FILE": str(candidate.absolute()),
        **{
            f"STONKS_SERVICE_AUDIENCE_{receiver.value.upper()}": audience
            for receiver, audience in audience_map().items()
        },
    }

    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_rs256_service_credential_provider(environment)


def test_issuer_rejects_weak_key_and_duplicate_audiences() -> None:
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_pem = weak.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="signing key"):
        RS256ServiceCredentialProvider(
            settings=settings(),
            private_key_pem=SecretBytes(weak_pem),
        )

    duplicate = tuple(
        ReceiverAudience(receiver=receiver, audience="shared-audience")
        for receiver in ServiceReceiver
    )
    with pytest.raises(ValidationError, match="audiences"):
        ServiceIssuerSettings.model_validate(
            {**settings().model_dump(mode="json"), "audiences": duplicate}
        )
