from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, ClassVar

import jwt
import pytest
import yaml
from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecars" / "openbb"
EXPECTED_ORIGIN = "http://127.0.0.1:6900"


def _load_smoke_module() -> object:
    spec = spec_from_file_location(
        "smoke_openbb_under_test", ROOT / "scripts" / "smoke_openbb.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_smoke_auth_module() -> Any:
    spec = spec_from_file_location(
        "prepare_openbb_smoke_auth_under_test",
        ROOT / "scripts" / "prepare_openbb_smoke_auth.py",
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, object]:
    loaded = yaml.safe_load(
        (SIDECAR / "provider-manifest.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_openbb_packages_are_exact_and_have_embedded_source() -> None:
    smoke_openbb = _load_smoke_module()
    manifest = _manifest()
    packages = manifest["packages"]
    assert isinstance(packages, list)
    expected = {
        "openbb-core": "1.6.13",
        "openbb-equity": "1.6.2",
        "openbb-platform-api": "1.3.6",
        "openbb-yfinance": "1.6.3",
    }
    assert {item["name"]: item["version"] for item in packages} == expected

    dockerfile = (SIDECAR / "Dockerfile").read_text(encoding="utf-8")
    for item in packages:
        member = str(item["source_archive_member"])
        filename = member.removeprefix("upstream/")
        checksum = str(item["sdist_sha256"])
        assert len(checksum) == 64
        assert f"--checksum=sha256:{checksum}" in dockerfile
        assert filename in str(item["sdist_url"])
        assert str(item["sdist_url"]) in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "uv run --frozen openbb-build" in dockerfile
    project = tomllib.loads((SIDECAR / "pyproject.toml").read_text(encoding="utf-8"))
    assert "stonks-service-auth" in project["project"]["dependencies"]
    assert project["tool"]["uv"]["sources"]["stonks-service-auth"] == {
        "path": "../../packages/service-auth"
    }
    assert "COPY packages/service-auth" in dockerfile
    assert "/srv/source-tree/packages/service-auth" in dockerfile
    assert "Dockerfile.dockerignore" in smoke_openbb.REQUIRED_SOURCE_MEMBERS
    assert (
        "COPY sidecars/openbb/Dockerfile sidecars/openbb/Dockerfile.dockerignore"
        in dockerfile
    )
    for token in (
        "find . -type f -print0",
        "sort -z",
        "tar --sort=name --no-recursion",
        "--mtime=@0 --owner=0 --group=0 --numeric-owner",
        "--mode=u=rw,go=r --null",
    ):
        assert token in dockerfile


def test_openbb_build_context_has_a_dedicated_allowlist() -> None:
    content = (SIDECAR / "Dockerfile.dockerignore").read_text(encoding="utf-8")

    assert set(content.splitlines()) == {
        "**",
        "!LICENSE",
        "!sidecars/",
        "!sidecars/openbb/",
        "!sidecars/openbb/Dockerfile",
        "!sidecars/openbb/Dockerfile.dockerignore",
        "!sidecars/openbb/NOTICE.md",
        "!sidecars/openbb/README.md",
        "!sidecars/openbb/SOURCE_OFFER.md",
        "!sidecars/openbb/app.py",
        "!sidecars/openbb/license-policy.yaml",
        "!sidecars/openbb/provider-manifest.yaml",
        "!sidecars/openbb/pyproject.toml",
        "!sidecars/openbb/sbom.cdx.json",
        "!sidecars/openbb/surface.py",
        "!sidecars/openbb/uv.lock",
        "!packages/",
        "!packages/service-auth/",
        "!packages/service-auth/pyproject.toml",
        "!packages/service-auth/src/",
        "!packages/service-auth/src/stonks_service_auth/",
        "!packages/service-auth/src/stonks_service_auth/__init__.py",
        "!packages/service-auth/src/stonks_service_auth/admission.py",
        "!packages/service-auth/src/stonks_service_auth/authorization.py",
        "!packages/service-auth/src/stonks_service_auth/environment.py",
        "!packages/service-auth/src/stonks_service_auth/headers.py",
        "!packages/service-auth/src/stonks_service_auth/oidc.py",
        "!packages/service-auth/src/stonks_service_auth/py.typed",
        "!packages/service-auth/src/stonks_service_auth/request_body.py",
        "!packages/service-auth/src/stonks_service_auth/source_identity.py",
        "**/__pycache__/",
        "**/__pycache__/**",
        "**/*.py[cod]",
    }


def test_lock_and_sbom_match_source_manifest() -> None:
    manifest = _manifest()
    packages = manifest["packages"]
    assert isinstance(packages, list)
    lock = tomllib.loads((SIDECAR / "uv.lock").read_text(encoding="utf-8"))
    locked = {item["name"]: item["version"] for item in lock["package"]}
    sbom = json.loads((SIDECAR / "sbom.cdx.json").read_text(encoding="utf-8"))
    components = {item["name"]: item for item in sbom["components"]}

    for item in packages:
        name = item["name"]
        assert locked[name] == item["version"]
        assert components[name]["version"] == item["version"]
        assert components[name]["hashes"] == [
            {"alg": "SHA-256", "content": item["sdist_sha256"]}
        ]


def test_sbom_components_have_one_auditable_spdx_license_value() -> None:
    sbom = json.loads((SIDECAR / "sbom.cdx.json").read_text(encoding="utf-8"))
    invalid: list[str] = []
    for component in sbom["components"]:
        licenses = component.get("licenses")
        if not isinstance(licenses, list) or len(licenses) != 1:
            invalid.append(component["name"])
            continue
        entry = licenses[0]
        license_id = entry.get("license", {}).get("id")
        expression = entry.get("expression")
        if not (
            (isinstance(license_id, str) and license_id)
            or (isinstance(expression, str) and expression)
        ):
            invalid.append(component["name"])

    assert invalid == []


def test_license_policy_exactly_covers_sbom_components() -> None:
    sbom = json.loads((SIDECAR / "sbom.cdx.json").read_text(encoding="utf-8"))
    policy = yaml.safe_load(
        (SIDECAR / "license-policy.yaml").read_text(encoding="utf-8")
    )

    assert set(policy["components"]) == {
        component["name"].lower().replace("_", "-") for component in sbom["components"]
    }
    assert set(policy["reviewed_components"]) == {
        "openbb-core",
        "openbb-equity",
        "openbb-platform-api",
        "openbb-yfinance",
    }


def test_transport_is_consistent_and_runtime_is_immutable() -> None:
    manifest = _manifest()
    assert manifest["transport"]["canonical_origin"] == EXPECTED_ORIGIN
    assert manifest["rest_policy"]["origin"] == EXPECTED_ORIGIN
    assert manifest["service"]["runtime_auto_build"] is False

    policy = yaml.safe_load(
        (ROOT / "config" / "providers" / "default.yaml").read_text(encoding="utf-8")
    )
    openbb_routes = [
        route
        for item in policy["policies"]
        for route in item["routes"]
        if route["provider"] == "openbb_rest"
    ]
    assert openbb_routes
    assert {route["origin"] for route in openbb_routes} == {EXPECTED_ORIGIN}

    compose = yaml.safe_load(
        (ROOT / "infra" / "compose.openbb.yaml").read_text(encoding="utf-8")
    )
    service = compose["services"]["openbb"]
    assert service["ports"] == ["127.0.0.1:6900:6900"]
    assert service["read_only"] is True
    assert service["user"] == "65532:65532"
    assert service["cap_drop"] == ["ALL"]
    assert service["environment"]["OPENBB_AUTO_BUILD"] == "false"
    assert service["build"] == {
        "context": "..",
        "dockerfile": "sidecars/openbb/Dockerfile",
    }
    assert service["environment"]["STONKS_SERVICE_OIDC_JWKS_FILE"] == (
        "/run/secrets/stonks-service-jwks.json"
    )
    assert any(
        value.endswith(":/run/secrets/stonks-service-jwks.json:ro")
        for value in service["volumes"]
    )
    assert "healthz" in " ".join(service["healthcheck"]["test"])
    assert service["labels"]["stonks.transport.canonical-origin"] == EXPECTED_ORIGIN
    assert service["labels"]["stonks.transport.rest-endpoint"] == (
        "/api/v1/equity/price/historical"
    )
    assert service["labels"]["stonks.transport.provider"] == "yfinance"


def test_source_offer_and_notice_are_packaged() -> None:
    dockerfile = (SIDECAR / "Dockerfile").read_text(encoding="utf-8")
    required = {
        "Dockerfile",
        "NOTICE.md",
        "OPENBB_LICENSE.txt",
        "README.md",
        "SOURCE_OFFER.md",
        "license-policy.yaml",
        "provider-manifest.yaml",
        "pyproject.toml",
        "sbom.cdx.json",
        "uv.lock",
        "packages/service-auth",
    }
    for name in required:
        assert name in dockerfile
    assert "/srv/stonks-openbb-sidecar-source.tar.gz" in dockerfile
    assert 'Link: </source>; rel="source"' in (
        ROOT / "THIRD_PARTY_NOTICES.md"
    ).read_text(encoding="utf-8")


def test_ci_verifies_deployed_license_policy_source_member() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    source_member_loop = workflow.split("for member in", maxsplit=1)[1].split(
        "; do", maxsplit=1
    )[0]
    assert "license-policy.yaml" in source_member_loop


def test_sidecar_exports_only_the_allowlisted_asgi_surface() -> None:
    app_source = (SIDECAR / "app.py").read_text(encoding="utf-8")
    dockerfile = (SIDECAR / "Dockerfile").read_text(encoding="utf-8")

    isolation = "validate_isolated_runtime_environment(os.environ)"
    authentication = "load_static_oidc_service_authenticator(os.environ)"
    upstream = 'importlib.import_module("openbb_core.api.rest_api")'
    assert app_source.index(isolation) < app_source.index(authentication)
    assert app_source.index(authentication) < app_source.index(upstream)
    assert "app = build_surface(" in app_source
    assert "authenticator=" in app_source
    assert "surface.py" in dockerfile


def test_ci_audits_frozen_sidecar_lock_and_runs_live_smoke() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "uv export --project sidecars/openbb" in workflow
    assert "--no-emit-project --no-emit-local --format requirements.txt" in workflow
    assert "uv run pip-audit --strict --requirement" in workflow
    assert "uv run python scripts/smoke_openbb.py" in workflow
    assert workflow.index("Start healthy optional sidecar") < workflow.index(
        "uv run python scripts/smoke_openbb.py"
    )


def test_ephemeral_smoke_identity_is_bound_to_the_exact_adapter_query() -> None:
    module = _load_smoke_auth_module()
    _jwks, token = module._material()
    claims = jwt.decode(token, options={"verify_signature": False})
    payload = {
        "method": "GET",
        "path": "/api/v1/equity/price/historical",
        "query": {
            "end_date": "2024-01-03",
            "provider": "yfinance",
            "start_date": "2024-01-02",
            "symbol": "AAPL",
        },
    }
    expected_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert claims["stonks_permission"] == "dispatch_assigned_market_data"
    assert claims["stonks_attempt_generation"] == 0
    assert claims["stonks_attempt_nonce_hash"] == expected_hash
    assert claims["stonks_request_hash"] == expected_hash
    assert claims["stonks_targets"] == ["market:US/AAPL"]


def _source_archive(
    smoke_openbb: Any,
    packages: list[dict[str, str]],
    *,
    omit: str | None = None,
    license_body: bytes = b"upstream license",
) -> bytes:
    tar_buffer = io.BytesIO()
    package_bodies = {
        package["source_archive_member"]: f"source:{index}".encode()
        for index, package in enumerate(packages)
    }
    bodies = {
        **{name: b"required" for name in smoke_openbb.REQUIRED_SOURCE_MEMBERS},
        **package_bodies,
        "OPENBB_LICENSE.txt": license_body,
    }
    if omit is not None:
        bodies.pop(omit)
    with tarfile.open(fileobj=tar_buffer, mode="w:") as bundle:
        for name, body in sorted(bodies.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))
    return gzip.compress(tar_buffer.getvalue(), mtime=0)


def _tar_with_members(
    members: list[tuple[tarfile.TarInfo, bytes]],
) -> tarfile.TarFile:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as bundle:
        for info, body in members:
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body) if info.isfile() else None)
    return tarfile.open(fileobj=io.BytesIO(buffer.getvalue()), mode="r:")


