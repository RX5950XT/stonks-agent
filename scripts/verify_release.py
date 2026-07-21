#!/usr/bin/env python3
# ruff: noqa: E402
"""Create and fail-closed verify a Stonks Agent release bundle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import release_source_contracts as source_contract
from scripts import release_verifier_bundle as bundle_contract
from scripts import release_verifier_common as common
from scripts import release_verifier_reports as report_contract
from scripts import release_verifier_signatures as signature_contract

MAX_MANIFEST_BYTES = common.MAX_MANIFEST_BYTES
REPORT_SCHEMA = common.REPORT_SCHEMA
CommandRunner = common.CommandRunner
ReleaseError = common.ReleaseError
audit_locks = bundle_contract.audit_locks
create_manifest = bundle_contract.create_manifest
load_json = common.load_json
stage_release = bundle_contract.stage_release
verify_grype_database_identity = report_contract.verify_grype_database_identity
verify_grype_report = report_contract.verify_grype_report
verify_image_report = report_contract.verify_image_report
verify_openbb_source = report_contract.verify_openbb_source
verify_alpine_source = source_contract.verify_alpine_source
verify_python_source = source_contract.verify_python_source

_artifact_entries = bundle_contract.artifact_entries
_bundle_limits = bundle_contract.bundle_limits
_inventory_files = bundle_contract.inventory_files
_mapping = common.as_mapping
_regular_status = common.regular_status
_required_payload_files = bundle_contract.required_payload_files
_safe_join = common.safe_join
_sha256 = common.sha256
_string = common.as_string
_validate_identity = bundle_contract.validate_identity
_verify_manifest_shape = bundle_contract.verify_manifest_shape
_verify_notices = report_contract.verify_notices
_verify_required_trees = bundle_contract.verify_required_trees
_verify_sbom = report_contract.verify_sbom
_verify_signatures = signature_contract.verify_signatures
_verify_structured_reports = report_contract.verify_structured_reports

__all__ = [
    "ReleaseError",
    "audit_locks",
    "create_manifest",
    "load_json",
    "main",
    "stage_release",
    "verify_alpine_source",
    "verify_grype_database_identity",
    "verify_grype_report",
    "verify_image_report",
    "verify_openbb_source",
    "verify_python_source",
    "verify_release",
]


def verify_release(
    bundle: Path,
    policy: Mapping[str, Any],
    *,
    expected_repository: str,
    expected_tag: str,
    expected_commit: str,
    require_signatures: bool,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    manifest_path = bundle / "release-manifest.json"
    manifest = load_json(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    _verify_manifest_shape(manifest)
    release = _mapping(manifest["release"], "manifest.release")
    image_data = _mapping(manifest["image"], "manifest.image")
    version = _string(release.get("version"), "release.version")
    tag = _string(release.get("tag"), "release.tag")
    repository = _string(release.get("repository"), "release.repository")
    commit = _string(release.get("commit"), "release.commit")
    image = _string(image_data.get("subject"), "image.subject")
    signing_mode = _string(manifest.get("signing_mode"), "signing_mode")
    _validate_identity(
        version=version,
        tag=tag,
        repository=repository,
        commit=commit,
        image=image,
        signing_mode=signing_mode,
    )
    if (
        repository != expected_repository
        or tag != expected_tag
        or commit != expected_commit
    ):
        raise ReleaseError("release identity does not match trusted expectations")
    if release.get("execution_mode") != "paper":
        raise ReleaseError("release is not paper-only")

    actual = _verify_payload_inventory(bundle, policy, manifest)
    _verify_structured_reports(bundle, policy)
    _verify_image_identity_report(
        bundle,
        policy,
        image=image,
        repository=repository,
        commit=commit,
        version=version,
        signing_mode=signing_mode,
    )
    _verify_sbom(bundle, policy, image=image)
    _verify_notices(bundle, policy)
    _verify_corresponding_sources(bundle, policy)
    openbb = _mapping(policy.get("openbb"), "policy.openbb")
    archive_path = _safe_join(bundle, _string(openbb.get("archive"), "openbb.archive"))
    verify_openbb_source(archive_path, openbb)
    signatures_verified = _verify_release_signatures(
        bundle,
        policy,
        manifest_path=manifest_path,
        image=image,
        repository=repository,
        tag=tag,
        commit=commit,
        signing_mode=signing_mode,
        require_signatures=require_signatures,
        runner=runner,
    )
    return _verification_report(
        manifest_path=manifest_path,
        version=version,
        tag=tag,
        repository=repository,
        commit=commit,
        image=image,
        actual=actual,
        signatures_verified=signatures_verified,
    )


def _verify_corresponding_sources(bundle: Path, policy: Mapping[str, Any]) -> None:
    legal_raw = policy.get("legal")
    if legal_raw is not None:
        legal = _mapping(legal_raw, "policy.legal")
        runtime_path = _safe_join(
            bundle,
            _string(
                legal.get("core_runtime_policy_path"),
                "legal.core_runtime_policy_path",
            ),
        )
        runtime = load_json(runtime_path, max_bytes=2 * 1024 * 1024)
        alpine = _mapping(runtime.get("alpine"), "core runtime alpine policy")
        source = _mapping(
            alpine.get("corresponding_source"),
            "Alpine corresponding source policy",
        )
        archive = _safe_join(
            bundle,
            _string(source.get("required_archive_path"), "Alpine source archive"),
        )
        verify_alpine_source(archive, runtime)
    python_raw = policy.get("python_source")
    if python_raw is None:
        return
    python = _mapping(python_raw, "policy.python_source")
    verify_python_source(
        _safe_join(bundle, _string(python.get("archive"), "python_source.archive")),
        _safe_join(bundle, _string(python.get("policy"), "python_source.policy")),
        _safe_join(bundle, _string(python.get("uv_lock"), "python_source.uv_lock")),
        python,
    )


def _verify_payload_inventory(
    bundle: Path,
    policy: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    declared = _artifact_entries(manifest["artifacts"])
    actual = _inventory_files(bundle / "payload", _bundle_limits(policy))
    declared_paths = {item["path"] for item in declared}
    actual_paths = {item["path"] for item in actual}
    extra = sorted(actual_paths - declared_paths)
    if extra:
        raise ReleaseError(f"unexpected payload file: {extra[0]}")
    missing = sorted(declared_paths - actual_paths)
    if missing:
        raise ReleaseError(f"manifest payload file is missing: {missing[0]}")
    actual_by_path = {item["path"]: item for item in actual}
    for item in declared:
        observed = actual_by_path[item["path"]]
        if observed["sha256"] != item["sha256"] or observed["size"] != item["size"]:
            raise ReleaseError(f"payload hash or size drift: {item['path']}")
    missing_required = sorted(_required_payload_files(policy) - actual_paths)
    if missing_required:
        raise ReleaseError(f"required payload file is missing: {missing_required[0]}")
    _verify_required_trees(policy, actual_paths)
    return actual


def _verify_image_identity_report(
    bundle: Path,
    policy: Mapping[str, Any],
    *,
    image: str,
    repository: str,
    commit: str,
    version: str,
    signing_mode: str,
) -> None:
    reports = _mapping(policy.get("reports"), "policy.reports")
    image_report_path = reports.get("core_image")
    if not isinstance(image_report_path, str):
        return
    verify_image_report(
        load_json(_safe_join(bundle, image_report_path), max_bytes=1024 * 1024),
        image=image,
        repository=repository,
        commit=commit,
        version=version,
        require_registry_identity=signing_mode == "keyless-release",
    )


def _verify_release_signatures(
    bundle: Path,
    policy: Mapping[str, Any],
    *,
    manifest_path: Path,
    image: str,
    repository: str,
    tag: str,
    commit: str,
    signing_mode: str,
    require_signatures: bool,
    runner: CommandRunner,
) -> bool:
    if require_signatures:
        if signing_mode != "keyless-release":
            raise ReleaseError("formal verification requires keyless-release mode")
        _verify_signatures(
            bundle,
            policy,
            manifest_path=manifest_path,
            image=image,
            repository=repository,
            tag=tag,
            commit=commit,
            runner=runner,
        )
        return True
    if signing_mode == "keyless-release":
        raise ReleaseError("keyless release cannot skip signature verification")
    return False


def _verification_report(
    *,
    manifest_path: Path,
    version: str,
    tag: str,
    repository: str,
    commit: str,
    image: str,
    actual: list[dict[str, Any]],
    signatures_verified: bool,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA,
        "success": True,
        "status": "passed",
        "release": {
            "version": version,
            "tag": tag,
            "repository": repository,
            "commit": commit,
            "execution_mode": "paper",
        },
        "image": image,
        "manifest_sha256": _sha256(manifest_path),
        "artifact_count": len(actual),
        "total_bytes": sum(int(item["size"]) for item in actual),
        "signatures_verified": signatures_verified,
    }


def _load_policy(path: Path) -> dict[str, Any]:
    payload = load_json(path, max_bytes=1024 * 1024)
    if payload.get("schema_version") != "stonks-agent/release-policy/v1":
        raise ReleaseError("release policy schema is invalid")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    content = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if path.exists() or path.is_symlink():
        _regular_status(path, max_bytes=MAX_MANIFEST_BYTES)
    try:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
    except OSError as error:
        raise ReleaseError("cannot write release JSON output") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "release-policy.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--bundle", required=True, type=Path)
    create.add_argument("--version", required=True)
    create.add_argument("--tag", required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--image", required=True)
    create.add_argument(
        "--signing-mode",
        choices=("unsigned-candidate", "keyless-release"),
        required=True,
    )
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", required=True, type=Path)
    verify.add_argument("--expected-repository", required=True)
    verify.add_argument("--expected-tag", required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--require-signatures", action="store_true")
    verify.add_argument("--report", type=Path)
    locks = subparsers.add_parser("audit-locks")
    locks.add_argument("--root", required=True, type=Path)
    locks.add_argument("--report", required=True, type=Path)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--root", required=True, type=Path)
    stage.add_argument("--bundle", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = _run_command(args, _load_policy(args.policy))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ReleaseError, OSError, subprocess.SubprocessError):
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "failed",
                    "data": None,
                    "error": {
                        "code": "RELEASE_VERIFICATION_FAILED",
                        "message": "release verification failed closed",
                    },
                },
                sort_keys=True,
            )
        )
        return 1


def _run_command(
    args: argparse.Namespace, policy: Mapping[str, Any]
) -> Mapping[str, Any]:
    if args.command == "create":
        manifest = create_manifest(
            args.bundle,
            policy,
            version=args.version,
            tag=args.tag,
            repository=args.repository,
            commit=args.commit,
            image=args.image,
            signing_mode=args.signing_mode,
        )
        _write_json(args.bundle / "release-manifest.json", manifest)
        return {
            "success": True,
            "status": "created",
            "data": {
                "artifact_count": len(manifest["artifacts"]),
                "signing_mode": args.signing_mode,
            },
            "error": None,
        }
    if args.command == "verify":
        report = verify_release(
            args.bundle,
            policy,
            expected_repository=args.expected_repository,
            expected_tag=args.expected_tag,
            expected_commit=args.expected_commit,
            require_signatures=args.require_signatures,
        )
        if args.report is not None:
            _write_json(args.report, report)
        return {
            "success": True,
            "status": "passed",
            "data": report,
            "error": None,
        }
    if args.command == "audit-locks":
        result = audit_locks(args.root, policy)
        _write_json(args.report, result)
        return result
    count = stage_release(args.root, args.bundle, policy)
    return {
        "success": True,
        "status": "staged",
        "data": {"file_count": count},
        "error": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
