"""Stable identity for the exact shared authentication source in a runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path


def service_auth_source_hash() -> str:
    """Hash every shipped source file without trusting bytecode or metadata."""

    root = Path(__file__).resolve().parent
    paths = sorted(
        (*root.glob("*.py"), root / "py.typed"),
        key=lambda path: path.name,
    )
    digest = hashlib.sha256()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("service auth source identity is unavailable")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
