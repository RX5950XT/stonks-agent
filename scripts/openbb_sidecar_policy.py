"""Parsed inputs and contract checks for the optional OpenBB sidecar."""

from __future__ import annotations

import ast
import ipaddress
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlsplit

import yaml

EXPECTED_PINS: Final = {
    "openbb-core": "1.6.13",
    "openbb-equity": "1.6.2",
    "openbb-platform-api": "1.3.6",
    "openbb-yfinance": "1.6.3",
}
EXPECTED_ORIGIN: Final = "http://127.0.0.1:6900"
EXPECTED_ENDPOINT: Final = "/api/v1/equity/price/historical"
EXPECTED_PROVIDER: Final = "yfinance"
EXPECTED_ADAPTER: Final = "openbb_rest"
EXPECTED_SERVICE: Final = "stonks-openbb-sidecar"
EXPECTED_LICENSE: Final = "AGPL-3.0-only"
EXPECTED_MARKETS: Final = frozenset({"US", "HK", "TW"})
EXPECTED_CHECKS: Final = (
    "exact-pins",
    "core-isolation",
    "transport-contract",
    "cyclonedx-sbom",
    "container-hardening",
    "embedded-source",
    "license-source-offer",
    "runtime-source-route",
)
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
DEPENDENCY_PATTERN: Final = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass(frozen=True, slots=True)
class Violation:
    """Stable machine-readable sidecar policy violation."""

    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class Inputs:
    """Parsed repository inputs required by the policy checks."""

    root: Path
    core_project: Mapping[str, Any]
    core_lock: Mapping[str, Any]
    sidecar_project: Mapping[str, Any]
    sidecar_lock: Mapping[str, Any]
    manifest: Mapping[str, Any]
    sbom: Mapping[str, Any]
    compose: Mapping[str, Any]
    provider_config: Mapping[str, Any]
    adapter: str
    dockerfile: str
    app: str
    notice: str
    source_offer: str
    third_party_notices: str


class PolicyInputError(ValueError):
    """A required policy input is absent or malformed."""


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().lower())


def mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyInputError(f"{label} must be a mapping")
    return value


def sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PolicyInputError(f"{label} must be a sequence")
    return cast(Sequence[Any], value)


