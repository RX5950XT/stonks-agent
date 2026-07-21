#!/usr/bin/env python3
"""Generate a canonical CycloneDX SBOM for one exact container image digest."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

IMAGE_PATTERN = re.compile(
    r"^[a-z0-9.-]+(?::[0-9]+)?/"
    r"[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
SYFT_PATTERN = re.compile(
    r"^[a-z0-9.-]+(?:/[a-z0-9._/-]+)+:[A-Za-z0-9._-]+"
    r"@sha256:[0-9a-f]{64}$"
)
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 600


class SbomError(ValueError):
    """Raised when scanner input or output cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SbomSummary:
    component_count: int
    package_count: int
    components_sha256: str
    sbom_sha256: str
    inventory_sha256: str


def validate_image_reference(value: str) -> str:
    if not IMAGE_PATTERN.fullmatch(value):
        raise SbomError("image must be an exact registry image digest")
    return value


def validate_syft_image(value: str) -> str:
    if not SYFT_PATTERN.fullmatch(value):
        raise SbomError("Syft image must include a version and exact OCI digest")
    return value


def build_syft_command(
    *,
    image_reference: str,
    output_directory: Path,
    output_name: str,
    syft_image: str,
) -> tuple[str, ...]:
    validate_image_reference(image_reference)
    validate_syft_image(syft_image)
    if not SAFE_OUTPUT_NAME.fullmatch(output_name):
        raise SbomError("SBOM output name is unsafe")
    resolved = output_directory.resolve(strict=True)
    if not resolved.is_dir() or output_directory.is_symlink():
        raise SbomError("SBOM output directory must be a regular directory")
    mount = f"type=bind,source={resolved},target=/out"
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock:ro",
        "--mount",
        mount,
        syft_image,
        f"docker:{image_reference}",
        "-o",
        f"cyclonedx-json=/out/{output_name}",
    )


