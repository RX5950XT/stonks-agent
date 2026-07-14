#!/usr/bin/env python3
"""Scan source artifacts for high-confidence credential material."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".data",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".research",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
    }
)
SKIP_SUFFIXES = frozenset(
    {
        ".7z",
        ".db",
        ".dll",
        ".exe",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".pdf",
        ".png",
        ".pyc",
        ".safetensors",
        ".sqlite",
        ".zip",
    }
)
ALLOWLIST_MARKER = "pragma: allowlist secret"
MAX_FILE_BYTES = 2 * 1024 * 1024
PATTERNS = (
    (
        "OPENAI_API_KEY",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    ),
    (
        "GITHUB_TOKEN",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ),
    (
        "AWS_ACCESS_KEY",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "PRIVATE_KEY",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    path: str
    line: int


def scan(root: Path) -> tuple[Finding, ...]:
    resolved = root.resolve()
    findings: list[Finding] = []
    for path in _files(resolved):
        findings.extend(_scan_file(resolved, path))
    return tuple(findings)


def _files(root: Path) -> Iterator[Path]:
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            directory for directory in directories if directory not in SKIP_DIRECTORIES
        )
        current_path = Path(current)
        for name in sorted(files):
            path = current_path / name
            if path.suffix.lower() not in SKIP_SUFFIXES and not path.is_symlink():
                yield path


def _scan_file(root: Path, path: Path) -> list[Finding]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        content = path.read_bytes()
    except OSError:
        return [Finding("UNREADABLE_FILE", path.relative_to(root).as_posix(), 0)]
    if b"\x00" in content:
        return []
    text = content.decode("utf-8", errors="replace")
    relative = path.relative_to(root).as_posix()
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if ALLOWLIST_MARKER in line:
            continue
        for code, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(Finding(code, relative, line_number))
    return findings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main() -> int:
    findings = scan(_parser().parse_args().root)
    payload = {
        "success": not findings,
        "status": 200 if not findings else 422,
        "data": {"finding_count": 0} if not findings else None,
        "error": None
        if not findings
        else {
            "code": "SECRET_SCAN_FAILED",
            "message": "high-confidence credential material detected",
            "details": [asdict(finding) for finding in findings],
        },
        "metadata": None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
