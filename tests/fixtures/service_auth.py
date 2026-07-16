"""Attempt-bound service authenticator used by HTTP boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceIdentity,
    ServicePermission,
    ServicePrincipal,
    ServiceReceiver,
    ServiceResourceKind,
    canonical_request_hash,
    service_nonce_hash,
)

AUTHORIZATION = "Bearer test-core-runner-credential-32-bytes"


class ExactServiceAuthenticator:
    def __init__(self, principal: ServicePrincipal) -> None:
        self._principal = principal
        self.calls: list[str | None] = []

    @classmethod
    def for_request(
        cls,
        request: Any,
        *,
        receiver: ServiceReceiver,
        kind: ServiceResourceKind = ServiceResourceKind.JOB,
        target_identifier: object | None = None,
    ) -> ExactServiceAuthenticator:
        payload: dict[str, object] = request.model_dump(mode="json")
        request_hash = canonical_request_hash(payload)
        generation = int(getattr(request, "attempt_generation", 0))
        nonce = str(getattr(request, "attempt_nonce", ""))
        deadline = getattr(request, "deadline", None)
        expires_at = int(
            (
                deadline
                if isinstance(deadline, datetime)
                else datetime.now(UTC) + timedelta(minutes=5)
            ).timestamp()
        )
        identifier = target_identifier
        if identifier is None:
            identifier = getattr(request, "job_id", request.request_id)
        principal = ServicePrincipal(
            subject="service:core-runner",
            identity=ServiceIdentity.CORE_RUNNER,
            receiver=receiver,
            permission=(
                ServicePermission.PREFLIGHT_ASSIGNED_RESEARCH
                if receiver is ServiceReceiver.KRONOS and generation == 0
                else ServicePermission.DISPATCH_ASSIGNED_BACKTEST
                if kind is ServiceResourceKind.BACKTEST_JOB
                else ServicePermission.DISPATCH_ASSIGNED_RESEARCH
            ),
            targets=frozenset(
                {ServiceAccessTarget(kind=kind, identifier=str(identifier))}
            ),
            attempt_generation=generation,
            attempt_nonce_hash=(
                request_hash if generation == 0 else service_nonce_hash(nonce)
            ),
            request_hash=request_hash,
            token_id="test-service-token-id",
            issued_at=max(1, expires_at - 300),
            expires_at=expires_at,
        )
        return cls(principal)

    def authenticate(self, authorization: str | None) -> ServicePrincipal | None:
        self.calls.append(authorization)
        return self._principal if authorization == AUTHORIZATION else None

    def altered(self, **updates: object) -> ExactServiceAuthenticator:
        return ExactServiceAuthenticator(self._principal.model_copy(update=updates))


def job_target(identifier: object) -> ServiceAccessTarget:
    return ServiceAccessTarget(
        kind=ServiceResourceKind.JOB,
        identifier=str(identifier),
    )


def backtest_target(identifier: object) -> ServiceAccessTarget:
    return ServiceAccessTarget(
        kind=ServiceResourceKind.BACKTEST_JOB,
        identifier=str(identifier),
    )


def authorization_headers() -> dict[str, str]:
    return {"authorization": AUTHORIZATION}
