#!/usr/bin/env python3
"""Fail-closed license, vendoring and core dependency policy checks."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REQUIRED_GATES = frozenset(
    {
        "NO_VENDOR_DEXTER_CODE",
        "NO_VENDOR_AI_TRADER_CODE",
        "NO_OPENBB_IMPORT_IN_CORE",
    }
)
REQUIRED_UPSTREAMS = frozenset(
    {
        "ai-hedge-fund",
        "ai-trader",
        "daily-stock-analysis",
        "dexter",
        "kronos",
        "nautilus-trader",
        "openbb",
        "qlib",
        "rd-agent",
        "tradingagents",
    }
)
ALLOWED_LICENSE_STATUSES = frozenset({"verified", "incomplete", "conflicting"})
ALLOWED_ADOPTION_MODES = frozenset(
    {
        "clean-room-only",
        "external-adapter-only",
        "isolated-worker",
        "optional-sidecar",
        "research-only",
        "selective-port",
    }
)
FORBIDDEN_CORE_DEPENDENCIES = frozenset(
    {
        "langgraph",
        "lean",
        "nautilus-trader",
        "openbb",
        "openbb-core",
        "openbb-platform",
        "openbb-terminal",
        "pyqlib",
        "pytorch",
        "qlib",
        "rd-agent",
        "rdagent",
        "torch",
    }
)
FORBIDDEN_VENDOR_ROOTS = {
    "NO_VENDOR_DEXTER_CODE": (
        "packages/dexter",
        "src/dexter",
        "third_party/dexter",
        "vendor/dexter",
        "workers/dexter",
    ),
    "NO_VENDOR_AI_TRADER_CODE": (
        "packages/ai-trader",
        "src/ai-trader",
        "third_party/ai-trader",
        "vendor/ai-trader",
        "workers/ai-trader",
    ),
}
CORE_SOURCE_ROOTS = ("src", "packages/contracts/src")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DEPENDENCY_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
NAUTILUS_REPOSITORY = "https://github.com/nautechsystems/nautilus_trader"
NAUTILUS_SNAPSHOT = "8160730c7c550480b0a439fb11086a4c4de15f0b"
NAUTILUS_NOTICE_ID = "NAUTILUS-TRADER-LGPL-3.0-SIDECAR"


@dataclass(frozen=True, slots=True)
class Violation:
    """A stable, machine-readable policy violation."""

    code: str
    message: str
    path: str | None = None


class PolicyInputError(ValueError):
    """Raised when a required policy input cannot be parsed safely."""


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().lower())


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyInputError(f"{label} must be a mapping")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PolicyInputError(f"{label} must be a sequence")
    return value


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
        return _mapping(yaml.safe_load(content), "manifest")
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise PolicyInputError(f"cannot read manifest: {error}") from error


def _load_toml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PolicyInputError(f"cannot read {label}: {error}") from error


def _manifest_entries(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_entries = _sequence(manifest.get("upstreams"), "manifest.upstreams")
    return [
        _mapping(entry, f"manifest.upstreams[{index}]")
        for index, entry in enumerate(raw_entries)
    ]


def _check_required_gates(manifest: Mapping[str, Any]) -> list[Violation]:
    policies = _mapping(manifest.get("policies"), "manifest.policies")
    raw_gates = _sequence(policies.get("required_gates"), "required_gates")
    gates = {str(gate) for gate in raw_gates}
    return [
        Violation("MISSING_CRITICAL_GATE", f"manifest must require {gate}")
        for gate in sorted(REQUIRED_GATES - gates)
    ]


def _check_policy_lists(manifest: Mapping[str, Any]) -> list[Violation]:
    policies = _mapping(manifest.get("policies"), "manifest.policies")
    raw_dependencies = _sequence(
        policies.get("forbidden_core_dependencies"),
        "forbidden_core_dependencies",
    )
    declared_dependencies = {_normalize_name(str(item)) for item in raw_dependencies}
    violations = [
        Violation(
            "MISSING_FORBIDDEN_DEPENDENCY",
            f"manifest must forbid core dependency {name}",
        )
        for name in sorted(FORBIDDEN_CORE_DEPENDENCIES - declared_dependencies)
    ]
    raw_roots = _sequence(
        policies.get("forbidden_vendor_roots"), "forbidden_vendor_roots"
    )
    declared_roots = {_normalize_path(str(item)) for item in raw_roots}
    required_roots = {
        _normalize_path(path)
        for paths in FORBIDDEN_VENDOR_ROOTS.values()
        for path in paths
    }
    violations.extend(
        Violation(
            "MISSING_FORBIDDEN_VENDOR_ROOT",
            f"manifest must forbid vendor root {path}",
        )
        for path in sorted(required_roots - declared_roots)
    )
    return violations


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").replace("_", "-").strip("/").lower()


def _validate_license(
    upstream_id: str, license_data: Mapping[str, Any]
) -> list[Violation]:
    violations: list[Violation] = []
    status = license_data.get("status")
    expression = license_data.get("expression")
    if status not in ALLOWED_LICENSE_STATUSES:
        violations.append(
            Violation(
                "UNKNOWN_LICENSE_STATUS",
                f"{upstream_id} has unapproved license status {status!r}",
            )
        )
    if not isinstance(expression, str) or not expression.strip():
        violations.append(
            Violation(
                "MISSING_LICENSE_EXPRESSION",
                f"{upstream_id} must pin a license expression",
            )
        )
    evidence = _sequence(license_data.get("evidence"), "license.evidence")
    if not evidence:
        violations.append(
            Violation(
                "MISSING_LICENSE_EVIDENCE", f"{upstream_id} has no license evidence"
            )
        )
    return violations


def _validate_adoption(
    upstream_id: str, adoption: Mapping[str, Any]
) -> list[Violation]:
    violations: list[Violation] = []
    mode = adoption.get("mode")
    if mode not in ALLOWED_ADOPTION_MODES:
        violations.append(
            Violation(
                "UNKNOWN_ADOPTION_MODE",
                f"{upstream_id} has unapproved adoption mode {mode!r}",
            )
        )
    for field in ("source_copy_allowed", "in_core_allowed"):
        if not isinstance(adoption.get(field), bool):
            violations.append(
                Violation(
                    "INVALID_ADOPTION_POLICY",
                    f"{upstream_id}.{field} must be boolean",
                )
            )
    return violations


def _check_critical_upstream_policy(
    entries: Mapping[str, Mapping[str, Any]],
    notices: str,
) -> list[Violation]:
    violations: list[Violation] = []
    for upstream_id in ("dexter", "ai-trader"):
        adoption = _mapping(entries[upstream_id].get("adoption"), "adoption")
        if adoption.get("source_copy_allowed") is not False:
            gate = (
                "NO_VENDOR_DEXTER_CODE"
                if upstream_id == "dexter"
                else ("NO_VENDOR_AI_TRADER_CODE")
            )
            violations.append(
                Violation(gate, f"{upstream_id} source copy is forbidden")
            )
    openbb = entries["openbb"]
    license_data = _mapping(openbb.get("license"), "openbb.license")
    adoption = _mapping(openbb.get("adoption"), "openbb.adoption")
    if license_data.get("expression") != "AGPL-3.0-only":
        violations.append(
            Violation("OPENBB_LICENSE_DRIFT", "OpenBB must remain AGPL-3.0-only")
        )
    if adoption.get("in_core_allowed") is not False:
        violations.append(
            Violation("NO_OPENBB_IMPORT_IN_CORE", "OpenBB is forbidden in the core")
        )
    nautilus = entries["nautilus-trader"]
    nautilus_license = _mapping(nautilus.get("license"), "nautilus.license")
    nautilus_adoption = _mapping(nautilus.get("adoption"), "nautilus.adoption")
    nautilus_notice = _mapping(nautilus.get("notice"), "nautilus.notice")
    expected = {
        "repository": NAUTILUS_REPOSITORY,
        "snapshot": NAUTILUS_SNAPSHOT,
    }
    if any(nautilus.get(key) != value for key, value in expected.items()):
        violations.append(
            Violation("NAUTILUS_PROVENANCE_DRIFT", "Nautilus provenance changed")
        )
    if nautilus_license.get("expression") != "LGPL-3.0-or-later":
        violations.append(
            Violation(
                "NAUTILUS_LICENSE_DRIFT",
                "Nautilus must remain LGPL-3.0-or-later",
            )
        )
    if (
        nautilus_adoption.get("mode") != "optional-sidecar"
        or nautilus_adoption.get("in_core_allowed") is not False
        or nautilus_adoption.get("source_copy_allowed") is not False
    ):
        violations.append(
            Violation(
                "NAUTILUS_BOUNDARY_DRIFT",
                "Nautilus must remain an isolated optional sidecar",
            )
        )
    required_notice = (
        nautilus_notice.get("required") is True
        and nautilus_notice.get("id") == NAUTILUS_NOTICE_ID
    )
    notice_fragments = (
        NAUTILUS_NOTICE_ID,
        NAUTILUS_REPOSITORY,
        NAUTILUS_SNAPSHOT,
        "Copyright (C) 2015-2026 Nautech Systems Pty Ltd",
    )
    if not required_notice or any(item not in notices for item in notice_fragments):
        violations.append(
            Violation(
                "NAUTILUS_NOTICE_INCOMPLETE",
                "Nautilus notice provenance or copyright is incomplete",
            )
        )
    return violations


def _validate_manifest(
    manifest: Mapping[str, Any], notices: str
) -> tuple[list[Violation], list[Mapping[str, Any]]]:
    violations = _check_required_gates(manifest) + _check_policy_lists(manifest)
    if manifest.get("schema_version") != 1:
        violations.append(
            Violation("UNSUPPORTED_MANIFEST_SCHEMA", "schema_version must be 1")
        )
    entries = _manifest_entries(manifest)
    indexed: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        upstream_id = entry.get("id")
        if not isinstance(upstream_id, str) or upstream_id in indexed:
            violations.append(
                Violation(
                    "INVALID_UPSTREAM_ID", f"invalid or duplicate id {upstream_id!r}"
                )
            )
            continue
        indexed[upstream_id] = entry
        violations.extend(_validate_entry(upstream_id, entry, notices))
    for upstream_id in sorted(REQUIRED_UPSTREAMS - indexed.keys()):
        violations.append(
            Violation(
                "MISSING_REQUIRED_UPSTREAM", f"missing {upstream_id} manifest entry"
            )
        )
    if indexed.keys() >= REQUIRED_UPSTREAMS:
        violations.extend(_check_critical_upstream_policy(indexed, notices))
    return violations, entries


def _validate_entry(
    upstream_id: str, entry: Mapping[str, Any], notices: str
) -> list[Violation]:
    violations: list[Violation] = []
    snapshot = entry.get("snapshot")
    if not isinstance(snapshot, str) or not COMMIT_PATTERN.fullmatch(snapshot):
        violations.append(
            Violation("INVALID_SNAPSHOT", f"{upstream_id} must pin a 40-char commit")
        )
    repository = entry.get("repository")
    if not isinstance(repository, str) or not repository.startswith(
        "https://github.com/"
    ):
        violations.append(
            Violation(
                "INVALID_REPOSITORY", f"{upstream_id} repository must be GitHub HTTPS"
            )
        )
    license_data = _mapping(entry.get("license"), f"{upstream_id}.license")
    adoption = _mapping(entry.get("adoption"), f"{upstream_id}.adoption")
    notice = _mapping(entry.get("notice"), f"{upstream_id}.notice")
    violations.extend(_validate_license(upstream_id, license_data))
    violations.extend(_validate_adoption(upstream_id, adoption))
    if notice.get("required") is True:
        notice_id = notice.get("id")
        if not isinstance(notice_id, str) or notice_id not in notices:
            violations.append(
                Violation(
                    "MISSING_REQUIRED_NOTICE",
                    f"{upstream_id} required notice id is absent",
                    "THIRD_PARTY_NOTICES.md",
                )
            )
    elif notice.get("required") is not False:
        violations.append(
            Violation(
                "INVALID_NOTICE_POLICY",
                f"{upstream_id}.notice.required must be boolean",
            )
        )
    return violations


def _dependency_name(requirement: Any) -> str | None:
    if not isinstance(requirement, str):
        return None
    match = DEPENDENCY_PATTERN.match(requirement)
    return _normalize_name(match.group(1)) if match else None


def _check_dependencies(root: Path) -> list[Violation]:
    project = _load_toml(root / "pyproject.toml", "pyproject.toml")
    project_data = _mapping(project.get("project"), "pyproject.project")
    raw_dependencies = _sequence(
        project_data.get("dependencies"), "project.dependencies"
    )
    direct = {name for item in raw_dependencies if (name := _dependency_name(item))}
    violations = _dependency_violations(direct, "pyproject.toml")
    lock = _load_toml(root / "uv.lock", "uv.lock")
    raw_packages = _sequence(lock.get("package", ()), "uv.lock.package")
    locked = {
        _normalize_name(str(package.get("name")))
        for raw_package in raw_packages
        if isinstance(raw_package, Mapping)
        and (package := _mapping(raw_package, "uv.lock.package"))
        and isinstance(package.get("name"), str)
    }
    return violations + _dependency_violations(locked, "uv.lock")


def _dependency_violations(names: set[str], path: str) -> list[Violation]:
    return [
        Violation(
            "FORBIDDEN_CORE_DEPENDENCY",
            f"forbidden core dependency: {name}",
            path,
        )
        for name in sorted(names & FORBIDDEN_CORE_DEPENDENCIES)
    ]


def _check_vendor_roots(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    all_paths = [path for path in root.rglob("*") if ".research" not in path.parts]
    normalized = {
        _normalize_path(path.relative_to(root).as_posix()): path for path in all_paths
    }
    for gate, candidates in FORBIDDEN_VENDOR_ROOTS.items():
        for candidate in candidates:
            normalized_candidate = _normalize_path(candidate)
            matches = [
                path
                for relative, path in normalized.items()
                if relative == normalized_candidate
                or relative.startswith(f"{normalized_candidate}/")
            ]
            if matches:
                violations.append(
                    Violation(gate, f"forbidden vendor tree: {candidate}", candidate)
                )
    return violations


def _imported_modules(path: Path) -> Iterable[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".", maxsplit=1)[0]


def _check_core_imports(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for source_root in CORE_SOURCE_ROOTS:
        path_root = root / source_root
        if not path_root.exists():
            continue
        for path in path_root.rglob("*.py"):
            try:
                modules = {
                    _normalize_name(module) for module in _imported_modules(path)
                }
            except (OSError, UnicodeError, SyntaxError) as error:
                violations.append(
                    Violation(
                        "CORE_SOURCE_PARSE_ERROR",
                        str(error),
                        path.relative_to(root).as_posix(),
                    )
                )
                continue
            for name in sorted(modules & FORBIDDEN_CORE_DEPENDENCIES):
                code = (
                    "NO_OPENBB_IMPORT_IN_CORE"
                    if name.startswith("openbb")
                    else "FORBIDDEN_CORE_IMPORT"
                )
                violations.append(
                    Violation(
                        code,
                        f"forbidden core import: {name}",
                        path.relative_to(root).as_posix(),
                    )
                )
    return violations


def _resolve_inside(parent: Path, relative: str) -> Path | None:
    candidate = (parent / relative).resolve()
    try:
        candidate.relative_to(parent.resolve())
    except ValueError:
        return None
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha256(repository: Path, relative: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "show", f"HEAD:{relative}"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def _git_path_dirty(repository: Path, relative: str) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "diff", "--quiet", "--", relative],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in {0, 1}:
        return None
    return result.returncode == 1


def _git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip().lower() if result.returncode == 0 else None


def _check_local_snapshot(root: Path, entry: Mapping[str, Any]) -> list[Violation]:
    upstream_id = str(entry.get("id"))
    local_path = entry.get("local_path")
    if not isinstance(local_path, str):
        return [
            Violation("INVALID_LOCAL_PATH", f"{upstream_id} local_path is required")
        ]
    repository = _resolve_inside(root, local_path)
    if repository is None or not _normalize_path(local_path).startswith(
        ".research/upstreams/"
    ):
        return [Violation("INVALID_LOCAL_PATH", f"unsafe local path for {upstream_id}")]
    if not repository.exists():
        return []
    violations: list[Violation] = []
    head = _git_head(repository)
    if head != entry.get("snapshot"):
        violations.append(
            Violation(
                "UPSTREAM_SNAPSHOT_DRIFT", f"{upstream_id} HEAD is {head!r}", local_path
            )
        )
    license_data = _mapping(entry.get("license"), f"{upstream_id}.license")
    evidence = _sequence(license_data.get("evidence"), "license.evidence")
    violations.extend(_check_evidence(repository, upstream_id, evidence))
    return violations


def _check_evidence(
    repository: Path, upstream_id: str, evidence: Sequence[Any]
) -> list[Violation]:
    violations: list[Violation] = []
    for raw_item in evidence:
        item = _mapping(raw_item, f"{upstream_id}.license.evidence")
        relative = item.get("path")
        expected = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            violations.append(
                Violation(
                    "INVALID_LICENSE_EVIDENCE", f"invalid evidence for {upstream_id}"
                )
            )
            continue
        path = _resolve_inside(repository, relative)
        if path is None or not path.is_file() or not SHA256_PATTERN.fullmatch(expected):
            violations.append(
                Violation(
                    "INVALID_LICENSE_EVIDENCE",
                    f"invalid evidence {upstream_id}/{relative}",
                )
            )
        else:
            canonical = _git_blob_sha256(repository, relative)
            dirty = _git_path_dirty(repository, relative)
            if dirty is True or (canonical or _sha256(path)) != expected:
                violations.append(
                    Violation(
                        "LICENSE_EVIDENCE_DRIFT",
                        f"license evidence drift for {upstream_id}",
                        relative,
                    )
                )
    return violations


def check_repository(root: Path) -> list[Violation]:
    """Return every detected violation; malformed required inputs raise."""

    resolved_root = root.resolve()
    manifest_path = resolved_root / "docs" / "legal" / "upstream-manifest.yaml"
    notices_path = resolved_root / "THIRD_PARTY_NOTICES.md"
    try:
        notices = notices_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PolicyInputError(
            f"cannot read THIRD_PARTY_NOTICES.md: {error}"
        ) from error
    manifest = _load_manifest(manifest_path)
    violations, entries = _validate_manifest(manifest, notices)
    violations.extend(_check_dependencies(resolved_root))
    violations.extend(_check_vendor_roots(resolved_root))
    violations.extend(_check_core_imports(resolved_root))
    for entry in entries:
        violations.extend(_check_local_snapshot(resolved_root, entry))
    return violations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the script's parent repository)",
    )
    return parser


def _emit(success: bool, violations: Sequence[Violation]) -> None:
    payload = {
        "success": success,
        "status": "passed" if success else "failed",
        "data": {"violation_count": len(violations)} if success else None,
        "error": None
        if success
        else {
            "code": "UPSTREAM_POLICY_VIOLATION",
            "message": "upstream policy checks failed",
            "details": [asdict(violation) for violation in violations],
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    args = _parser().parse_args()
    try:
        violations = check_repository(args.root)
    except PolicyInputError as error:
        violations = [Violation("POLICY_INPUT_ERROR", str(error))]
    _emit(not violations, violations)
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
