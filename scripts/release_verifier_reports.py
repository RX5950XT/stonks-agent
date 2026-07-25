"""Semantic verification for release reports, SBOM, notices, and source offers."""

from __future__ import annotations

import hashlib
import re
import tarfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.release_verifier_bundle import verify_uv_lock
from scripts.release_verifier_common import (
    SHA256_PATTERN,
    ReleaseError,
    as_mapping,
    as_string,
    canonical_json_bytes,
    load_json,
    positive_int,
    regular_status,
    safe_join,
    sha256,
    validate_relative_path,
)

BLOCKED_SEVERITIES = frozenset({"High", "Critical"})


def verify_grype_report(
    payload: Mapping[str, Any],
    approved_suppressions: Mapping[str, tuple[str, str]] | None = None,
) -> None:
    descriptor_value = payload.get("descriptor")
    if not isinstance(descriptor_value, Mapping):
        raise ReleaseError("Grype database identity is missing or invalid")
    database_value = descriptor_value.get("db")
    if not isinstance(database_value, Mapping):
        raise ReleaseError("Grype database identity is missing or invalid")
    status = database_value.get("status")
    identity = status if isinstance(status, Mapping) else database_value
    built = identity.get("built") if isinstance(identity, Mapping) else None
    valid = identity.get("valid", True) if isinstance(identity, Mapping) else False
    if not isinstance(built, str) or not built or valid is not True:
        raise ReleaseError("Grype database identity is missing or invalid")
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise ReleaseError("Grype matches must be a list")
    for index, raw in enumerate(matches):
        match = as_mapping(raw, f"Grype match {index}")
        vulnerability = as_mapping(
            match.get("vulnerability"), f"Grype match {index}.vulnerability"
        )
        severity = vulnerability.get("severity")
        if severity in BLOCKED_SEVERITIES:
            identifier = vulnerability.get("id", "unknown")
            raise ReleaseError(f"unsuppressed {severity} vulnerability: {identifier}")
    _verify_ignored_grype_matches(payload, approved_suppressions)


def _verify_ignored_grype_matches(
    payload: Mapping[str, Any],
    approved_suppressions: Mapping[str, tuple[str, str]] | None,
) -> None:
    ignored = payload.get("ignoredMatches", [])
    if not isinstance(ignored, list):
        raise ReleaseError("Grype ignored matches must be a list")
    blocked_ignored: set[str] = set()
    for raw in ignored:
        match = as_mapping(raw, "Grype ignored match")
        vulnerability = as_mapping(
            match.get("vulnerability"), "Grype ignored vulnerability"
        )
        if vulnerability.get("severity") not in BLOCKED_SEVERITIES:
            continue
        identifier = as_string(vulnerability.get("id"), "ignored vulnerability id")
        artifact = as_mapping(match.get("artifact"), "Grype ignored artifact")
        purl = as_string(artifact.get("purl"), "Grype ignored artifact purl")
        rules = match.get("appliedIgnoreRules")
        if (
            approved_suppressions is None
            or identifier not in approved_suppressions
            or approved_suppressions[identifier][0] != purl
            or not _exact_vex_rule(rules)
        ):
            raise ReleaseError(f"unreviewed suppressed vulnerability: {identifier}")
        blocked_ignored.add(identifier)
    stale = sorted(set(approved_suppressions or {}) - blocked_ignored)
    if stale:
        raise ReleaseError(f"stale VEX statement: {stale[0]}")
    if blocked_ignored and approved_suppressions is None:
        raise ReleaseError(
            f"unreviewed suppressed vulnerability: {sorted(blocked_ignored)[0]}"
        )


def verify_grype_database_identity(
    report: Mapping[str, Any], status: Mapping[str, Any]
) -> None:
    descriptor = as_mapping(report.get("descriptor"), "Grype descriptor")
    database = as_mapping(descriptor.get("db"), "Grype database descriptor")
    identity = as_mapping(
        database.get("status", database), "Grype scan database identity"
    )
    fields = ("schemaVersion", "from", "built", "path", "valid")
    observed = {field: identity.get(field) for field in fields}
    recorded = {field: status.get(field) for field in fields}
    if (
        not isinstance(recorded["schemaVersion"], str)
        or not recorded["schemaVersion"]
        or not isinstance(recorded["from"], str)
        or not recorded["from"].startswith("https://grype.anchore.io/databases/")
        or not isinstance(recorded["built"], str)
        or not recorded["built"]
        or not isinstance(recorded["path"], str)
        or not recorded["path"].endswith("/vulnerability.db")
        or recorded["valid"] is not True
    ):
        raise ReleaseError("Grype database status is invalid")
    if recorded != observed:
        raise ReleaseError("Grype database status does not match scan descriptor")