def normalize_sbom(
    payload: Mapping[str, Any],
    *,
    image_reference: str,
    license_overrides: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_image_reference(image_reference)
    normalized = _mapping_copy(payload, "SBOM")
    if normalized.get("bomFormat") != "CycloneDX":
        raise SbomError("SBOM must use CycloneDX")
    if normalized.get("specVersion") != "1.6":
        raise SbomError("SBOM must use CycloneDX 1.6")
    normalized.pop("serialNumber", None)
    metadata = _mapping_copy(normalized.get("metadata"), "SBOM metadata")
    metadata.pop("timestamp", None)
    normalized["metadata"] = metadata
    components = normalized.get("components")
    if not isinstance(components, list) or not components:
        raise SbomError("SBOM components must be a non-empty list")
    if len(components) > 100_000:
        raise SbomError("SBOM component count exceeds policy")

    normalized_components = [
        _normalize_component(component, index)
        for index, component in enumerate(components)
    ]
    normalized_components.sort(key=_component_key)
    normalized["components"] = normalized_components
    dependencies = normalized.get("dependencies")
    if dependencies is not None:
        if not isinstance(dependencies, list):
            raise SbomError("SBOM dependencies must be a list")
        normalized["dependencies"] = sorted(
            (_normalize_dependency(item) for item in dependencies),
            key=lambda item: str(item.get("ref", "")),
        )

    inventory_components: list[dict[str, Any]] = []
    seen_purls: set[str] = set()
    for component in normalized_components:
        purl = component.get("purl")
        if not isinstance(purl, str):
            continue
        if purl in seen_purls:
            raise SbomError(f"duplicate package purl: {purl}")
        seen_purls.add(purl)
        licenses = _component_licenses(component)
        source = "sbom"
        if not licenses:
            reviewed = license_overrides.get(purl)
            if not isinstance(reviewed, str) or not reviewed.strip():
                raise SbomError(f"missing reviewed license for package: {purl}")
            licenses = (reviewed.strip(),)
            source = "reviewed-override"
        inventory_components.append(
            {
                "type": component.get("type"),
                "name": component.get("name"),
                "version": component.get("version"),
                "purl": purl,
                "licenses": list(licenses),
                "license_source": source,
            }
        )
    if not inventory_components:
        raise SbomError("SBOM contains no package inventory")
    components_sha256 = hashlib.sha256(_json_bytes(inventory_components)).hexdigest()
    inventory = {
        "schema_version": "stonks-agent/sbom-inventory/v1",
        "image_reference": image_reference,
        "component_count": len(inventory_components),
        "components_sha256": components_sha256,
        "components": inventory_components,
    }
    return _canonical(normalized), _canonical(inventory)


def write_normalized_sbom(
    raw_path: Path,
    output_path: Path,
    inventory_path: Path,
    *,
    image_reference: str,
    license_overrides: Mapping[str, str],
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> SbomSummary:
    payload = _load_json(raw_path, max_bytes=max_input_bytes)
    normalized, inventory = normalize_sbom(
        payload,
        image_reference=image_reference,
        license_overrides=license_overrides,
    )
    sbom_bytes = _json_bytes(normalized)
    inventory_bytes = _json_bytes(inventory)
    _atomic_write(output_path, sbom_bytes)
    _atomic_write(inventory_path, inventory_bytes)
    return SbomSummary(
        component_count=len(normalized["components"]),
        package_count=inventory["component_count"],
        components_sha256=inventory["components_sha256"],
        sbom_sha256=hashlib.sha256(sbom_bytes).hexdigest(),
        inventory_sha256=hashlib.sha256(inventory_bytes).hexdigest(),
    )


def generate(
    *,
    image_reference: str,
    output_path: Path,
    inventory_path: Path,
    syft_image: str,
    license_overrides: Mapping[str, str],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> SbomSummary:
    validate_image_reference(image_reference)
    if timeout_seconds < 1 or timeout_seconds > 3_600:
        raise SbomError("scanner timeout is outside policy")
    output_parent = output_path.parent.resolve(strict=True)
    if output_parent != inventory_path.parent.resolve(strict=True):
        raise SbomError("SBOM and inventory must share one output directory")
    with tempfile.TemporaryDirectory(
        prefix=".sbom-",
        dir=output_parent,
    ) as temporary:
        temporary_path = Path(temporary)
        raw_name = "raw.cdx.json"
        command = build_syft_command(
            image_reference=image_reference,
            output_directory=temporary_path,
            output_name=raw_name,
            syft_image=syft_image,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            raise SbomError("Syft failed to generate an SBOM")
        return write_normalized_sbom(
            temporary_path / raw_name,
            output_path,
            inventory_path,
            image_reference=image_reference,
            license_overrides=license_overrides,
        )


def _normalize_component(raw: object, index: int) -> dict[str, Any]:
    component = _mapping_copy(raw, f"SBOM component {index}")
    for key in ("type", "name"):
        value = component.get(key)
        if not isinstance(value, str) or not value:
            raise SbomError(f"SBOM component {index} has invalid {key}")
    for key in ("licenses", "hashes", "properties", "externalReferences"):
        value = component.get(key)
        if value is not None:
            if not isinstance(value, list):
                raise SbomError(f"SBOM component {index}.{key} must be a list")
            component[key] = sorted(
                (_canonical(item) for item in value),
                key=_canonical_sort_key,
            )
    return cast(dict[str, Any], _canonical(component))


def _normalize_dependency(raw: object) -> dict[str, Any]:
    dependency = _mapping_copy(raw, "SBOM dependency")
    reference = dependency.get("ref")
    targets = dependency.get("dependsOn", [])
    if not isinstance(reference, str) or not isinstance(targets, list):
        raise SbomError("SBOM dependency is invalid")
    if not all(isinstance(item, str) for item in targets):
        raise SbomError("SBOM dependency targets are invalid")
    dependency["dependsOn"] = sorted(set(targets))
    return cast(dict[str, Any], _canonical(dependency))


def _component_key(component: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(component.get("bom-ref", "")),
        str(component.get("purl", "")),
        str(component.get("name", "")),
        str(component.get("version", "")),
    )


def _component_licenses(component: Mapping[str, Any]) -> tuple[str, ...]:
    raw = component.get("licenses", [])
    if not isinstance(raw, list):
        raise SbomError("component licenses must be a list")
    expressions: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise SbomError("component license entry is invalid")
        expression = item.get("expression")
        if isinstance(expression, str) and expression.strip():
            expressions.add(expression.strip())
            continue
        license_data = item.get("license")
        if not isinstance(license_data, Mapping):
            continue
        value = license_data.get("id") or license_data.get("name")
        if isinstance(value, str) and value.strip():
            expressions.add(value.strip())
    return tuple(sorted(expressions))


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise SbomError("SBOM contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SbomError("SBOM contains an unsupported JSON value")


def _mapping_copy(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SbomError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise SbomError(f"{label} contains a non-string key")
    return dict(value)


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _load_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        status_result = path.lstat()
        if not stat.S_ISREG(status_result.st_mode) or path.is_symlink():
            raise SbomError("SBOM input must be a regular file")
        if status_result.st_size < 2 or status_result.st_size > max_bytes:
            raise SbomError("SBOM input size is outside policy")
        raw = path.read_bytes()
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except SbomError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SbomError("cannot read valid SBOM JSON") from error
    if not isinstance(payload, dict):
        raise SbomError("SBOM root must be an object")
    return payload


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SbomError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SbomError(f"SBOM contains a non-finite number: {value}")


def _atomic_write(path: Path, content: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.parent.is_symlink():
        raise SbomError("output parent must be a regular directory")
    if path.exists() or path.is_symlink():
        status_result = path.lstat()
        if not stat.S_ISREG(status_result.st_mode) or path.is_symlink():
            raise SbomError("output must be a regular file")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise SbomError("cannot atomically write SBOM output") from error
    finally:
        with contextlib.suppress(OSError):
            Path(temporary).unlink(missing_ok=True)


def _load_policy(path: Path) -> tuple[str, dict[str, str]]:
    policy = _load_json(path, max_bytes=1024 * 1024)
    try:
        tools = policy["tools"]
        sbom = policy["sbom"]
        if not isinstance(tools, Mapping) or not isinstance(sbom, Mapping):
            raise KeyError
        syft_image = tools["syft_image"]
        overrides = sbom["license_overrides"]
        if not isinstance(syft_image, str) or not isinstance(overrides, Mapping):
            raise KeyError
        normalized = {
            str(key): str(value)
            for key, value in overrides.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    except KeyError as error:
        raise SbomError("release policy lacks SBOM settings") from error
    validate_syft_image(syft_image)
    return syft_image, normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "release-policy.json",
    )
    parser.add_argument("--raw-sbom", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        syft_image, overrides = _load_policy(args.policy)
        if args.raw_sbom is None:
            summary = generate(
                image_reference=args.image,
                output_path=args.output,
                inventory_path=args.inventory,
                syft_image=syft_image,
                license_overrides=overrides,
            )
        else:
            summary = write_normalized_sbom(
                args.raw_sbom,
                args.output,
                args.inventory,
                image_reference=args.image,
                license_overrides=overrides,
            )
        print(
            json.dumps(
                {
                    "success": True,
                    "status": "passed",
                    "data": {
                        "component_count": summary.component_count,
                        "package_count": summary.package_count,
                        "components_sha256": summary.components_sha256,
                        "sbom_sha256": summary.sbom_sha256,
                        "inventory_sha256": summary.inventory_sha256,
                    },
                    "error": None,
                },
                sort_keys=True,
            )
        )
        return 0
    except (SbomError, OSError, subprocess.SubprocessError):
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "failed",
                    "data": None,
                    "error": {
                        "code": "SBOM_GENERATION_FAILED",
                        "message": "SBOM generation failed closed",
                    },
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
