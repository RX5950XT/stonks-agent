#!/usr/bin/env python3
"""Create exact ephemeral OIDC material for isolated backtest smoke requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from stonks_contracts.backtest import BacktestEngineKind, BacktestJob
from stonks_service_auth import canonical_request_hash

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.smoke_engine_parity import _cases, _endpoint, _engine_job  # noqa: E402
from scripts.smoke_lean import _job as lean_job  # noqa: E402
from scripts.smoke_nautilus import _job as nautilus_job  # noqa: E402

ISSUER = "https://identity.backtest-smoke.invalid"
SUBJECT = "service:core-runner"
CLIENT_ID = "stonks-core-runner"
KID = "backtest-smoke-ephemeral"
AUDIENCES = {
    "nautilus": "stonks-nautilus-smoke-ingress",
    "lean": "stonks-lean-smoke-ingress",
}


def _material() -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk: dict[str, object] = {
        **raw,
        "kid": KID,
        "alg": "RS256",
        "use": "sig",
        "key_ops": ["verify"],
    }
    return key, {"keys": [jwk]}


def _token(
    key: rsa.RSAPrivateKey,
    *,
    receiver: str,
    job: BacktestJob,
    issued_at: datetime,
) -> str:
    now = int(issued_at.timestamp())
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": AUDIENCES[receiver],
            "exp": int(job.deadline.timestamp()),
            "iat": now,
            "nbf": now,
            "jti": f"{receiver}-smoke-{secrets.token_hex(16)}",
            "client_id": CLIENT_ID,
            "azp": CLIENT_ID,
            "stonks_service_identity": "core_runner",
            "stonks_receiver": receiver,
            "stonks_permission": "dispatch_assigned_backtest",
            "stonks_attempt_generation": job.attempt_generation,
            "stonks_attempt_nonce_hash": hashlib.sha256(
                job.attempt_nonce.encode("utf-8")
            ).hexdigest(),
            "stonks_request_hash": canonical_request_hash(job.model_dump(mode="json")),
            "stonks_targets": [f"backtest_job:{job.job_id}"],
        },
        key,
        algorithm="RS256",
        headers={"kid": KID, "typ": "at+jwt"},
    )


def _common_environment(jwks_path: Path) -> dict[str, str]:
    return {
        "STONKS_SERVICE_OIDC_ISSUER": ISSUER,
        "STONKS_SERVICE_OIDC_CORE_SUBJECT": SUBJECT,
        "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": CLIENT_ID,
        "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
        "STONKS_SERVICE_OIDC_JWKS_HOST_FILE": jwks_path.as_posix(),
        "STONKS_NAUTILUS_SERVICE_OIDC_AUDIENCE": AUDIENCES["nautilus"],
        "STONKS_LEAN_SERVICE_OIDC_AUDIENCE": AUDIENCES["lean"],
    }


def _single_environment(
    key: rsa.RSAPrivateKey,
    *,
    receiver: str,
    runtime_hash: str,
    image_digest: str,
    requested_at: datetime,
) -> dict[str, str]:
    factory = nautilus_job if receiver == "nautilus" else lean_job
    job = factory(runtime_hash, image_digest, requested_at)
    prefix = receiver.upper()
    return {
        f"{prefix}_SMOKE_TOKEN": _token(
            key, receiver=receiver, job=job, issued_at=requested_at
        ),
        f"{prefix}_SMOKE_REQUESTED_AT": requested_at.isoformat(),
    }


def _parity_environment(
    key: rsa.RSAPrivateKey,
    *,
    nautilus_runtime_hash: str,
    nautilus_image_digest: str,
    lean_runtime_hash: str,
    lean_image_digest: str,
    requested_at: datetime,
) -> dict[str, str]:
    base = nautilus_job(nautilus_runtime_hash, nautilus_image_digest, requested_at)
    cases = _cases(base)
    endpoints = (
        _endpoint(
            BacktestEngineKind.NAUTILUS,
            "http://nautilus.invalid",
            (),
            nautilus_runtime_hash,
            nautilus_image_digest,
        ),
        _endpoint(
            BacktestEngineKind.LEAN,
            "http://lean.invalid",
            (),
            lean_runtime_hash,
            lean_image_digest,
        ),
    )
    tokens: dict[str, dict[str, str]] = {"nautilus": {}, "lean": {}}
    for case in cases:
        for endpoint in endpoints:
            job = _engine_job(case, endpoint)
            receiver = endpoint.engine.value
            tokens[receiver][case.name] = _token(
                key, receiver=receiver, job=job, issued_at=requested_at
            )
    return {
        "PARITY_SMOKE_REQUESTED_AT": requested_at.isoformat(),
        "PARITY_NAUTILUS_TOKENS_JSON": json.dumps(
            tokens["nautilus"], separators=(",", ":"), sort_keys=True
        ),
        "PARITY_LEAN_TOKENS_JSON": json.dumps(
            tokens["lean"], separators=(",", ":"), sort_keys=True
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jwks-file", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    single = subparsers.add_parser("single")
    single.add_argument("--receiver", choices=("nautilus", "lean"), required=True)
    single.add_argument("--runtime-hash", required=True)
    single.add_argument("--image-digest", required=True)
    parity = subparsers.add_parser("parity")
    parity.add_argument("--nautilus-runtime-hash", required=True)
    parity.add_argument("--nautilus-image-digest", required=True)
    parity.add_argument("--lean-runtime-hash", required=True)
    parity.add_argument("--lean-image-digest", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    requested_at = datetime.now(UTC).replace(microsecond=0)
    key, jwks = _material()
    jwks_path = args.jwks_file.resolve()
    jwks_path.parent.mkdir(parents=True, exist_ok=True)
    jwks_path.write_text(json.dumps(jwks, sort_keys=True), encoding="utf-8")
    values = _common_environment(jwks_path)
    if args.mode == "single":
        values.update(
            _single_environment(
                key,
                receiver=args.receiver,
                runtime_hash=args.runtime_hash,
                image_digest=args.image_digest,
                requested_at=requested_at,
            )
        )
    else:
        values.update(
            _parity_environment(
                key,
                nautilus_runtime_hash=args.nautilus_runtime_hash,
                nautilus_image_digest=args.nautilus_image_digest,
                lean_runtime_hash=args.lean_runtime_hash,
                lean_image_digest=args.lean_image_digest,
                requested_at=requested_at,
            )
        )
    with args.github_env.open("a", encoding="utf-8", newline="\n") as handle:
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