def verify_image_report(
    payload: Mapping[str, Any],
    *,
    image: str,
    repository: str,
    commit: str,
    version: str,
    require_registry_identity: bool,
) -> None:
    digest = image.rsplit("@", maxsplit=1)[1]
    expected: dict[str, object] = {
        "schema_version": "stonks-agent/core-image/v1",
        "subject": image,
        "digest": digest,
        "repository": repository,
        "revision": commit,
        "version": version,
        "source": f"https://github.com/{repository}",
        "licenses": "Apache-2.0",
        "user": "65532:65532",
        "execution_mode": "paper",
    }
    allowed = {*expected, "config_digest", "registry_verified"}
    if set(payload) != allowed:
        raise ReleaseError("core image report is not a closed contract")
    config_digest = payload.get("config_digest")
    if not isinstance(config_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", config_digest
    ):
        raise ReleaseError("core image config digest is invalid")
    if any(payload.get(name) != value for name, value in expected.items()):
        raise ReleaseError("core image identity drifted")
    registry_verified = payload.get("registry_verified")
    if not isinstance(registry_verified, bool):
        raise ReleaseError("core image registry identity is invalid")
    if require_registry_identity and registry_verified is not True:
        raise ReleaseError("formal release lacks verified registry identity")


def verify_openbb_source(archive_path: Path, policy: Mapping[str, Any]) -> None:
    max_members = positive_int(policy.get("max_members"), "openbb.max_members")
    max_member_bytes = positive_int(
        policy.get("max_member_bytes"), "openbb.max_member_bytes"
    )
    max_expanded_bytes = positive_int(
        policy.get("max_expanded_bytes"), "openbb.max_expanded_bytes"
    )
    required_raw = as_mapping(policy.get("required_members"), "openbb.required_members")
    required = {str(key): value for key, value in required_raw.items()}
    regular_status(archive_path, max_bytes=max(max_expanded_bytes, max_member_bytes))
    _verify_deterministic_gzip_header(archive_path)
    observed, ordered_names = _inspect_openbb_archive(
        archive_path,
        max_members=max_members,
        max_member_bytes=max_member_bytes,
        max_expanded_bytes=max_expanded_bytes,
    )
    if ordered_names != sorted(ordered_names):
        raise ReleaseError("OpenBB archive member order is nondeterministic")
    missing = sorted(set(required) - set(observed))
    if missing:
        raise ReleaseError(f"required OpenBB source member is missing: {missing[0]}")
    for name, expected in required.items():
        if (
            isinstance(expected, str)
            and SHA256_PATTERN.fullmatch(expected)
            and observed[name] != expected
        ):
            raise ReleaseError(f"OpenBB source member hash drift: {name}")


def _verify_deterministic_gzip_header(archive_path: Path) -> None:
    try:
        with archive_path.open("rb") as source:
            header = source.read(10)
    except OSError as error:
        raise ReleaseError("OpenBB corresponding source cannot be read") from error
    if (
        len(header) < 10
        or header[:3] != b"\x1f\x8b\x08"
        or header[3] != 0
        or header[4:8] != b"\0\0\0\0"
    ):
        raise ReleaseError("OpenBB archive has a nondeterministic gzip header")


def _inspect_openbb_archive(
    archive_path: Path,
    *,
    max_members: int,
    max_member_bytes: int,
    max_expanded_bytes: int,
) -> tuple[dict[str, str], list[str]]:
    observed: dict[str, str] = {}
    casefolded: set[str] = set()
    ordered_names: list[str] = []
    total = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > max_members:
                raise ReleaseError("OpenBB archive member count is outside policy")
            for member in members:
                name = member.name.removeprefix("./")
                validate_relative_path(name, label="archive member")
                if not member.isfile():
                    raise ReleaseError("OpenBB archive may contain regular files only")
                if name in observed or name.casefold() in casefolded:
                    raise ReleaseError(f"duplicate OpenBB archive member: {name}")
                if (
                    member.mtime != 0
                    or member.uid != 0
                    or member.gid != 0
                    or member.mode & 0o7777 != 0o644
                ):
                    raise ReleaseError(
                        "OpenBB archive member has nondeterministic metadata"
                    )
                if member.size < 0 or member.size > max_member_bytes:
                    raise ReleaseError("OpenBB archive member exceeds size policy")
                total += member.size
                if total > max_expanded_bytes:
                    raise ReleaseError("OpenBB archive expanded size exceeds policy")
                handle = archive.extractfile(member)
                if handle is None:
                    raise ReleaseError("OpenBB archive member cannot be read")
                observed[name] = hashlib.sha256(
                    handle.read(max_member_bytes + 1)
                ).hexdigest()
                casefolded.add(name.casefold())
                ordered_names.append(name)
    except ReleaseError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError("OpenBB corresponding source is invalid") from error
    return observed, ordered_names


