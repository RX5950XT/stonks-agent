"""One-shot operator provisioning of the pinned Kronos weights.

This is a provisioning step, not a worker runtime download. Every file is
fetched from its exact pinned Hugging Face revision and accepted only when its
size and SHA-256 match `workers/kronos/model-manifest.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _REPO_ROOT / "workers" / "kronos" / "model-manifest.json"
_DEFAULT_TARGET = _REPO_ROOT / ".data" / "models" / "kronos"
_HF_ORIGIN = "https://huggingface.co"
_TIMEOUT = httpx.Timeout(30.0, read=300.0)
_CHUNK = 1 << 20
_REPOSITORY = re.compile(r"^NeoQuasar/Kronos(?:-Tokenizer)?-[A-Za-z0-9-]+$")
_REVISION = re.compile(r"^[a-f0-9]{40}$")
_DIRECTORY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ProvisionError(RuntimeError):
    """A fail-closed provisioning error."""


def _components(manifest: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for key in ("model", "tokenizer"):
        component = manifest.get(key)
        if not isinstance(component, dict):
            raise ProvisionError(f"manifest component is invalid: {key}")
        if not _REPOSITORY.fullmatch(str(component.get("repository", ""))):
            raise ProvisionError(f"manifest repository is invalid: {key}")
        if not _REVISION.fullmatch(str(component.get("revision", ""))):
            raise ProvisionError(f"manifest revision is invalid: {key}")
        if not _DIRECTORY.fullmatch(str(component.get("directory", ""))):
            raise ProvisionError(f"manifest directory is invalid: {key}")
        yield component


def _files(component: dict[str, Any]) -> Iterator[tuple[str, int, str]]:
    entries = component.get("files")
    if not isinstance(entries, list) or not entries:
        raise ProvisionError("manifest files are invalid")
    for entry in entries:
        name = str(entry.get("path", ""))
        digest = str(entry.get("sha256", ""))
        size = entry.get("size_bytes")
        if not _FILE_NAME.fullmatch(name) or not _SHA256.fullmatch(digest):
            raise ProvisionError("manifest file entry is invalid")
        if not isinstance(size, int) or size <= 0:
            raise ProvisionError("manifest file size is invalid")
        yield name, size, digest


def _digest_of(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _download(client: httpx.Client, url: str, target: Path) -> None:
    staged = target.with_suffix(f"{target.suffix}.partial")
    staged.unlink(missing_ok=True)
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ProvisionError(f"download failed with {response.status_code}")
            with staged.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
        staged.replace(target)
    except httpx.HTTPError as error:
        staged.unlink(missing_ok=True)
        raise ProvisionError("download transport failed") from error
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _provision_file(
    client: httpx.Client,
    *,
    component: dict[str, Any],
    directory: Path,
    name: str,
    size: int,
    digest: str,
) -> str:
    target = directory / name
    if target.is_file() and _digest_of(target) == (size, digest):
        return "verified"
    url = (
        f"{_HF_ORIGIN}/{component['repository']}/resolve/{component['revision']}/{name}"
    )
    _download(client, url, target)
    actual_size, actual_digest = _digest_of(target)
    if (actual_size, actual_digest) != (size, digest):
        target.unlink(missing_ok=True)
        raise ProvisionError(f"{name} does not match the pinned manifest digest")
    return "downloaded"


def provision(target_root: Path) -> int:
    try:
        manifest = json.loads(_MANIFEST.read_bytes())
    except (OSError, ValueError) as error:
        raise ProvisionError("model manifest is unreadable") from error
    if not isinstance(manifest, dict):
        raise ProvisionError("model manifest is invalid")

    downloaded = 0
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        for component in _components(manifest):
            directory = target_root / str(component["directory"])
            directory.mkdir(parents=True, exist_ok=True)
            for name, size, digest in _files(component):
                outcome = _provision_file(
                    client,
                    component=component,
                    directory=directory,
                    name=name,
                    size=size,
                    digest=digest,
                )
                downloaded += outcome == "downloaded"
                print(f"{outcome}: {component['directory']}/{name}")
    return downloaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=_DEFAULT_TARGET)
    arguments = parser.parse_args(argv)
    try:
        provision(arguments.target.resolve())
    except ProvisionError as error:
        print(f"fetch-kronos-model: {error}", file=sys.stderr)
        return 1
    print(f"Kronos weights verified under {arguments.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
