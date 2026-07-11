"""Container hardening and corresponding-source checks for OpenBB."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from openbb_sidecar_policy import (
    COMMIT_PATTERN,
    EXPECTED_LICENSE,
    SHA256_PATTERN,
    Inputs,
    PolicyInputError,
    Violation,
    compose_service,
    literal_constants,
    manifest_packages,
    mapping,
)


def _docker_instructions(dockerfile: str) -> tuple[str, ...]:
    logical: list[str] = []
    pending = ""
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(re.sub(r"\s+", " ", pending))
        pending = ""
    if pending:
        logical.append(re.sub(r"\s+", " ", pending))
    return tuple(logical)


def _docker_user(instructions: Sequence[str]) -> tuple[str, int]:
    indexes = [
        index for index, item in enumerate(instructions) if item.startswith("USER ")
    ]
    if not indexes:
        return "", -1
    index = indexes[-1]
    return instructions[index].removeprefix("USER "), index


def _check_docker_build(inputs: Inputs) -> list[Violation]:
    instructions = _docker_instructions(inputs.dockerfile)
    from_values = [item.split()[1] for item in instructions if item.startswith("FROM ")]
    violations: list[Violation] = []
    if not from_values or any(
        re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", value) is None
        for value in from_values
    ):
        violations.append(Violation("UNPINNED_DOCKER_BASE", "base digest missing"))
    joined = "\n".join(instructions)
    if "uv sync --frozen" not in joined or "uv run --frozen openbb-build" not in joined:
        violations.append(
            Violation("NON_FROZEN_SIDECAR_BUILD", "frozen build steps missing")
        )
    user, user_index = _docker_user(instructions)
    build_indexes = [
        index for index, item in enumerate(instructions) if "openbb-build" in item
    ]
    if (
        not build_indexes
        or max(build_indexes) > user_index
        or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user) is None
    ):
        violations.append(Violation("SIDECAR_NOT_NONROOT", "invalid Docker USER"))
    return violations


def _check_compose_hardening(inputs: Inputs) -> list[Violation]:
    service = compose_service(inputs)
    environment = mapping(service.get("environment"), "compose environment")
    _, docker_user_index = _docker_user(_docker_instructions(inputs.dockerfile))
    docker_user = ""
    if docker_user_index >= 0:
        docker_user = _docker_instructions(inputs.dockerfile)[
            docker_user_index
        ].removeprefix("USER ")
    security_opt = service.get("security_opt")
    secure = (
        service.get("read_only") is True
        and str(service.get("user", "")) == docker_user
        and service.get("cap_drop") == ["ALL"]
        and isinstance(security_opt, Sequence)
        and "no-new-privileges:true" in security_opt
        and service.get("privileged") is not True
        and environment.get("OPENBB_AUTO_BUILD") == "false"
    )
    violations: list[Violation] = []
    if not secure:
        violations.append(
            Violation("INSECURE_SIDECAR_RUNTIME", "runtime hardening drifted")
        )
    if service.get("ports") != ["127.0.0.1:6900:6900"]:
        violations.append(Violation("NON_LOOPBACK_BIND", "port is not loopback-only"))
    return violations


def _remote_adds(dockerfile: str) -> tuple[str, ...]:
    return tuple(
        item for item in _docker_instructions(dockerfile) if item.startswith("ADD ")
    )


def _valid_sdist_url(value: Any, member: Any) -> bool:
    if not isinstance(value, str) or not isinstance(member, str):
        return False
    parsed = urlsplit(value)
    member_path = PurePosixPath(member)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "files.pythonhosted.org"
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and member_path.parts[:1] == ("upstream",)
        and ".." not in member_path.parts
        and member_path.name == PurePosixPath(parsed.path).name
        and member_path.suffixes[-2:] == [".tar", ".gz"]
    )


def _archive_add_is_exact(adds: Sequence[str], url: str, checksum: str) -> bool:
    matching = [
        item
        for item in adds
        if url in item
        and f"--checksum=sha256:{checksum}" in item
        and item.endswith(" /srv/source-tree/upstream/")
    ]
    return len(matching) == 1


def _check_embedded_source(inputs: Inputs) -> list[Violation]:
    packages = manifest_packages(inputs)
    adds = _remote_adds(inputs.dockerfile)
    violations: list[Violation] = []
    valid_urls: set[str] = set()
    for name, package in packages.items():
        url = package.get("sdist_url")
        checksum = package.get("sdist_sha256")
        member = package.get("source_archive_member")
        if (
            not isinstance(url, str)
            or not isinstance(checksum, str)
            or not _valid_sdist_url(url, member)
            or SHA256_PATTERN.fullmatch(checksum) is None
        ):
            violations.append(Violation("INVALID_SOURCE_ARCHIVE_METADATA", name))
            continue
        valid_urls.add(url)
        if not _archive_add_is_exact(adds, url, checksum):
            violations.append(Violation("SOURCE_ARCHIVE_NOT_EMBEDDED", name))
    remote_sdists = [item for item in adds if "files.pythonhosted.org" in item]
    if len(valid_urls) != 4 or len(remote_sdists) != 4:
        violations.append(Violation("SOURCE_ARCHIVE_SET_DRIFT", "expected four sdists"))
    joined = "\n".join(_docker_instructions(inputs.dockerfile))
    bundle = "tar -czf /srv/stonks-openbb-sidecar-source.tar.gz -C /srv/source-tree ."
    if bundle not in joined:
        violations.append(Violation("MISSING_SOURCE_BUNDLE_BUILD", "tar step missing"))
    return violations


def _license_source_pin(inputs: Inputs) -> tuple[str, str] | None:
    service = mapping(inputs.manifest.get("service"), "service")
    commit = service.get("upstream_commit")
    checksum = service.get("upstream_raw_license_sha256")
    if (
        not isinstance(commit, str)
        or COMMIT_PATTERN.fullmatch(commit) is None
        or not isinstance(checksum, str)
        or SHA256_PATTERN.fullmatch(checksum) is None
    ):
        return None
    return commit, checksum


def _check_license_packaging(inputs: Inputs) -> list[Violation]:
    pin = _license_source_pin(inputs)
    if pin is None:
        return [Violation("INVALID_LICENSE_SOURCE_PIN", "invalid license pin")]
    commit, checksum = pin
    url = f"https://raw.githubusercontent.com/OpenBB-finance/OpenBB/{commit}/LICENSE"
    matching = [
        item
        for item in _remote_adds(inputs.dockerfile)
        if url in item
        and f"--checksum=sha256:{checksum}" in item
        and item.endswith(" /srv/source-tree/OPENBB_LICENSE.txt")
    ]
    if (
        len(matching) == 1
        and "/usr/share/licenses/openbb/AGPL-3.0.txt" in inputs.dockerfile
    ):
        return []
    return [Violation("LICENSE_NOT_CHECKSUM_PACKAGED", "license is not packaged")]


def _check_notices(inputs: Inputs) -> list[Violation]:
    pin = _license_source_pin(inputs)
    commit = pin[0] if pin is not None else ""
    notice_tokens = (
        EXPECTED_LICENSE,
        "GET /source",
        "/usr/share/licenses/openbb/AGPL-3.0.txt",
        "four exact OpenBB source distributions",
    )
    violations: list[Violation] = []
    if any(token not in inputs.notice for token in notice_tokens):
        violations.append(Violation("INCOMPLETE_LEGAL_NOTICE", "NOTICE drifted"))
    offer_tokens = [EXPECTED_LICENSE, "/source", commit, "Dockerfile", "uv.lock"]
    for name, package in manifest_packages(inputs).items():
        offer_tokens.extend(
            (f"{name}=={package.get('version')}", str(package.get("sdist_sha256")))
        )
    if any(token not in inputs.source_offer for token in offer_tokens):
        violations.append(Violation("INCOMPLETE_SOURCE_OFFER", "source offer drifted"))
    root_tokens = (
        "OPENBB-AGPL-3.0-SIDECAR",
        EXPECTED_LICENSE,
        'Link: </source>; rel="source"',
    )
    if any(token not in inputs.third_party_notices for token in root_tokens):
        violations.append(
            Violation("MISSING_OPENBB_DISTRIBUTION_NOTICE", "root notice drifted")
        )
    return violations


def _source_routes(source: str) -> tuple[set[str], Mapping[str, Any], ast.Module]:
    try:
        tree = ast.parse(source, filename="sidecars/openbb/app.py")
    except SyntaxError as error:
        raise PolicyInputError(
            f"cannot parse sidecars/openbb/app.py: {error}"
        ) from error
    routes: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
            ):
                continue
            try:
                route = ast.literal_eval(decorator.args[0])
            except (ValueError, TypeError):
                continue
            if isinstance(route, str):
                routes.add(route)
    return routes, literal_constants(source, "sidecars/openbb/app.py"), tree


def _check_source_route(inputs: Inputs) -> list[Violation]:
    routes, constants, tree = _source_routes(inputs.app)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    valid = (
        {"/source", "/healthz"} <= routes
        and constants.get("SOURCE_LINK")
        == '</source>; rel="source"; type="application/gzip"'
        and "FileResponse" in calls
        and "X-Corresponding-Source" in inputs.app
    )
    violations: list[Violation] = []
    if not valid:
        violations.append(Violation("MISSING_SOURCE_ROUTE", "/source route drifted"))
    required = {
        "Dockerfile",
        "NOTICE.md",
        "README.md",
        "SOURCE_OFFER.md",
        "app.py",
        "provider-manifest.yaml",
        "pyproject.toml",
        "sbom.cdx.json",
        "uv.lock",
    }
    if any(name not in inputs.dockerfile for name in required):
        violations.append(Violation("INCOMPLETE_SOURCE_TREE", "build input omitted"))
    return violations


def container_violations(inputs: Inputs) -> list[Violation]:
    """Check image hardening, embedded sources and network source offer."""

    checks = (
        _check_docker_build,
        _check_compose_hardening,
        _check_embedded_source,
        _check_license_packaging,
        _check_notices,
        _check_source_route,
    )
    return [violation for check in checks for violation in check(inputs)]
