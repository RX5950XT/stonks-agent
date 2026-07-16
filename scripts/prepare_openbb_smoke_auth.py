#!/usr/bin/env python3
"""Create ephemeral OpenBB smoke OIDC trust material without persisting a signer."""

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

ISSUER = "https://identity.openbb-smoke.invalid"
AUDIENCE = "stonks-openbb-ingress"
SUBJECT = "service:core-runner"
CLIENT_ID = "stonks-core-runner"
KID = "openbb-smoke-ephemeral"


def _material() -> tuple[dict[str, object], str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw_jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk: dict[str, object] = {
        **raw_jwk,
        "kid": KID,
        "alg": "RS256",
        "use": "sig",
        "key_ops": ["verify"],
    }
    now = int(datetime.now(UTC).timestamp())
    request_payload = {
        "method": "GET",
        "path": "/api/v1/equity/price/historical",
        "query": {
            "end_date": "2024-01-03",
            "provider": "yfinance",
            "start_date": "2024-01-02",
            "symbol": "AAPL",
        },
    }
    request_hash = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": AUDIENCE,
            "exp": now + 300,
            "iat": now,
            "nbf": now,
            "jti": f"openbb-smoke-{secrets.token_hex(16)}",
            "client_id": CLIENT_ID,
            "azp": CLIENT_ID,
            "stonks_service_identity": "core_runner",
            "stonks_receiver": "openbb",
            "stonks_permission": "dispatch_assigned_market_data",
            "stonks_attempt_generation": 0,
            "stonks_attempt_nonce_hash": request_hash,
            "stonks_request_hash": request_hash,
            "stonks_targets": ["market:US/AAPL"],
        },
        key,
        algorithm="RS256",
        headers={"kid": KID, "typ": "at+jwt"},
    )
    return {"keys": [jwk]}, token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jwks-file", required=True, type=Path)
    parser.add_argument("--github-env", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    jwks, token = _material()
    jwks_path = args.jwks_file.resolve()
    jwks_path.parent.mkdir(parents=True, exist_ok=True)
    jwks_path.write_text(json.dumps(jwks, sort_keys=True), encoding="utf-8")
    values = {
        "STONKS_SERVICE_OIDC_ISSUER": ISSUER,
        "STONKS_SERVICE_OIDC_AUDIENCE": AUDIENCE,
        "STONKS_SERVICE_OIDC_CORE_SUBJECT": SUBJECT,
        "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": CLIENT_ID,
        "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
        "STONKS_SERVICE_OIDC_JWKS_HOST_FILE": jwks_path.as_posix(),
        "STONKS_OPENBB_SMOKE_TOKEN": token,
    }
    with args.github_env.open("a", encoding="utf-8", newline="\n") as handle:
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