def verify_structured_reports(bundle: Path, policy: Mapping[str, Any]) -> None:
    reports = as_mapping(policy.get("reports"), "policy.reports")
    for name in ("secret", "upstream"):
        relative = as_string(reports.get(name), f"reports.{name}")
        payload = load_json(safe_join(bundle, relative), max_bytes=16 * 1024 * 1024)
        if payload.get("success") is not True:
            raise ReleaseError(f"{name} report did not pass")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ReleaseError(f"{name} report lacks result data")
        counter = "finding_count" if name == "secret" else "violation_count"
        if data.get(counter) != 0:
            raise ReleaseError(f"{name} report contains findings")
    grype = _verify_grype_and_vex(bundle, policy, reports)
    _verify_optional_reports(bundle, policy, reports, grype)


def _verify_grype_and_vex(
    bundle: Path,
    policy: Mapping[str, Any],
    reports: Mapping[str, Any],
) -> dict[str, Any]:
    grype_path = as_string(reports.get("grype"), "reports.grype")
    grype = load_json(safe_join(bundle, grype_path), max_bytes=128 * 1024 * 1024)
    suppressions = None
    if policy.get("vulnerabilities") is not None:
        vulnerability_policy = as_mapping(
            policy.get("vulnerabilities"), "policy.vulnerabilities"
        )
        vex_path = vulnerability_policy.get("vex_path")
        if isinstance(vex_path, str):
            suppressions = approved_vex(
                load_json(safe_join(bundle, vex_path), max_bytes=4 * 1024 * 1024)
            )
    verify_grype_report(grype, suppressions)
    return grype


def _verify_optional_reports(
    bundle: Path,
    policy: Mapping[str, Any],
    reports: Mapping[str, Any],
    grype: Mapping[str, Any],
) -> None:
    database_path = reports.get("grype_database")
    if isinstance(database_path, str):
        verify_grype_database_identity(
            grype,
            load_json(safe_join(bundle, database_path), max_bytes=4 * 1024 * 1024),
        )
    dependency_path = reports.get("dependency_audit")
    if isinstance(dependency_path, str):
        verify_dependency_audit(
            load_json(safe_join(bundle, dependency_path), max_bytes=64 * 1024 * 1024)
        )
    lock_path = reports.get("lock")
    if isinstance(lock_path, str):
        verify_lock_report(
            bundle,
            policy,
            load_json(safe_join(bundle, lock_path), max_bytes=4 * 1024 * 1024),
        )


