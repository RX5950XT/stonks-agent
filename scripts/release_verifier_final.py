"""Closed five-evidence verification for one formal keyless release."""

from __future__ import annotations

import json
import stat
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.release_verifier_common import (
    COMMIT_PATTERN,
    IMAGE_PATTERN,
    REPOSITORY_PATTERN,
    VERSION_PATTERN,
    CommandRunner,
    ReleaseError,
    as_mapping,
    as_string,
    load_json,
    positive_int,
    regular_status,
    reject_constant,
    safe_join,
    unique_object,
)
from scripts.release_verifier_signatures import (
    image_bundle_verify_command,
    image_registry_verify_command,
)

FINAL_SCHEMA = "stonks-agent/formal-release-verification/v1"
_FINAL_FIELDS = {
    "image_bundle",
    "manifest_bundle",
    "verification_report_bundle",
    "provenance_bundle",
    "sbom_bundle",
    "provenance_predicate_type",
    "sbom_predicate_type",
    "max_bundle_bytes",
}
_BUNDLE_FIELDS = (
    "image_bundle",
    "manifest_bundle",
    "verification_report_bundle",
    "provenance_bundle",
    "sbom_bundle",
)


def verify_formal_evidence(
    bundle: Path,
    policy: Mapping[str, Any],
    *,
    image: str,
    repository: str,
    tag: str,
    commit: str,
    expected_report: Mapping[str, Any],
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Reverify the complete formal evidence set after attestations land."""
    _verify_identity(image=image, repository=repository, tag=tag, commit=commit)
    signing, final = _final_policy(policy)
    paths = _closed_evidence_paths(bundle, final)
    canonical_sbom = _load_canonical_sbom(bundle, policy, image=image)
    report_path = bundle / "verification-report.json"
    manifest_path = bundle / "release-manifest.json"
    regular_status(manifest_path, max_bytes=4 * 1024 * 1024)
    observed_report = load_json(report_path, max_bytes=4 * 1024 * 1024)
    if observed_report != dict(expected_report):
        raise ReleaseError("verification report drifted")
    commands = _commands(
        paths=paths,
        signing=signing,
        final=final,
        manifest_path=manifest_path,
        report_path=report_path,
        image=image,
        repository=repository,
        tag=tag,
        commit=commit,
    )
    outputs = _run_commands(commands, runner)
    _verify_attestation_output(outputs[4], image, final["provenance_predicate_type"])
    _verify_attestation_output(
        outputs[5],
        image,
        final["sbom_predicate_type"],
        expected_predicate_payload=canonical_sbom,
    )
    return _result(image=image, repository=repository, tag=tag, commit=commit)


def _final_policy(
    policy: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    signing = as_mapping(policy.get("signing"), "policy.signing")
    issuer = as_string(signing.get("issuer"), "signing.issuer")
    workflow = as_string(signing.get("workflow"), "signing.workflow")
    if issuer != "https://token.actions.githubusercontent.com":
        raise ReleaseError("signing issuer is not trusted")
    if workflow != ".github/workflows/release.yml":
        raise ReleaseError("signing workflow identity drifted")
    final = as_mapping(signing.get("final_evidence"), "signing.final_evidence")
    if set(final) != _FINAL_FIELDS:
        raise ReleaseError("final evidence policy fields drifted")
    for field in _BUNDLE_FIELDS[:3]:
        if signing.get(field) != final.get(field):
            raise ReleaseError("formal evidence path policy diverged")
    positive_int(final.get("max_bundle_bytes"), "final evidence max bytes")
    return signing, final


def _closed_evidence_paths(bundle: Path, final: Mapping[str, Any]) -> tuple[Path, ...]:
    _verify_bundle_root(bundle)
    signature_root = bundle / "signatures"
    try:
        root_status = signature_root.lstat()
    except OSError as error:
        raise ReleaseError("formal evidence directory is missing") from error
    if signature_root.is_symlink() or not stat.S_ISDIR(root_status.st_mode):
        raise ReleaseError("formal evidence directory is invalid")
    relatives = tuple(as_string(final.get(field), field) for field in _BUNDLE_FIELDS)
    if len(set(relatives)) != 5:
        raise ReleaseError("formal evidence paths are not unique")
    if any(PurePosixPath(path).parent.as_posix() != "signatures" for path in relatives):
        raise ReleaseError("formal evidence path is outside signatures")
    actual = {f"signatures/{entry.name}" for entry in signature_root.iterdir()}
    if actual != set(relatives):
        raise ReleaseError("formal evidence tree is not closed")
    max_bytes = positive_int(final.get("max_bundle_bytes"), "final evidence max bytes")
    raw_paths = tuple(bundle / PurePosixPath(relative) for relative in relatives)
    for path in raw_paths:
        _verify_raw_evidence_file(path, max_bytes=max_bytes)
    joined_paths = tuple(safe_join(bundle, relative) for relative in relatives)
    try:
        resolved_root = signature_root.resolve(strict=True)
        resolved_paths = tuple(path.resolve(strict=True) for path in joined_paths)
    except (OSError, RuntimeError) as error:
        raise ReleaseError("formal evidence path resolution failed") from error
    if len(set(resolved_paths)) != len(resolved_paths):
        raise ReleaseError("formal evidence resolved paths are not unique")
    if any(path.parent != resolved_root for path in resolved_paths):
        raise ReleaseError("formal evidence resolved path escaped signatures")
    return resolved_paths


def _verify_raw_evidence_file(path: Path, *, max_bytes: int) -> None:
    try:
        result = path.lstat()
    except OSError as error:
        raise ReleaseError("formal evidence file is missing") from error
    if path.is_symlink() or not stat.S_ISREG(result.st_mode):
        raise ReleaseError("formal evidence file is invalid")
    if result.st_size < 0 or result.st_size > max_bytes:
        raise ReleaseError("formal evidence file size is outside policy")
    if result.st_nlink != 1:
        raise ReleaseError("formal evidence file must not be a hardlink")


def _verify_bundle_root(bundle: Path) -> None:
    try:
        root_status = bundle.lstat()
    except OSError as error:
        raise ReleaseError("formal release bundle is missing") from error
    if bundle.is_symlink() or not stat.S_ISDIR(root_status.st_mode):
        raise ReleaseError("formal release bundle is invalid")
    expected = {
        "payload",
        "signatures",
        "release-manifest.json",
        "verification-report.json",
    }
    if {entry.name for entry in bundle.iterdir()} != expected:
        raise ReleaseError("formal release bundle root is not closed")
    for name in ("payload", "signatures"):
        path = bundle / name
        result = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(result.st_mode):
            raise ReleaseError("formal release bundle directory is invalid")


def _load_canonical_sbom(
    bundle: Path, policy: Mapping[str, Any], *, image: str
) -> dict[str, Any]:
    sbom_policy = as_mapping(policy.get("sbom"), "policy.sbom")
    relative = as_string(sbom_policy.get("path"), "sbom.path")
    if relative != "payload/release/core.cdx.json":
        raise ReleaseError("formal SBOM path drifted")
    sbom = load_json(safe_join(bundle, relative), max_bytes=16 * 1024 * 1024)
    serial_name = f"stonks-agent-cyclonedx:{image}"
    expected_serial = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_name)}"
    if (
        sbom.get("bomFormat") != "CycloneDX"
        or sbom.get("specVersion") != "1.6"
        or sbom.get("serialNumber") != expected_serial
    ):
        raise ReleaseError("canonical SBOM identity is invalid")
    return sbom


def _commands(
    *,
    paths: tuple[Path, ...],
    signing: Mapping[str, Any],
    final: Mapping[str, Any],
    manifest_path: Path,
    report_path: Path,
    image: str,
    repository: str,
    tag: str,
    commit: str,
) -> tuple[tuple[str, ...], ...]:
    identity = _identity(repository, signing, tag)
    common = _cosign_identity(identity, repository, tag, commit, signing)
    return (
        image_bundle_verify_command(paths[0], common, image),
        image_registry_verify_command(common, image),
        (
            "cosign",
            "verify-blob",
            "--bundle",
            str(paths[1]),
            *common,
            str(manifest_path),
        ),
        (
            "cosign",
            "verify-blob",
            "--bundle",
            str(paths[2]),
            *common,
            str(report_path),
        ),
        _gh_command(
            paths[3],
            final["provenance_predicate_type"],
            image,
            repository,
            tag,
            commit,
            signing,
        ),
        _gh_command(
            paths[4],
            final["sbom_predicate_type"],
            image,
            repository,
            tag,
            commit,
            signing,
        ),
    )


def _cosign_identity(
    identity: str,
    repository: str,
    tag: str,
    commit: str,
    signing: Mapping[str, Any],
) -> tuple[str, ...]:
    return (
        "--certificate-identity",
        identity,
        "--certificate-oidc-issuer",
        as_string(signing.get("issuer"), "signing.issuer"),
        "--certificate-github-workflow-repository",
        repository,
        "--certificate-github-workflow-ref",
        f"refs/tags/{tag}",
        "--certificate-github-workflow-sha",
        commit,
        "--certificate-github-workflow-trigger",
        "push",
    )


def _gh_command(
    evidence: Path,
    predicate: object,
    image: str,
    repository: str,
    tag: str,
    commit: str,
    signing: Mapping[str, Any],
) -> tuple[str, ...]:
    return (
        "gh",
        "attestation",
        "verify",
        f"oci://{image}",
        "--repo",
        repository,
        "--bundle",
        str(evidence),
        "--cert-identity",
        _identity(repository, signing, tag),
        "--cert-oidc-issuer",
        as_string(signing.get("issuer"), "signing.issuer"),
        "--signer-digest",
        commit,
        "--source-ref",
        f"refs/tags/{tag}",
        "--source-digest",
        commit,
        "--predicate-type",
        as_string(predicate, "attestation predicate type"),
        "--deny-self-hosted-runners",
        "--format",
        "json",
    )


def _identity(repository: str, signing: Mapping[str, Any], tag: str) -> str:
    workflow = as_string(signing.get("workflow"), "signing.workflow")
    return f"https://github.com/{repository}/{workflow}@refs/tags/{tag}"


def _run_commands(
    commands: tuple[tuple[str, ...], ...], runner: CommandRunner
) -> tuple[str, ...]:
    outputs: list[str] = []
    for command in commands:
        try:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ReleaseError("formal evidence verification failed") from error
        if completed.returncode != 0:
            raise ReleaseError("formal evidence verification failed")
        outputs.append(completed.stdout)
    return tuple(outputs)


def _verify_attestation_output(
    output: str,
    image: str,
    predicate: object,
    *,
    expected_predicate_payload: Mapping[str, Any] | None = None,
) -> None:
    try:
        payload = json.loads(
            output,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (ReleaseError, json.JSONDecodeError) as error:
        raise ReleaseError("attestation verification output is invalid") from error
    if not isinstance(payload, list) or len(payload) != 1:
        raise ReleaseError("attestation verification result count drifted")
    entry = as_mapping(payload[0], "attestation verification result")
    result = as_mapping(entry.get("verificationResult"), "verificationResult")
    statement = as_mapping(result.get("statement"), "attestation statement")
    expected_predicate = as_string(predicate, "attestation predicate type")
    if statement.get("predicateType") != expected_predicate:
        raise ReleaseError("attestation predicate drifted")
    observed_predicate = statement.get("predicate")
    if expected_predicate_payload is not None:
        if observed_predicate != dict(expected_predicate_payload):
            raise ReleaseError("SBOM attestation predicate drifted")
    elif not isinstance(observed_predicate, Mapping) or not observed_predicate:
        raise ReleaseError("attestation predicate is invalid")
    _verify_subject(statement.get("subject"), image)


def _verify_subject(value: object, image: str) -> None:
    name, digest = image.rsplit("@sha256:", maxsplit=1)
    expected = [{"name": name, "digest": {"sha256": digest}}]
    if value != expected:
        raise ReleaseError("attestation subject digest drifted")


def _verify_identity(*, image: str, repository: str, tag: str, commit: str) -> None:
    if not IMAGE_PATTERN.fullmatch(image):
        raise ReleaseError("formal image identity is invalid")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError("formal repository identity is invalid")
    image_repository, separator, _digest = image.partition("@sha256:")
    expected_repository = f"ghcr.io/{repository.lower()}"
    if separator != "@sha256:" or image_repository != expected_repository:
        raise ReleaseError(
            "formal image repository does not match formal release repository"
        )
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseError("formal commit identity is invalid")
    if not tag.startswith("v") or not VERSION_PATTERN.fullmatch(tag[1:]):
        raise ReleaseError("formal tag identity is invalid")


def _result(*, image: str, repository: str, tag: str, commit: str) -> dict[str, Any]:
    return {
        "schema_version": FINAL_SCHEMA,
        "status": "passed",
        "evidence_count": 5,
        "image": image,
        "repository": repository,
        "ref": f"refs/tags/{tag}",
        "commit": commit,
    }
