from __future__ import annotations

import json
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import ClassVar

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


def _manifest() -> dict[str, object]:
    loaded = yaml.safe_load(
        (SIDECAR / "provider-manifest.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_openbb_packages_are_exact_and_have_embedded_source() -> None:
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


def test_transport_is_consistent_and_runtime_is_immutable() -> None:
    manifest = _manifest()
    assert manifest["transport"]["canonical_origin"] == EXPECTED_ORIGIN
    assert manifest["rest_policy"]["origin"] == EXPECTED_ORIGIN
    assert manifest["service"]["runtime_auto_build"] is False

    policy = yaml.safe_load(
        (ROOT / "config" / "providers" / "default.yaml").read_text(
            encoding="utf-8"
        )
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
        "provider-manifest.yaml",
        "pyproject.toml",
        "sbom.cdx.json",
        "uv.lock",
    }
    for name in required:
        assert name in dockerfile
    assert "/srv/stonks-openbb-sidecar-source.tar.gz" in dockerfile
    assert 'Link: </source>; rel="source"' in (
        ROOT / "THIRD_PARTY_NOTICES.md"
    ).read_text(encoding="utf-8")


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