@pytest.mark.parametrize(
    "unsafe",
    [
        tarfile.TarInfo("../escape"),
        tarfile.TarInfo("folder\\escape"),
        tarfile.TarInfo("/absolute"),
        tarfile.TarInfo("NOTICE.md"),
    ],
)
def test_source_archive_rejects_unsafe_or_case_colliding_members(
    unsafe: tarfile.TarInfo,
) -> None:
    smoke_openbb = _load_smoke_module()
    safe = tarfile.TarInfo("notice.md")
    with (
        _tar_with_members([(safe, b"safe"), (unsafe, b"unsafe")]) as bundle,
        pytest.raises(ValueError, match="unsafe member"),
    ):
        smoke_openbb._archive_members(bundle)  # type: ignore[attr-defined]


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_source_archive_rejects_links(member_type: bytes) -> None:
    smoke_openbb = _load_smoke_module()
    linked = tarfile.TarInfo("linked")
    linked.type = member_type
    linked.linkname = "NOTICE.md"
    with (
        _tar_with_members([(linked, b"")]) as bundle,
        pytest.raises(ValueError, match="unsafe member"),
    ):
        smoke_openbb._archive_members(bundle)  # type: ignore[attr-defined]


def test_source_archive_rejects_total_expanded_size(
    monkeypatch: MonkeyPatch,
) -> None:
    smoke_openbb = _load_smoke_module()
    monkeypatch.setattr(smoke_openbb, "MAX_SOURCE_EXPANDED_BYTES", 7)
    first = tarfile.TarInfo("a")
    second = tarfile.TarInfo("b")
    with (
        _tar_with_members([(first, b"1234"), (second, b"5678")]) as bundle,
        pytest.raises(ValueError, match="expanded size"),
    ):
        smoke_openbb._archive_members(bundle)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mtime", 1),
        ("uid", 1),
        ("gid", 1),
        ("mode", 0o755),
    ],
)
def test_source_archive_rejects_nondeterministic_metadata(
    field: str,
    value: int,
) -> None:
    smoke_openbb = _load_smoke_module()
    info = tarfile.TarInfo("source.txt")
    setattr(info, field, value)
    with (
        _tar_with_members([(info, b"source")]) as bundle,
        pytest.raises(ValueError, match="nondeterministic metadata"),
    ):
        smoke_openbb._archive_members(bundle)  # type: ignore[attr-defined]