def _read_text(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PolicyInputError(f"cannot read {relative}: {error}") from error


def _load_toml(root: Path, relative: str) -> Mapping[str, Any]:
    try:
        with (root / relative).open("rb") as handle:
            return mapping(tomllib.load(handle), relative)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PolicyInputError(f"cannot parse {relative}: {error}") from error


def _load_yaml(root: Path, relative: str) -> Mapping[str, Any]:
    try:
        return mapping(yaml.safe_load(_read_text(root, relative)), relative)
    except yaml.YAMLError as error:
        raise PolicyInputError(f"cannot parse {relative}: {error}") from error


def _load_json(root: Path, relative: str) -> Mapping[str, Any]:
    try:
        return mapping(json.loads(_read_text(root, relative)), relative)
    except json.JSONDecodeError as error:
        raise PolicyInputError(f"cannot parse {relative}: {error}") from error


def load_inputs(root: Path) -> Inputs:
    """Load every required file before evaluating any policy."""

    resolved = root.resolve()
    if not resolved.is_dir():
        raise PolicyInputError(f"repository root is not a directory: {resolved}")
    return Inputs(
        root=resolved,
        core_project=_load_toml(resolved, "pyproject.toml"),
        core_lock=_load_toml(resolved, "uv.lock"),
        sidecar_project=_load_toml(resolved, "sidecars/openbb/pyproject.toml"),
        sidecar_lock=_load_toml(resolved, "sidecars/openbb/uv.lock"),
        manifest=_load_yaml(resolved, "sidecars/openbb/provider-manifest.yaml"),
        sbom=_load_json(resolved, "sidecars/openbb/sbom.cdx.json"),
        compose=_load_yaml(resolved, "infra/compose.openbb.yaml"),
        provider_config=_load_yaml(resolved, "config/providers/default.yaml"),
        adapter=_read_text(
            resolved, "src/stonks_agent/adapters/market_data/openbb_rest.py"
        ),
        dockerfile=_read_text(resolved, "sidecars/openbb/Dockerfile"),
        app=_read_text(resolved, "sidecars/openbb/app.py"),
        notice=_read_text(resolved, "sidecars/openbb/NOTICE.md"),
        source_offer=_read_text(resolved, "sidecars/openbb/SOURCE_OFFER.md"),
        third_party_notices=_read_text(resolved, "THIRD_PARTY_NOTICES.md"),
    )


def dependency_name(requirement: Any) -> str | None:
    if not isinstance(requirement, str):
        return None
    match = DEPENDENCY_PATTERN.match(requirement)
    return normalize_name(match.group(1)) if match else None


def locked_packages(lock: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    packages = sequence(lock.get("package"), "uv.lock.package")
    return [
        mapping(package, f"uv.lock.package[{index}]")
        for index, package in enumerate(packages)
    ]


def package_index(
    packages: Sequence[Mapping[str, Any]], label: str
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for package in packages:
        raw_name = package.get("name")
        if not isinstance(raw_name, str):
            raise PolicyInputError(f"{label} package name must be a string")
        name = normalize_name(raw_name)
        if name in indexed:
            raise PolicyInputError(f"{label} contains duplicate package {name}")
        indexed[name] = package
    return indexed


def manifest_packages(inputs: Inputs) -> dict[str, Mapping[str, Any]]:
    packages = sequence(inputs.manifest.get("packages"), "manifest.packages")
    parsed = [
        mapping(package, f"manifest.packages[{index}]")
        for index, package in enumerate(packages)
    ]
    return package_index(parsed, "provider manifest")


def literal_constants(source: str, path: str) -> Mapping[str, Any]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise PolicyInputError(f"cannot parse {path}: {error}") from error
    constants: dict[str, Any] = {}
    for node in tree.body:
        name: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        if name is None or value is None:
            continue
        try:
            constants[name] = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue
    return constants


def compose_service(inputs: Inputs) -> Mapping[str, Any]:
    services = mapping(inputs.compose.get("services"), "compose.services")
    return mapping(services.get("openbb"), "compose.services.openbb")


def _exact_requirement(requirement: Any) -> tuple[str, str] | None:
    if not isinstance(requirement, str):
        return None
    match = re.fullmatch(
        r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+_-]*)\s*",
        requirement,
    )
    if match is None:
        return None
    return normalize_name(match.group(1)), match.group(2)


def _check_project_pins(inputs: Inputs) -> list[Violation]:
    project = mapping(inputs.sidecar_project.get("project"), "sidecar.project")
    tool = mapping(inputs.sidecar_project.get("tool"), "sidecar.tool")
    uv_config = mapping(tool.get("uv"), "sidecar.tool.uv")
    violations: list[Violation] = []
    if project.get("name") != EXPECTED_SERVICE:
        violations.append(Violation("SIDECAR_IDENTITY_DRIFT", "name drifted"))
    if project.get("license") != EXPECTED_LICENSE:
        violations.append(Violation("SIDECAR_LICENSE_DRIFT", "license drifted"))
    if uv_config.get("package") is not False:
        violations.append(
            Violation("SIDECAR_LOCK_NOT_ISOLATED", "package must be false")
        )
    dependencies = sequence(project.get("dependencies"), "sidecar dependencies")
    parsed = [_exact_requirement(item) for item in dependencies]
    direct = {item[0]: item[1] for item in parsed if item is not None}
    if len(parsed) != 4 or None in parsed or direct != EXPECTED_PINS:
        violations.append(
            Violation("OPENBB_PIN_DRIFT", "exact approved pins are required")
        )
    return violations


def _check_lock_and_manifest_pins(inputs: Inputs) -> list[Violation]:
    locked = package_index(locked_packages(inputs.sidecar_lock), "sidecar lock")
    lock_pins = {
        name: package.get("version")
        for name, package in locked.items()
        if name == "openbb" or name.startswith("openbb-")
    }
    manifest = manifest_packages(inputs)
    manifest_pins = {name: package.get("version") for name, package in manifest.items()}
    violations: list[Violation] = []
    if lock_pins != EXPECTED_PINS:
        violations.append(Violation("OPENBB_LOCK_DRIFT", "lock pins drifted"))
    if manifest_pins != EXPECTED_PINS:
        violations.append(
            Violation("OPENBB_MANIFEST_PIN_DRIFT", "manifest pins drifted")
        )
    return violations


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [item for nested in value.values() for item in _walk_strings(nested)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for nested in value for item in _walk_strings(nested)]
    return []


def _check_core_isolation(inputs: Inputs) -> list[Violation]:
    sections = {
        "project": inputs.core_project.get("project"),
        "dependency-groups": inputs.core_project.get("dependency-groups"),
    }
    direct = {
        name
        for item in _walk_strings(sections)
        if (name := dependency_name(item)) is not None
        and (name == "openbb" or name.startswith("openbb-"))
    }
    locked = package_index(locked_packages(inputs.core_lock), "core lock")
    transitive = {
        name for name in locked if name == "openbb" or name.startswith("openbb-")
    }
    return [
        *[
            Violation("OPENBB_IN_CORE_PROJECT", f"forbidden dependency: {name}")
            for name in sorted(direct)
        ],
        *[
            Violation("OPENBB_IN_CORE_LOCK", f"forbidden lock package: {name}")
            for name in sorted(transitive)
        ],
    ]


def _is_loopback_origin(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _compose_labels(service: Mapping[str, Any]) -> Mapping[str, str]:
    raw = service.get("labels")
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    labels: dict[str, str] = {}
    for value in sequence(raw, "compose labels"):
        if not isinstance(value, str) or "=" not in value:
            raise PolicyInputError("compose labels must use key=value")
        key, label = value.split("=", maxsplit=1)
        labels[key] = label
    return labels


def _openbb_routes(inputs: Inputs) -> list[tuple[str, Mapping[str, Any]]]:
    policies = sequence(inputs.provider_config.get("policies"), "provider policies")
    routes: list[tuple[str, Mapping[str, Any]]] = []
    for index, raw_policy in enumerate(policies):
        policy = mapping(raw_policy, f"provider policies[{index}]")
        market = policy.get("market")
        if not isinstance(market, str):
            raise PolicyInputError("provider market must be a string")
        for route_index, raw_route in enumerate(
            sequence(policy.get("routes"), "routes")
        ):
            route = mapping(raw_route, f"{market} routes[{route_index}]")
            if route.get("provider") == EXPECTED_ADAPTER:
                routes.append((market, route))
    return routes


def _transport_is_consistent(inputs: Inputs) -> bool:
    transport = mapping(inputs.manifest.get("transport"), "transport")
    rest = mapping(inputs.manifest.get("rest_policy"), "rest_policy")
    service_policy = mapping(inputs.manifest.get("service"), "service")
    constants = literal_constants(inputs.adapter, "openbb_rest.py")
    labels = _compose_labels(compose_service(inputs))
    expected_labels = {
        "stonks.transport.canonical-origin": EXPECTED_ORIGIN,
        "stonks.transport.rest-endpoint": EXPECTED_ENDPOINT,
        "stonks.transport.provider": EXPECTED_PROVIDER,
        "stonks.transport.plaintext-scope": "loopback-only",
    }
    values = (
        transport.get("canonical_origin") == EXPECTED_ORIGIN,
        rest.get("origin") == EXPECTED_ORIGIN,
        rest.get("endpoint") == EXPECTED_ENDPOINT,
        rest.get("provider") == EXPECTED_PROVIDER,
        transport.get("plaintext_scope") == "loopback-only",
        rest.get("methods") == ["GET"],
        rest.get("redirect_policy") == "deny",
        rest.get("arbitrary_url_policy") == "deny",
        service_policy.get("name") == EXPECTED_SERVICE,
        service_policy.get("runtime_auto_build") is False,
        constants.get("OPENBB_ORIGIN") == EXPECTED_ORIGIN,
        constants.get("OPENBB_HISTORICAL_ENDPOINT") == EXPECTED_ENDPOINT,
        constants.get("OPENBB_PROVIDER") == EXPECTED_PROVIDER,
        all(labels.get(key) == value for key, value in expected_labels.items()),
    )
    routes = _openbb_routes(inputs)
    route_values = all(
        route.get("origin") == EXPECTED_ORIGIN
        and route.get("endpoints") == [EXPECTED_ENDPOINT]
        for _, route in routes
    )
    return (
        all(values)
        and {market for market, _ in routes} == EXPECTED_MARKETS
        and route_values
    )


def _check_transport(inputs: Inputs) -> list[Violation]:
    rest = mapping(inputs.manifest.get("rest_policy"), "rest_policy")
    violations: list[Violation] = []
    if not _transport_is_consistent(inputs):
        violations.append(
            Violation("TRANSPORT_POLICY_DRIFT", "transport contract is inconsistent")
        )
    if not _is_loopback_origin(rest.get("origin")):
        violations.append(Violation("NON_LOOPBACK_ORIGIN", "origin is not loopback"))
    return violations


def _sbom_components(inputs: Inputs) -> dict[str, Mapping[str, Any]]:
    raw = sequence(inputs.sbom.get("components"), "SBOM components")
    components = [
        mapping(component, f"SBOM components[{index}]")
        for index, component in enumerate(raw)
    ]
    return package_index(components, "SBOM")


def _check_sbom(inputs: Inputs) -> list[Violation]:
    violations: list[Violation] = []
    if (
        inputs.sbom.get("bomFormat") != "CycloneDX"
        or inputs.sbom.get("specVersion") != "1.6"
        or inputs.sbom.get("version") != 1
    ):
        violations.append(Violation("INVALID_CYCLONEDX_SBOM", "invalid SBOM"))
    components = _sbom_components(inputs)
    for package in locked_packages(inputs.sidecar_lock):
        source = package.get("source")
        if isinstance(source, Mapping) and "virtual" in source:
            continue
        name = normalize_name(str(package.get("name")))
        component = components.get(name)
        if component is None:
            violations.append(
                Violation("SBOM_MISSING_LOCK_COMPONENT", f"missing {name}")
            )
        elif component.get("version") != package.get("version"):
            violations.append(Violation("SBOM_VERSION_DRIFT", f"drift for {name}"))
    manifest = manifest_packages(inputs)
    for name, version in EXPECTED_PINS.items():
        component = components.get(name)
        hashes = component.get("hashes") if component else None
        actual = {
            str(item.get("content"))
            for item in hashes or ()
            if isinstance(item, Mapping)
        }
        if (
            component is None
            or component.get("version") != version
            or actual != {manifest[name].get("sdist_sha256")}
        ):
            violations.append(Violation("OPENBB_SBOM_PIN_DRIFT", f"drift for {name}"))
    return violations


def contract_violations(inputs: Inputs) -> list[Violation]:
    """Check pins, core isolation, transport and SBOM completeness."""

    checks = (
        _check_project_pins,
        _check_lock_and_manifest_pins,
        _check_core_isolation,
        _check_transport,
        _check_sbom,
    )
    return [violation for check in checks for violation in check(inputs)]
