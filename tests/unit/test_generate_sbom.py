from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "generate_sbom_under_test",
    ROOT / "scripts" / "generate_sbom.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SbomError = MODULE.SbomError
build_syft_command: Any = MODULE.build_syft_command
normalize_sbom: Any = MODULE.normalize_sbom
validate_image_reference: Any = MODULE.validate_image_reference
write_normalized_sbom: Any = MODULE.write_normalized_sbom

IMAGE = "ghcr.io/acme/stonks-agent@sha256:" + ("a" * 64)
SYFT = (
    "anchore/syft:v1.44.0@sha256:"
    "86fde6445b483d902fe011dd9f68c4987dd94e07da1e9edc004e3c2422650de6"
)


def _sbom(*, timestamp: str, serial: str) -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "type": "container",
                "name": "ghcr.io/acme/stonks-agent",
                "version": "sha256:" + ("a" * 64),
                "bom-ref": "image",
            },
        },
        "components": [
            {
                "type": "library",
                "name": "zeta",
                "version": "1.0",
                "purl": "pkg:pypi/zeta@1.0",
                "bom-ref": "zeta",
                "licenses": [{"license": {"id": "MIT"}}],
            },
            {
                "type": "file",
                "name": "/etc/config",
                "bom-ref": "file",
            },
            {
                "type": "library",
                "name": "alpha",
                "version": "2.0",
                "purl": "pkg:pypi/alpha@2.0",
                "bom-ref": "alpha",
            },
        ],
        "dependencies": [
            {"ref": "zeta", "dependsOn": []},
            {"ref": "alpha", "dependsOn": ["zeta"]},
        ],
    }


@pytest.mark.parametrize(
    "value",
    [
        "ghcr.io/acme/stonks-agent:latest",
        "stonks-agent@sha256:abc",
        "sha256:" + ("a" * 64),
        "ghcr.io/acme/stonks-agent@sha256:" + ("A" * 64),
        "ghcr.io/acme/stonks agent@sha256:" + ("a" * 64),
    ],
)
def test_validate_image_reference_rejects_mutable_or_ambiguous_input(
    value: str,
) -> None:
    with pytest.raises(SbomError, match="exact registry image digest"):
        validate_image_reference(value)


def test_syft_command_is_digest_pinned_bounded_and_never_uses_shell(
    tmp_path: Path,
) -> None:
    command = build_syft_command(
        image_reference=IMAGE,
        output_directory=tmp_path,
        output_name="raw.cdx.json",
        syft_image=SYFT,
    )

    assert command[:3] == ("docker", "run", "--rm")
    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"
    assert SYFT in command
    assert f"docker:{IMAGE}" in command
    assert command[-2:] == ("-o", "cyclonedx-json=/out/raw.cdx.json")
    assert all("\n" not in argument for argument in command)


def test_normalization_removes_only_nondeterminism_and_preserves_file_components() -> (
    None
):
    first_sbom, first_inventory = normalize_sbom(
        _sbom(timestamp="2026-07-18T01:00:00Z", serial="urn:uuid:first"),
        image_reference=IMAGE,
        license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
    )
    second_sbom, second_inventory = normalize_sbom(
        _sbom(timestamp="2026-07-18T02:00:00Z", serial="urn:uuid:second"),
        image_reference=IMAGE,
        license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
    )

    assert first_sbom == second_sbom
    assert first_inventory == second_inventory
    assert first_sbom["serialNumber"] == "urn:uuid:87ad2edb-6a1e-5aee-bb8a-ee169762e3ab"
    assert "timestamp" not in first_sbom["metadata"]
    assert [item["bom-ref"] for item in first_sbom["components"]] == [
        "alpha",
        "file",
        "zeta",
    ]
    assert first_inventory["component_count"] == 2
    assert len(first_inventory["components_sha256"]) == 64
    assert first_inventory["components"][0]["licenses"] == ["Apache-2.0"]


def test_normalization_binds_deterministic_serial_to_exact_image_digest() -> None:
    first, _ = normalize_sbom(
        _sbom(timestamp="2026-07-18T01:00:00Z", serial="urn:uuid:random"),
        image_reference=IMAGE,
        license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
    )
    second, _ = normalize_sbom(
        _sbom(timestamp="2026-07-18T01:00:00Z", serial="urn:uuid:random"),
        image_reference="ghcr.io/acme/stonks-agent@sha256:" + ("b" * 64),
        license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
    )

    assert first["serialNumber"] != second["serialNumber"]


def test_normalization_rejects_unknown_package_license_and_duplicate_purl() -> None:
    raw = _sbom(timestamp="2026-07-18T01:00:00Z", serial="urn:uuid:first")
    with pytest.raises(SbomError, match="missing reviewed license"):
        normalize_sbom(raw, image_reference=IMAGE, license_overrides={})

    components = raw["components"]
    assert isinstance(components, list)
    components.append(dict(components[0]))
    with pytest.raises(SbomError, match="duplicate package purl"):
        normalize_sbom(
            raw,
            image_reference=IMAGE,
            license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
        )


def test_write_normalized_sbom_is_canonical_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.json"
    output = tmp_path / "normalized.json"
    inventory = tmp_path / "inventory.json"
    raw.write_text(
        json.dumps(_sbom(timestamp="now", serial="random")),
        encoding="utf-8",
    )

    summary = write_normalized_sbom(
        raw,
        output,
        inventory,
        image_reference=IMAGE,
        license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
        max_input_bytes=100_000,
    )

    assert summary.package_count == 2
    assert output.read_bytes().endswith(b"\n")
    assert inventory.read_bytes().endswith(b"\n")
    assert json.loads(output.read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"

    link = tmp_path / "linked.json"
    try:
        link.symlink_to(output)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(SbomError, match="regular file"):
        write_normalized_sbom(
            link,
            tmp_path / "other.json",
            tmp_path / "other-inventory.json",
            image_reference=IMAGE,
            license_overrides={"pkg:pypi/alpha@2.0": "Apache-2.0"},
            max_input_bytes=100_000,
        )