def test_source_archive_rejects_nondeterministic_member_order() -> None:
    smoke_openbb = _load_smoke_module()
    later = tarfile.TarInfo("z")
    earlier = tarfile.TarInfo("a")
    with (
        _tar_with_members([(later, b"later"), (earlier, b"earlier")]) as bundle,
        pytest.raises(ValueError, match="member order"),
    ):
        smoke_openbb._archive_members(bundle)  # type: ignore[attr-defined]


@pytest.mark.parametrize("flag", [0x02, 0x04, 0x08, 0x10, 0x20])
def test_source_archive_rejects_nondeterministic_gzip_header(flag: int) -> None:
    smoke_openbb = _load_smoke_module()
    archive = bytearray(gzip.compress(b"payload", mtime=0))
    archive[3] = flag

    with pytest.raises(ValueError, match="gzip header"):
        smoke_openbb._validate_gzip_header(bytes(archive))  # type: ignore[attr-defined]


def test_source_archive_rejects_nondeterministic_gzip_timestamp() -> None:
    smoke_openbb = _load_smoke_module()
    archive = gzip.compress(b"payload", mtime=1)

    with pytest.raises(ValueError, match="gzip header"):
        smoke_openbb._validate_gzip_header(archive)  # type: ignore[attr-defined]


def test_smoke_verifies_all_source_and_license_hashes(
    monkeypatch: MonkeyPatch,
) -> None:
    smoke_openbb = _load_smoke_module()
    packages = [
        {
            "source_archive_member": f"upstream/package-{index}.tar.gz",
            "sdist_sha256": hashlib.sha256(f"source:{index}".encode()).hexdigest(),
        }
        for index in range(4)
    ]
    license_body = b"upstream license"
    manifest = {
        "packages": packages,
        "service": {
            "upstream_raw_license_sha256": hashlib.sha256(license_body).hexdigest()
        },
    }
    archive = _source_archive(
        smoke_openbb,
        packages,
        license_body=license_body,
    )
    monkeypatch.setattr(smoke_openbb, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(smoke_openbb, "SOURCE_MEMBER_PATHS", {})
    monkeypatch.setattr(
        smoke_openbb,
        "_get",
        lambda *_args, **_kwargs: (
            archive,
            {"link": '</source>; rel="source"'},
        ),
    )

    verified = smoke_openbb._verify_source_archive(1)  # type: ignore[attr-defined]

    assert len(verified) == 5
    assert (
        verified["OPENBB_LICENSE.txt"]
        == manifest["service"]["upstream_raw_license_sha256"]
    )


def test_smoke_rejects_missing_governance_source_member(
    monkeypatch: MonkeyPatch,
) -> None:
    smoke_openbb = _load_smoke_module()
    packages = [
        {
            "source_archive_member": f"upstream/package-{index}.tar.gz",
            "sdist_sha256": hashlib.sha256(f"source:{index}".encode()).hexdigest(),
        }
        for index in range(4)
    ]
    license_body = b"upstream license"
    manifest = {
        "packages": packages,
        "service": {
            "upstream_raw_license_sha256": hashlib.sha256(license_body).hexdigest()
        },
    }
    archive = _source_archive(
        smoke_openbb,
        packages,
        omit="license-policy.yaml",
        license_body=license_body,
    )
    monkeypatch.setattr(smoke_openbb, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(smoke_openbb, "SOURCE_MEMBER_PATHS", {})
    monkeypatch.setattr(
        smoke_openbb,
        "_get",
        lambda *_args, **_kwargs: (
            archive,
            {"link": '</source>; rel="source"'},
        ),
    )

    with pytest.raises(ValueError, match=r"license-policy\.yaml"):
        smoke_openbb._verify_source_archive(1)  # type: ignore[attr-defined]


def test_smoke_rejects_upstream_license_hash_drift(
    monkeypatch: MonkeyPatch,
) -> None:
    smoke_openbb = _load_smoke_module()
    packages = [
        {
            "source_archive_member": f"upstream/package-{index}.tar.gz",
            "sdist_sha256": hashlib.sha256(f"source:{index}".encode()).hexdigest(),
        }
        for index in range(4)
    ]
    manifest = {
        "packages": packages,
        "service": {"upstream_raw_license_sha256": "0" * 64},
    }
    archive = _source_archive(smoke_openbb, packages)
    monkeypatch.setattr(smoke_openbb, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(smoke_openbb, "SOURCE_MEMBER_PATHS", {})
    monkeypatch.setattr(
        smoke_openbb,
        "_get",
        lambda *_args, **_kwargs: (
            archive,
            {"link": '</source>; rel="source"'},
        ),
    )

    with pytest.raises(ValueError, match="license hash mismatch"):
        smoke_openbb._verify_source_archive(1)  # type: ignore[attr-defined]


def test_smoke_normalizes_http_header_names(monkeypatch: MonkeyPatch) -> None:
    smoke_openbb = _load_smoke_module()

    class FakeResponse:
        headers: ClassVar[dict[str, str]] = {
            "link": '</source>; rel="source"',
            "content-length": "2",
        }

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return b"{}"

    monkeypatch.setattr(
        smoke_openbb,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    body, headers = smoke_openbb._get("/healthz", timeout=1)  # type: ignore[attr-defined]

    assert body == b"{}"
    assert headers["link"] == '</source>; rel="source"'
