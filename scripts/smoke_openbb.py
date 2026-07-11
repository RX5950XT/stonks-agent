#!/usr/bin/env python3
"""Exercise the exact OpenBB loopback adapter and corresponding-source flow."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
import yaml

from stonks_agent.adapters.market_data.openbb_rest import (
    OPENBB_ORIGIN,
    OpenBBRestAdapter,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "sidecars" / "openbb" / "provider-manifest.yaml"
MAX_SOURCE_BYTES = 100 * 1024 * 1024


def _get(path: str, *, timeout: float) -> tuple[bytes, dict[str, str]]:
    request = Request(
        f"{OPENBB_ORIGIN}{path}",
        headers={"Accept": "application/json, application/gzip"},
    )
    with urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAX_SOURCE_BYTES:
            raise ValueError("OpenBB response exceeds smoke limit")
        body = response.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValueError("OpenBB response exceeds smoke limit")
        return body, {
            key.lower(): value for key, value in response.headers.items()
        }


def _wait_for_health(timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "unavailable"
    while time.monotonic() < deadline:
        try:
            body, headers = _get("/healthz", timeout=min(5.0, timeout))
            payload: dict[str, Any] = json.loads(body)
            if payload.get("status") != "ok":
                raise ValueError("unexpected health payload")
            if "/source" not in headers.get("link", ""):
                raise ValueError("health response omitted source link")
            return payload
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last_error = type(error).__name__
            time.sleep(1)
    raise TimeoutError(f"OpenBB health did not become ready: {last_error}")


def _load_packages() -> list[dict[str, str]]:
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    packages = raw.get("packages") if isinstance(raw, dict) else None
    if not isinstance(packages, list):
        raise ValueError("OpenBB package manifest is invalid")
    return [dict(item) for item in packages]


def _verify_source_archive(timeout: float) -> dict[str, str]:
    archive, headers = _get("/source", timeout=timeout)
    if "/source" not in headers.get("link", ""):
        raise ValueError("source response omitted source link")
    verified: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = {item.name.lstrip("./"): item for item in bundle.getmembers()}
        for package in _load_packages():
            name = package["source_archive_member"]
            member = members.get(name)
            if member is None or not member.isfile():
                raise ValueError(f"source archive is missing {name}")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError(f"source archive cannot read {name}")
            actual = hashlib.sha256(stream.read()).hexdigest()
            if actual != package["sdist_sha256"]:
                raise ValueError(f"source hash mismatch for {name}")
            verified[name] = actual
        for required in ("Dockerfile", "OPENBB_LICENSE.txt", "uv.lock"):
            if required not in members:
                raise ValueError(f"source archive is missing {required}")
    return verified


def _verify_adapter(timeout: float) -> int:
    observed_at = datetime.now(UTC)
    request = FetchDataRequest(
        market="US",
        capability="prices",
        as_of=observed_at + timedelta(minutes=5),
        query={
            "symbol": "AAPL",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
        },
    )
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        result = OpenBBRestAdapter(
            client=client,
            timeout_seconds=timeout,
            clock=lambda: observed_at,
        ).fetch(request)
    if result.state is not ProviderDataState.AVAILABLE or not result.data:
        reasons = ",".join(result.reasons) or result.state.value
        raise RuntimeError(f"OpenBB adapter smoke failed: {reasons}")
    if result.metadata is None or result.metadata.provider != "yfinance":
        raise RuntimeError("OpenBB adapter omitted provider metadata")
    return len(result.data)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-timeout", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        health = _wait_for_health(args.health_timeout)
        sources = _verify_source_archive(args.request_timeout)
        rows = _verify_adapter(args.request_timeout)
    except Exception as error:  # external boundary; emit no exception details
        payload = {
            "success": False,
            "status": "failed",
            "data": None,
            "error": {
                "code": type(error).__name__,
                "message": "OpenBB sidecar smoke verification failed",
            },
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    payload = {
        "success": True,
        "status": "passed",
        "data": {
            "health": health,
            "source_count": len(sources),
            "provider": "yfinance",
            "row_count": rows,
        },
        "error": None,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
