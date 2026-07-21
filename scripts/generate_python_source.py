#!/usr/bin/env python3
"""Download uv-locked sdists and emit deterministic corresponding source."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.python_source_contract import (
    EXPECTED_HOSTS,
    LockedSource,
    PythonSourceError,
    SourceArchiveSummary,
    SourcePlan,
    build_archive_bytes,
    load_source_plan,
    validate_download_url,
    verify_source_archive,
)

Fetcher = Callable[[LockedSource], bytes]
HARD_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


class PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Revalidate every redirect before urllib follows it."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_download_url(newurl, self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_source_opener(allowed_hosts: frozenset[str]) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        PolicyRedirectHandler(allowed_hosts),
    )


def download_locked_source(
    source: LockedSource,
    *,
    opener: urllib.request.OpenerDirector | Any,
) -> bytes:
    validate_download_url(source.url, EXPECTED_HOSTS)
    if not 0 < source.size <= HARD_MAX_DOWNLOAD_BYTES:
        raise PythonSourceError("Python source download size exceeds hard limit")
    request = urllib.request.Request(
        source.url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "stonks-agent-python-source/1",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=60) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != source.size:
                raise PythonSourceError(
                    "Python source declared size differs from uv.lock"
                )
            final_url = getattr(response, "geturl", lambda: source.url)()
            validate_download_url(final_url, EXPECTED_HOSTS)
            payload = bytes(response.read(source.size + 1))
    except PythonSourceError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise PythonSourceError("Python source download failed") from error
    if len(payload) != source.size:
        raise PythonSourceError("Python source download has the wrong exact size")
    if hashlib.sha256(payload).hexdigest() != source.sha256:
        raise PythonSourceError("Python source download SHA-256 differs from uv.lock")
    return payload


def generate_python_source(
    *,
    plan: SourcePlan,
    output: Path,
    fetcher: Fetcher,
) -> SourceArchiveSummary:
    payloads: dict[str, bytes] = {}
    total = 0
    for source in plan.sources:
        payload = fetcher(source)
        total += len(payload)
        if total > plan.max_total_source_bytes:
            raise PythonSourceError("downloaded Python source total exceeds policy")
        payloads[source.filename] = payload
    archive = build_archive_bytes(plan, payloads)
    summary = verify_source_archive(archive, plan)
    _atomic_write(output, archive)
    if output.read_bytes() != archive:
        raise PythonSourceError("written Python source archive changed")
    return summary


def generate_from_files(
    *,
    policy_path: Path,
    lock_path: Path,
    output: Path,
) -> SourceArchiveSummary:
    plan = load_source_plan(policy_path, lock_path)
    opener = build_source_opener(plan.allowed_hosts)
    return generate_python_source(
        plan=plan,
        output=output,
        fetcher=lambda source: download_locked_source(source, opener=opener),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.parent.is_symlink():
        raise PythonSourceError("Python source output directory must be regular")
    if path.exists():
        result = path.lstat()
        if not stat.S_ISREG(result.st_mode) or path.is_symlink():
            raise PythonSourceError("Python source output must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise PythonSourceError("Python source archive cannot be written") from error
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--uv-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = generate_from_files(
            policy_path=args.policy,
            lock_path=args.uv_lock,
            output=args.output,
        )
        result: dict[str, object] = {
            "success": True,
            "status": "passed",
            "data": asdict(summary),
            "error": None,
        }
    except PythonSourceError:
        result = {
            "success": False,
            "status": "failed",
            "data": None,
            "error": {"code": "PYTHON_SOURCE_GENERATION_FAILED"},
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["success"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