def approved_vex(payload: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    if payload.get("@context") != "https://openvex.dev/ns/v0.2.0":
        raise ReleaseError("OpenVEX context is invalid")
    statements = payload.get("statements")
    if not isinstance(statements, list) or not statements:
        raise ReleaseError("OpenVEX statements are empty")
    allowed_justifications = {
        "inline_mitigations_already_exist",
        "vulnerable_code_not_in_execute_path",
        "vulnerable_code_not_present",
    }
    result: dict[str, tuple[str, str]] = {}
    for raw in statements:
        statement = as_mapping(raw, "OpenVEX statement")
        vulnerability = as_mapping(
            statement.get("vulnerability"), "OpenVEX vulnerability"
        )
        identifier = as_string(vulnerability.get("name"), "OpenVEX CVE")
        products = statement.get("products")
        if not isinstance(products, list) or len(products) != 1:
            raise ReleaseError("OpenVEX statement must bind one exact product")
        product = as_mapping(products[0], "OpenVEX product")
        purl = as_string(product.get("@id"), "OpenVEX product purl")
        justification = as_string(
            statement.get("justification"), "OpenVEX justification"
        )
        if (
            not re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", identifier)
            or purl != "pkg:generic/python@3.12.13"
            or statement.get("status") != "not_affected"
            or justification not in allowed_justifications
            or identifier in result
        ):
            raise ReleaseError("OpenVEX statement drifted")
        result[identifier] = (purl, justification)
    return result


def _exact_vex_rule(value: object) -> bool:
    return value == [{"namespace": "vex", "vex-status": "not_affected"}]


def verify_dependency_audit(payload: Mapping[str, Any]) -> None:
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise ReleaseError("dependency audit report is empty")
    for raw in dependencies:
        dependency = as_mapping(raw, "dependency audit entry")
        vulnerabilities = dependency.get("vulns")
        if not isinstance(vulnerabilities, list):
            raise ReleaseError("dependency audit entry is invalid")
        if vulnerabilities:
            raise ReleaseError("dependency audit contains vulnerabilities")


def verify_lock_report(
    bundle: Path, policy: Mapping[str, Any], payload: Mapping[str, Any]
) -> None:
    if payload.get("success") is not True:
        raise ReleaseError("lock report did not pass")
    data = as_mapping(payload.get("data"), "lock report data")
    projects = data.get("projects")
    if not isinstance(projects, list):
        raise ReleaseError("lock report project inventory is invalid")
    lock_policy = as_mapping(policy.get("locks"), "policy.locks")
    expected_projects = lock_policy.get("uv_projects")
    if not isinstance(expected_projects, list):
        raise ReleaseError("release lock policy is invalid")
    observed: dict[str, Mapping[str, Any]] = {}
    for raw in projects:
        item = as_mapping(raw, "lock report project")
        project = as_string(item.get("project"), "lock project")
        if project in observed:
            raise ReleaseError("lock report project is duplicated")
        observed[project] = item
    if set(observed) != set(expected_projects):
        raise ReleaseError("lock report project inventory drifted")
    _verify_lock_projects(bundle, expected_projects, observed)
    _verify_nuget_locks(bundle, lock_policy, data)


def _verify_lock_projects(
    bundle: Path,
    expected_projects: list[object],
    observed: Mapping[str, Mapping[str, Any]],
) -> None:
    for project in expected_projects:
        if not isinstance(project, str):
            raise ReleaseError("lock project path is invalid")
        relative = "payload/uv.lock" if project == "." else f"payload/{project}/uv.lock"
        lock_path = safe_join(bundle, relative)
        item = observed[project]
        if item.get("status") != "passed" or item.get("sha256") != sha256(lock_path):
            raise ReleaseError(f"lock report drifted: {project}")
        verify_uv_lock(lock_path)


def _verify_nuget_locks(
    bundle: Path, lock_policy: Mapping[str, Any], data: Mapping[str, Any]
) -> None:
    expected_nuget = positive_int(
        lock_policy.get("nuget_lock_count"), "locks.nuget_lock_count"
    )
    if data.get("nuget_lock_count") != expected_nuget:
        raise ReleaseError("NuGet lock report count drifted")
    nuget_root = as_string(lock_policy.get("nuget_tree"), "locks.nuget_tree")
    files = [
        path
        for path in (bundle / f"payload/{nuget_root}/").rglob("packages.lock.json")
        if path.is_file() and not path.is_symlink()
    ]
    if len(files) != expected_nuget:
        raise ReleaseError("NuGet lock tree count drifted")
    for path in files:
        document = load_json(path, max_bytes=16 * 1024 * 1024)
        if document.get("version") != 1 or not isinstance(
            document.get("dependencies"), Mapping
        ):
            raise ReleaseError("NuGet lock document is invalid")


def verify_sbom(bundle: Path, policy: Mapping[str, Any], *, image: str) -> None:
    sbom_policy = as_mapping(policy.get("sbom"), "policy.sbom")
    sbom_path = safe_join(bundle, as_string(sbom_policy.get("path"), "sbom.path"))
    inventory_path = safe_join(
        bundle,
        as_string(sbom_policy.get("inventory_path"), "sbom.inventory_path"),
    )
    sbom = load_json(sbom_path, max_bytes=128 * 1024 * 1024)
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        raise ReleaseError("release SBOM must use CycloneDX 1.6")
    if sbom.get("serialNumber") != _deterministic_sbom_serial(image):
        raise ReleaseError("release SBOM serialNumber is not image-bound")
    metadata = as_mapping(sbom.get("metadata"), "SBOM metadata")
    if "timestamp" in metadata:
        raise ReleaseError("release SBOM contains nondeterministic timestamp")
    components = sbom.get("components")
    if not isinstance(components, list) or not components:
        raise ReleaseError("release SBOM component inventory is empty")
    inventory = load_json(inventory_path, max_bytes=64 * 1024 * 1024)
    if inventory.get("schema_version") != "stonks-agent/sbom-inventory/v1":
        raise ReleaseError("SBOM inventory schema is invalid")
    if inventory.get("image_reference") != image:
        raise ReleaseError("SBOM image identity does not match release image")
    packages = inventory.get("components")
    if not isinstance(packages, list) or not packages:
        raise ReleaseError("SBOM package inventory is empty")
    if inventory.get("component_count") != len(packages):
        raise ReleaseError("SBOM package count drifted")
    component_hash = hashlib.sha256(canonical_json_bytes(packages)).hexdigest()
    if inventory.get("components_sha256") != component_hash:
        raise ReleaseError("SBOM package inventory hash drifted")
    _verify_package_licenses(packages)
    expected_hash = sbom_policy.get("expected_components_sha256")
    if (
        isinstance(expected_hash, str)
        and SHA256_PATTERN.fullmatch(expected_hash)
        and component_hash != expected_hash
    ):
        raise ReleaseError("reviewed license inventory drifted")


def _deterministic_sbom_serial(image: str) -> str:
    name = f"stonks-agent-cyclonedx:{image}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, name)}"


def _verify_package_licenses(packages: list[object]) -> None:
    purls: set[str] = set()
    for raw in packages:
        item = as_mapping(raw, "SBOM package")
        purl = item.get("purl")
        licenses = item.get("licenses")
        if not isinstance(purl, str) or purl in purls:
            raise ReleaseError("SBOM package purl is missing or duplicated")
        if (
            not isinstance(licenses, list)
            or not licenses
            or not all(isinstance(value, str) and value for value in licenses)
        ):
            raise ReleaseError(f"SBOM package has unknown license: {purl}")
        purls.add(purl)


def verify_notices(bundle: Path, policy: Mapping[str, Any]) -> None:
    legal = policy.get("legal")
    if legal is None:
        return
    legal_policy = as_mapping(legal, "policy.legal")
    notices_path = safe_join(
        bundle,
        as_string(legal_policy.get("notices_path"), "legal.notices_path"),
    )
    try:
        notices = notices_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseError("third-party notices cannot be read") from error
    tokens = legal_policy.get("required_notice_ids", [])
    if not isinstance(tokens, list):
        raise ReleaseError("required notice ids must be a list")
    _verify_required_notice_ids(notices, tokens)
    _verify_dedicated_notices(bundle, policy, legal_policy, tokens)
    _verify_feature_notices(bundle, policy, legal_policy, notices, tokens)
    verify_core_runtime_legal(bundle, legal_policy)


def _verify_required_notice_ids(notices: str, notice_ids: list[object]) -> None:
    seen: set[str] = set()
    for notice_id in notice_ids:
        if not isinstance(notice_id, str):
            raise ReleaseError(f"required third-party notice is missing: {notice_id}")
        if notice_id in seen:
            raise ReleaseError("required notice identity is duplicated")
        if notices.count(f"## {notice_id}\n") != 1:
            raise ReleaseError(f"required third-party notice is missing: {notice_id}")
        seen.add(notice_id)


def _verify_dedicated_notices(
    bundle: Path,
    policy: Mapping[str, Any],
    legal: Mapping[str, Any],
    notice_ids: list[object],
) -> None:
    raw = legal.get("dedicated_notices")
    if raw is None:
        return
    if not isinstance(raw, list) or not raw:
        raise ReleaseError("dedicated notice policy must be a non-empty list")
    required = _required_release_paths(policy)
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_runtime_paths: set[str] = set()
    for value in raw:
        item = as_mapping(value, "dedicated notice")
        if set(item) != {"id", "path", "sha256", "runtime_path"}:
            raise ReleaseError("dedicated notice policy fields drifted")
        notice_id = as_string(item.get("id"), "dedicated notice id")
        path = as_string(item.get("path"), "dedicated notice path")
        expected_hash = as_string(item.get("sha256"), "dedicated notice sha256")
        runtime_path = as_string(
            item.get("runtime_path"), "dedicated notice runtime path"
        )
        validate_relative_path(path, label="dedicated notice path")
        filename = path.rsplit("/", maxsplit=1)[-1]
        if (
            notice_id in seen_ids
            or path in seen_paths
            or runtime_path in seen_runtime_paths
            or notice_id not in notice_ids
            or path not in required
            or not path.startswith("payload/docs/legal/notices/")
            or not SHA256_PATTERN.fullmatch(expected_hash)
            or runtime_path != f"/usr/share/licenses/stonks-agent/{filename}"
        ):
            raise ReleaseError("dedicated notice closure is invalid")
        notice_path = safe_join(bundle, path)
        regular_status(notice_path, max_bytes=2_000_000)
        if sha256(notice_path) != expected_hash:
            raise ReleaseError("dedicated notice content drifted")
        seen_ids.add(notice_id)
        seen_paths.add(path)
        seen_runtime_paths.add(runtime_path)


def _verify_feature_notices(
    bundle: Path,
    policy: Mapping[str, Any],
    legal: Mapping[str, Any],
    notices: str,
    notice_ids: list[object],
) -> None:
    raw = legal.get("feature_notices")
    if raw is None:
        return
    if not isinstance(raw, list) or not raw:
        raise ReleaseError("feature notice policy must be a non-empty list")
    required = _required_release_paths(policy)
    if "payload/config/features.yaml" not in required:
        raise ReleaseError("feature catalog is not signed")
    seen_integrations: set[str] = set()
    seen_notice_ids: set[str] = set()
    seen_paths: set[str] = set()
    for value in raw:
        item = as_mapping(value, "feature notice")
        if set(item) != {
            "integration",
            "root_notice_id",
            "paths",
            "execution_authority",
        }:
            raise ReleaseError("feature notice policy fields drifted")
        integration = as_string(item.get("integration"), "feature integration")
        notice_id = as_string(item.get("root_notice_id"), "feature notice id")
        if (
            integration in seen_integrations
            or notice_id in seen_notice_ids
            or notice_id not in notice_ids
        ):
            raise ReleaseError("feature notice identity drifted")
        if item.get("execution_authority") is not False:
            raise ReleaseError("feature notice grants execution authority")
        _verify_feature_notice_section(notices, notice_id)
        paths = item.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ReleaseError("feature notice paths are invalid")
        for path_value in paths:
            path = as_string(path_value, "feature notice path")
            validate_relative_path(path, label="feature notice path")
            if path in seen_paths or f"payload/{path}" not in required:
                raise ReleaseError("feature notice is not uniquely signed")
            regular_status(safe_join(bundle, f"payload/{path}"), max_bytes=2_000_000)
            seen_paths.add(path)
        seen_integrations.add(integration)
        seen_notice_ids.add(notice_id)


def _required_release_paths(policy: Mapping[str, Any]) -> set[str]:
    bundle = as_mapping(policy.get("bundle"), "policy.bundle")
    raw = bundle.get("required_payload_files")
    if not isinstance(raw, list):
        raise ReleaseError("required payload files are invalid")
    return {as_string(value, "required payload file") for value in raw}


def _verify_feature_notice_section(notices: str, notice_id: str) -> None:
    heading = f"## {notice_id}\n"
    if notices.count(heading) != 1:
        raise ReleaseError("feature root notice identity drifted")
    section = notices.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    if "execution authority" not in " ".join(section.split()):
        raise ReleaseError("feature root notice lacks authority boundary")


def verify_core_runtime_legal(bundle: Path, legal_policy: Mapping[str, Any]) -> None:
    raw_path = legal_policy.get("core_runtime_policy_path")
    if raw_path is None:
        return
    runtime_policy = load_json(
        safe_join(bundle, as_string(raw_path, "legal.core_runtime_policy_path")),
        max_bytes=2 * 1024 * 1024,
    )
    if runtime_policy.get("schema_version") != "stonks-agent/core-runtime-legal/v1":
        raise ReleaseError("core runtime legal policy schema is invalid")
    alpine = as_mapping(runtime_policy.get("alpine"), "core runtime alpine policy")
    source = as_mapping(
        alpine.get("corresponding_source"), "Alpine corresponding source policy"
    )
    if (
        source.get("required_for_distribution") is not True
        or source.get("status") != "verified"
        or source.get("release_decision") != "allow"
    ):
        raise ReleaseError("Alpine corresponding source closure is incomplete")
