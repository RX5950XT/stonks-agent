#!/usr/bin/env python3
"""Exercise the exact OpenBB loopback adapter and corresponding-source flow."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
import yaml
from pydantic import SecretStr

from stonks_agent.adapters.market_data.openbb_rest import (
    OPENBB_ORIGIN,
    OpenBBRestAdapter,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState
from stonks_agent.domain.errors import Success
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialRequest,
)

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecars" / "openbb"
MANIFEST = SIDECAR / "provider-manifest.yaml"
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_SOURCE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_SOURCE_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_SOURCE_MEMBERS = 64
_SAFE_MEMBER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
SOURCE_MEMBER_PATHS: Final = {
    name: SIDECAR / name
    for name in (
        "Dockerfile",
        "Dockerfile.dockerignore",
        "NOTICE.md",
        "README.md",
        "SOURCE_OFFER.md",
        "app.py",
        "license-policy.yaml",
        "provider-manifest.yaml",
        "pyproject.toml",
        "sbom.cdx.json",
        "surface.py",
        "uv.lock",
    )
}
SOURCE_MEMBER_PATHS.update(
    {
        "packages/service-auth/LICENSE": ROOT / "LICENSE",
        "packages/service-auth/pyproject.toml": (
            ROOT / "packages" / "service-auth" / "pyproject.toml"
        ),
        **{
            f"packages/service-auth/src/stonks_service_auth/{name}": (
                ROOT
                / "packages"
                / "service-auth"
                / "src"
                / "stonks_service_auth"
                / name
            )
            for name in (
                "__init__.py",
                "authorization.py",
                "environment.py",
                "headers.py",
                "oidc.py",
                "py.typed",
                "source_identity.py",
            )
        },
    }
)
REQUIRED_SOURCE_MEMBERS: Final = frozenset({"OPENBB_LICENSE.txt", *SOURCE_MEMBER_PATHS})


class _SmokeCredentialProvider:
    def __init__(self, token: str) -> None:
        self._credential = ServiceBearerCredential(token=SecretStr(token))

    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Success[ServiceBearerCredential]:
        del request
        return Success(self._credential)


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
        return body, {key.lower(): value for key, value in response.headers.items()}


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


def _load_manifest() -> dict[str, Any]:
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("OpenBB package manifest is invalid")
    return raw


def _archive_members(bundle: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    items = bundle.getmembers()
    if len(items) > MAX_SOURCE_MEMBERS:
        raise ValueError("source archive has too many members")
    members: dict[str, tarfile.TarInfo] = {}
    casefolded: set[str] = set()
    ordered_names: list[str] = []
    expanded_size = 0
    for item in items:
        name = _safe_member_name(item.name)
        folded = name.casefold()
        if (
            not item.isfile()
            or name in members
            or folded in casefolded
            or item.size < 0
            or item.size > MAX_SOURCE_MEMBER_BYTES
        ):
            raise ValueError("source archive contains an unsafe member")
        if (
            item.mtime != 0
            or item.uid != 0
            or item.gid != 0
            or item.mode & 0o7777 != 0o644
        ):
            raise ValueError("source archive has nondeterministic metadata")
        expanded_size += item.size
        if expanded_size > MAX_SOURCE_EXPANDED_BYTES:
            raise ValueError("source archive expanded size exceeds limit")
        members[name] = item
        casefolded.add(folded)
        ordered_names.append(name)
    if ordered_names != sorted(ordered_names):
        raise ValueError("source archive member order is nondeterministic")
    return members


def _safe_member_name(raw_name: str) -> str:
    name = raw_name.removeprefix("./")
    path = PurePosixPath(name)
    if (
        not name
        or not name.isascii()
        or _SAFE_MEMBER_PATTERN.fullmatch(name) is None
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError("source archive contains an unsafe member")
    return name


def _validate_gzip_header(archive: bytes) -> None:
    if (
        len(archive) < 10
        or archive[:3] != b"\x1f\x8b\x08"
        or archive[3] != 0
        or archive[4:8] != b"\0\0\0\0"
    ):
        raise ValueError("source archive has nondeterministic gzip header")


def _member_sha256(
    bundle: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> str:
    member = members.get(name)
    if member is None or not member.isfile() or member.size > MAX_SOURCE_MEMBER_BYTES:
        raise ValueError(f"source archive is missing {name}")
    stream = bundle.extractfile(member)
    if stream is None:
        raise ValueError(f"source archive cannot read {name}")
    return hashlib.sha256(stream.read(MAX_SOURCE_MEMBER_BYTES + 1)).hexdigest()


def _package_manifest(raw: dict[str, Any]) -> list[dict[str, str]]:
    packages = raw.get("packages")
    if not isinstance(packages, list) or len(packages) != 4:
        raise ValueError("OpenBB package manifest must contain four packages")
    parsed = [dict(item) for item in packages if isinstance(item, dict)]
    if len(parsed) != 4:
        raise ValueError("OpenBB package manifest is invalid")
    return parsed


def _license_hash(raw: dict[str, Any]) -> str:
    service = raw.get("service")
    expected = (
        service.get("upstream_raw_license_sha256")
        if isinstance(service, dict)
        else None
    )
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("OpenBB license hash is invalid")
    return expected


def _verify_local_sources(
    bundle: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
) -> None:
    for name, path in SOURCE_MEMBER_PATHS.items():
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        if _member_sha256(bundle, members, name) != expected:
            raise ValueError(f"source hash mismatch for {name}")


def verify_source_archive_bytes(archive: bytes) -> dict[str, str]:
    """Verify one bounded deterministic corresponding-source archive."""

    _validate_gzip_header(archive)
    manifest = _load_manifest()
    verified: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = _archive_members(bundle)
        for name in REQUIRED_SOURCE_MEMBERS:
            if name not in members:
                raise ValueError(f"source archive is missing {name}")
        _verify_local_sources(bundle, members)
        for package in _package_manifest(manifest):
            name = package["source_archive_member"]
            actual = _member_sha256(bundle, members, name)
            if actual != package["sdist_sha256"]:
                raise ValueError(f"source hash mismatch for {name}")
            verified[name] = actual
        license_name = "OPENBB_LICENSE.txt"
        actual_license = _member_sha256(bundle, members, license_name)
        if actual_license != _license_hash(manifest):
            raise ValueError("upstream license hash mismatch")
        verified[license_name] = actual_license
    return verified


def _verify_source_archive(timeout: float) -> dict[str, str]:
    archive, headers = _get("/source", timeout=timeout)
    if "/source" not in headers.get("link", ""):
        raise ValueError("source response omitted source link")
    return verify_source_archive_bytes(archive)


def _verify_adapter(timeout: float, token: str) -> int:
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
            credentials=_SmokeCredentialProvider(token),
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
        token = os.environ.get("STONKS_OPENBB_SMOKE_TOKEN", "")
        rows = _verify_adapter(args.request_timeout, token)
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
