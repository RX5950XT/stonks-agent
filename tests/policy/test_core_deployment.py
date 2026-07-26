from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "infra" / "compose.yaml"
OPTIONAL_COMPOSE = ROOT / "infra" / "compose.optional.yaml"

UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.9.27@"
    "sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a"
)
PYTHON_IMAGE = (
    "python:3.12.13-alpine3.23@"
    "sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d"
)
POSTGRES_IMAGE = (
    "postgres:17.10-alpine@"
    "sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
)
OPTIONAL_PROFILES = frozenset(
    {
        "openbb",
        "tradingagents-paper",
        "tradingagents-backtest",
        "tradingagents-production",
        "kronos-cpu",
        "kronos-cuda",
        "qlib",
        "nautilus",
        "lean",
        "rd-agent",
    }
)
PINNED_IMAGE = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
_COMPOSE_RENDER_TIMEOUT_SECONDS = 60


def _load_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _services(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = manifest["services"]
    assert isinstance(raw, dict)
    services: dict[str, dict[str, object]] = {}
    for name, value in raw.items():
        assert isinstance(name, str)
        assert isinstance(value, dict)
        services[name] = value
    return services


def _instructions(path: Path) -> list[tuple[str, str]]:
    logical_lines: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    assert not pending, "Dockerfile must not end with an incomplete continuation"
    result: list[tuple[str, str]] = []
    for line in logical_lines:
        instruction, separator, argument = line.partition(" ")
        assert separator, f"invalid Dockerfile instruction: {line}"
        result.append((instruction.upper(), argument.strip()))
    return result


def _runtime_instructions() -> list[tuple[str, str]]:
    instructions = _instructions(DOCKERFILE)
    runtime_index = next(
        index
        for index, item in enumerate(instructions)
        if item[0] == "FROM" and item[1].lower().endswith(" as runtime")
    )
    return instructions[runtime_index:]


def _secret_names(service: dict[str, object]) -> set[str]:
    raw = service.get("secrets", [])
    assert isinstance(raw, list)
    names: set[str] = set()
    for value in raw:
        if isinstance(value, str):
            names.add(value)
        else:
            assert isinstance(value, dict)
            source = value.get("source")
            assert isinstance(source, str)
            names.add(source)
    return names


def _environment(service: dict[str, object]) -> dict[str, str]:
    raw = service.get("environment", {})
    assert isinstance(raw, dict)
    assert all(isinstance(key, str) for key in raw)
    assert all(isinstance(value, (str, int)) for value in raw.values())
    return {str(key): str(value) for key, value in raw.items()}


def _assert_hardened_service(
    service: dict[str, object],
    *,
    user: str,
) -> None:
    assert service["user"] == user
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["init"] is True
    assert int(service["pids_limit"]) >= 32
    assert service["mem_limit"]
    assert 0 < float(service["cpus"]) <= 2
    assert service.get("privileged") is not True
    assert service.get("network_mode") != "host"
    for volume in service.get("volumes", []):
        assert "/var/run/docker.sock" not in str(volume)


def _render_environment(tmp_path: Path) -> dict[str, str]:
    owner_secret = tmp_path / "postgres-owner"
    runtime_secret = tmp_path / "postgres-runtime"
    jwks = tmp_path / "service-jwks.json"
    owner_secret.write_text("owner-render-secret", encoding="utf-8")
    runtime_secret.write_text("runtime-render-secret", encoding="utf-8")
    jwks.write_text('{"keys":[]}', encoding="utf-8")
    environment = {
        **os.environ,
        "STONKS_BUILD_REVISION": "deadbeef",
        "STONKS_POSTGRES_PASSWORD_FILE": str(owner_secret),
        "STONKS_RUNTIME_DB_PASSWORD_FILE": str(runtime_secret),
        "STONKS_SERVICE_OIDC_JWKS_HOST_FILE": str(jwks),
    }
    manifests = [COMPOSE, OPTIONAL_COMPOSE, *ROOT.glob("infra/compose.*.yaml")]
    required = re.compile(r"\$\{([A-Z][A-Z0-9_]+):\?[^}]*}")
    for path in manifests:
        for name in required.findall(path.read_text(encoding="utf-8")):
            environment.setdefault(name, "policy-render-value")
    return environment


def _compose_command() -> list[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    return [docker, "compose", "-f", str(COMPOSE)]


def test_core_dockerfile_uses_exact_digest_pinned_multistage_bases() -> None:
    instructions = _instructions(DOCKERFILE)
    from_images = [
        argument.split(maxsplit=1)[0]
        for instruction, argument in instructions
        if instruction == "FROM"
    ]

    assert from_images == [UV_IMAGE, PYTHON_IMAGE, PYTHON_IMAGE]
    assert all(PINNED_IMAGE.fullmatch(image) for image in from_images)
    assert [
        argument.rsplit(" ", maxsplit=1)[-1].lower()
        for instruction, argument in instructions
        if instruction == "FROM"
    ] == [
        "uv",
        "builder",
        "runtime",
    ]


def test_core_dockerfile_builds_frozen_production_environment() -> None:
    instructions = _instructions(DOCKERFILE)
    builder = "\n".join(
        argument for instruction, argument in instructions if instruction == "RUN"
    )

    sync = next(line for line in builder.splitlines() if "uv sync" in line)
    assert {"--frozen", "--no-dev", "--no-editable"} <= set(sync.split())
    assert "--all-groups" not in sync.split()


def test_core_runtime_image_is_nonroot_and_excludes_build_tooling() -> None:
    runtime = _runtime_instructions()
    users = [argument for instruction, argument in runtime if instruction == "USER"]
    entrypoints = [
        argument for instruction, argument in runtime if instruction == "ENTRYPOINT"
    ]
    runtime_runs = [
        argument.lower() for instruction, argument in runtime if instruction == "RUN"
    ]
    runtime_copies = [
        argument.lower() for instruction, argument in runtime if instruction == "COPY"
    ]

    assert users == ["65532:65532"]
    assert entrypoints and "stonks-deploy" in json.loads(entrypoints[-1])
    assert not any("apt-get install" in command for command in runtime_runs)
    assert sum(command.count("apk add") for command in runtime_runs) == 1
    assert any(
        "/sbin/apk add --no-cache libpq=18.4-r0" in command for command in runtime_runs
    )
    assert not any("uv sync" in command for command in runtime_runs)
    assert not any("from=uv" in source for source in runtime_copies)
    assert not any(
        re.search(r"(^|\s)/?(tests|\.research)(/|\s|$)", source)
        for source in runtime_copies
    )
    removal = "\n".join(runtime_runs)
    assert "pip" in removal and "ensurepip" in removal
    assert any(token in removal for token in ("rm ", "uninstall"))
    assert all(instruction != "HEALTHCHECK" for instruction, _ in runtime)


def test_docker_build_context_excludes_non_runtime_content() -> None:
    patterns = {
        line.strip().rstrip("/")
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".git", ".research", ".data", ".venv", "tests"} <= patterns
    assert any(pattern in patterns for pattern in {"**/.venv", ".venv/**"})
    assert any(
        pattern in patterns for pattern in {"**/__pycache__", "**/__pycache__/**"}
    )
    assert not {"src", "packages", "migrations"} & patterns


def test_default_compose_surface_is_exactly_core_and_postgres(
    tmp_path: Path,
) -> None:
    manifest = _load_yaml(COMPOSE)
    services = _services(manifest)

    assert set(services) == {"core", "postgres", "migrate"}
    assert services["migrate"]["profiles"] == ["migration"]
    assert "profiles" not in services["core"]
    assert "profiles" not in services["postgres"]

    result = subprocess.run(
        [*_compose_command(), "config", "--services"],
        cwd=ROOT,
        env=_render_environment(tmp_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=_COMPOSE_RENDER_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == {"core", "postgres"}


def test_postgres_is_pinned_nonroot_readonly_private_and_scram_only() -> None:
    manifest = _load_yaml(COMPOSE)
    postgres = _services(manifest)["postgres"]
    environment = _environment(postgres)

    assert postgres["image"] == POSTGRES_IMAGE
    _assert_hardened_service(postgres, user="70:70")
    assert postgres.get("ports", []) == []
    assert _secret_names(postgres) == {"postgres_owner_password"}
    assert environment["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/postgres_owner_password"
    )
    initdb_args = environment["POSTGRES_INITDB_ARGS"].split()
    assert "--auth-host=scram-sha-256" in initdb_args
    assert "--auth-local=scram-sha-256" in initdb_args
    assert "trust" not in environment["POSTGRES_INITDB_ARGS"].lower()
    assert "/var/run/postgresql" in postgres["tmpfs"]
    assert "/tmp" in postgres["tmpfs"]
    assert any(
        str(volume).endswith(":/var/lib/postgresql/data")
        for volume in postgres["volumes"]
    )


def test_core_is_loopback_nonroot_readonly_bounded_and_readiness_gated() -> None:
    manifest = _load_yaml(COMPOSE)
    core = _services(manifest)["core"]
    environment = _environment(core)
    networks = manifest["networks"]
    assert isinstance(networks, dict)

    _assert_hardened_service(core, user="65532:65532")
    assert core["ports"] == ["127.0.0.1:${STONKS_CORE_PORT:-18000}:8000"]
    assert set(_string_values(core["networks"])) == {
        "core-backend",
        "core-ingress",
    }
    assert networks["core-backend"]["internal"] is True
    assert networks["core-ingress"].get("internal") is not True
    assert _secret_names(core) == {"runtime_db_password"}
    assert environment["STONKS_EXECUTION_MODE"] == "paper"
    assert environment["STONKS_DB_PASSWORD_FILE"] == (
        "/run/secrets/runtime_db_password"
    )
    assert environment["STONKS_DB_USER"] != environment.get("POSTGRES_USER")
    assert core["healthcheck"]["test"] == [
        "CMD",
        "stonks-deploy",
        "probe",
        "--target",
        "ready",
    ]
    assert core["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "/tmp" in core["tmpfs"]


def test_migration_is_explicit_one_shot_with_separated_credentials() -> None:
    manifest = _load_yaml(COMPOSE)
    services = _services(manifest)
    migrate = services["migrate"]
    environment = _environment(migrate)

    _assert_hardened_service(migrate, user="65532:65532")
    assert migrate["profiles"] == ["migration"]
    assert migrate["restart"] == "no"
    assert migrate.get("ports", []) == []
    assert migrate["command"] == ["migrate"]
    assert _secret_names(migrate) == {
        "postgres_owner_password",
        "runtime_db_password",
    }
    assert environment["STONKS_DB_PASSWORD_FILE"] == (
        "/run/secrets/postgres_owner_password"
    )
    assert environment["STONKS_RUNTIME_DB_PASSWORD_FILE"] == (
        "/run/secrets/runtime_db_password"
    )
    assert environment["STONKS_DB_USER"] != environment["STONKS_RUNTIME_DB_USER"]
    assert _secret_names(services["core"]).isdisjoint({"postgres_owner_password"})


def test_compose_forbids_ambient_authority_and_raw_credentials() -> None:
    manifest = _load_yaml(COMPOSE)
    services = _services(manifest)
    secrets = manifest["secrets"]
    assert isinstance(secrets, dict)

    assert secrets == {
        "postgres_owner_password": {
            "file": "${STONKS_POSTGRES_PASSWORD_FILE:?required}"
        },
        "runtime_db_password": {"file": "${STONKS_RUNTIME_DB_PASSWORD_FILE:?required}"},
    }
    for service in services.values():
        _assert_no_raw_credentials(_environment(service))
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        serialized = json.dumps(service, sort_keys=True).lower()
        assert "/var/run/docker.sock" not in serialized
        assert "execution_mode=live" not in serialized
        assert '"stonks_execution_mode": "live"' not in serialized


def _assert_no_raw_credentials(environment: dict[str, str]) -> None:
    for name, value in environment.items():
        normalized = name.upper()
        assert not normalized.endswith(("DATABASE_URL", "DB_URL"))
        if normalized.endswith("PASSWORD"):
            raise AssertionError(f"{name} must use a secret file")
        if normalized.endswith("PASSWORD_FILE"):
            assert value.startswith("/run/secrets/")
        assert not re.search(r"://[^/\s]+:[^@/\s]+@", value)


def test_optional_profiles_are_default_off_and_render_with_core(
    tmp_path: Path,
) -> None:
    optional = _services(_load_yaml(OPTIONAL_COMPOSE))
    profiles = {
        profile
        for service in optional.values()
        for profile in _string_values(service["profiles"])
    }
    assert profiles == OPTIONAL_PROFILES

    command = [
        *_compose_command(),
        "-f",
        str(OPTIONAL_COMPOSE),
    ]
    environment = _render_environment(tmp_path)
    for profile in sorted(OPTIONAL_PROFILES):
        result = subprocess.run(
            [*command, "--profile", profile, "config", "--services"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=_COMPOSE_RENDER_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, f"{profile}: {result.stderr}"
        assert {"core", "postgres"} <= set(result.stdout.split())
        assert any(
            profile in _string_values(optional_service["profiles"])
            and optional_name in result.stdout.split()
            for optional_name, optional_service in optional.items()
        )

    core_dependencies = _services(_load_yaml(COMPOSE))["core"].get("depends_on", {})
    assert isinstance(core_dependencies, (dict, list))
    assert not set(optional) & set(core_dependencies)


def _string_values(value: object) -> Iterable[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return (item for item in value if isinstance(item, str))
