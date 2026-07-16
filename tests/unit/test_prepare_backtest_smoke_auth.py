from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest

from scripts.prepare_backtest_smoke_auth import (
    AUDIENCES,
    CLIENT_ID,
    ISSUER,
    SUBJECT,
    _material,
    _parity_environment,
    _single_environment,
)
from scripts.smoke_engine_parity import _cases, _endpoint, _engine_job
from scripts.smoke_lean import _job as lean_job
from scripts.smoke_nautilus import _job as nautilus_job
from stonks_contracts.backtest import BacktestEngineKind
from stonks_service_auth import (
    ServiceAccessTarget,
    ServiceOIDCSettings,
    ServicePermission,
    ServiceReceiver,
    ServiceResourceKind,
    StaticOIDCServiceAuthenticator,
    authorize_service_dispatch,
)

NOW = datetime.now(UTC).replace(microsecond=0)
HASH = "a" * 64
IMAGE = "sha256:" + "b" * 64


def _authenticator(
    jwks: dict[str, object], receiver: str
) -> StaticOIDCServiceAuthenticator:
    keys = cast(list[Mapping[str, object]], jwks["keys"])
    return StaticOIDCServiceAuthenticator(
        settings=ServiceOIDCSettings(
            issuer=ISSUER,
            audience=AUDIENCES[receiver],
            allowed_algorithms=("RS256",),
            core_subject=SUBJECT,
            core_client_id=CLIENT_ID,
            receiver=ServiceReceiver(receiver),
        ),
        keys=keys,
        clock=lambda: NOW,
    )


@pytest.mark.parametrize("receiver", ["nautilus", "lean"])
def test_single_smoke_token_is_exactly_bound(receiver: str) -> None:
    key, jwks = _material()
    values = _single_environment(
        key,
        receiver=receiver,
        runtime_hash=HASH,
        image_digest=IMAGE,
        requested_at=NOW,
    )
    factory = nautilus_job if receiver == "nautilus" else lean_job
    job = factory(HASH, IMAGE, NOW)
    token = values[f"{receiver.upper()}_SMOKE_TOKEN"]
    principal = _authenticator(jwks, receiver).authenticate(f"Bearer {token}")

    assert principal is not None
    assert authorize_service_dispatch(
        principal,
        permission=ServicePermission.DISPATCH_ASSIGNED_BACKTEST,
        target=ServiceAccessTarget(
            kind=ServiceResourceKind.BACKTEST_JOB,
            identifier=str(job.job_id),
        ),
        receiver=ServiceReceiver(receiver),
        attempt_generation=job.attempt_generation,
        attempt_nonce=job.attempt_nonce,
        request_payload=job.model_dump(mode="json"),
        deadline=job.deadline,
    )


def test_parity_smoke_issues_one_exact_token_per_engine_and_case() -> None:
    key, jwks = _material()
    values = _parity_environment(
        key,
        nautilus_runtime_hash=HASH,
        nautilus_image_digest=IMAGE,
        lean_runtime_hash=HASH,
        lean_image_digest=IMAGE,
        requested_at=NOW,
    )
    base = nautilus_job(HASH, IMAGE, NOW)
    cases = _cases(base)

    for receiver, engine in (
        ("nautilus", BacktestEngineKind.NAUTILUS),
        ("lean", BacktestEngineKind.LEAN),
    ):
        tokens = json.loads(values[f"PARITY_{receiver.upper()}_TOKENS_JSON"])
        endpoint = _endpoint(engine, "http://example.invalid", (), HASH, IMAGE)
        assert set(tokens) == {case.name for case in cases}
        for case in cases:
            job = _engine_job(case, endpoint)
            principal = _authenticator(jwks, receiver).authenticate(
                f"Bearer {tokens[case.name]}"
            )
            assert principal is not None
            assert authorize_service_dispatch(
                principal,
                permission=ServicePermission.DISPATCH_ASSIGNED_BACKTEST,
                target=ServiceAccessTarget(
                    kind=ServiceResourceKind.BACKTEST_JOB,
                    identifier=str(job.job_id),
                ),
                receiver=ServiceReceiver(receiver),
                attempt_generation=job.attempt_generation,
                attempt_nonce=job.attempt_nonce,
                request_payload=job.model_dump(mode="json"),
                deadline=job.deadline,
            )
