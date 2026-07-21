from __future__ import annotations

import json
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "generate_image_report_under_test",
    ROOT / "scripts" / "generate_image_report.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ImageReportError = MODULE.ImageReportError
build_image_report: Any = MODULE.build_image_report
generate_image_report: Any = MODULE.generate_image_report

DIGEST = "sha256:" + ("a" * 64)
CONFIG_DIGEST = "sha256:" + ("b" * 64)
SUBJECT = f"ghcr.io/acme/stonks-agent@{DIGEST}"
COMMIT = "c" * 40


def _inspect() -> list[dict[str, object]]:
    return [
        {
            "Id": CONFIG_DIGEST,
            "RepoDigests": [SUBJECT],
            "Config": {
                "User": "65532:65532",
                "Labels": {
                    "org.opencontainers.image.licenses": "Apache-2.0",
                    "org.opencontainers.image.revision": COMMIT,
                    "org.opencontainers.image.source": (
                        "https://github.com/acme/stonks-agent"
                    ),
                    "org.opencontainers.image.version": "0.1.0",
                },
            },
        }
    ]


def test_image_report_binds_exact_subject_config_and_oci_identity() -> None:
    report = build_image_report(
        _inspect(),
        subject=SUBJECT,
        repository="acme/stonks-agent",
        commit=COMMIT,
        version="0.1.0",
    )

    assert report == {
        "schema_version": "stonks-agent/core-image/v1",
        "subject": SUBJECT,
        "digest": DIGEST,
        "config_digest": CONFIG_DIGEST,
        "repository": "acme/stonks-agent",
        "revision": COMMIT,
        "version": "0.1.0",
        "source": "https://github.com/acme/stonks-agent",
        "licenses": "Apache-2.0",
        "user": "65532:65532",
        "execution_mode": "paper",
        "registry_verified": True,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"Id": "sha256:short"}), "config digest"),
        (lambda value: value.update({"RepoDigests": []}), "repository digest"),
        (
            lambda value: value["Config"].update({"User": "root"}),
            "non-root",
        ),
        (
            lambda value: value["Config"]["Labels"].update(
                {"org.opencontainers.image.revision": "d" * 40}
            ),
            "revision",
        ),
        (
            lambda value: value["Config"]["Labels"].update(
                {"org.opencontainers.image.source": "https://evil.invalid/repo"}
            ),
            "source",
        ),
    ],
)
def test_image_report_rejects_identity_drift(
    mutation: Any,
    message: str,
) -> None:
    payload = _inspect()
    mutation(payload[0])

    with pytest.raises(ImageReportError, match=message):
        build_image_report(
            payload,
            subject=SUBJECT,
            repository="acme/stonks-agent",
            commit=COMMIT,
            version="0.1.0",
        )


def test_generator_uses_typed_docker_argv_and_writes_canonical_json(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(_inspect()), "")

    output = tmp_path / "core-image.json"
    generate_image_report(
        local_reference="stonks-agent-core:release",
        subject=SUBJECT,
        repository="acme/stonks-agent",
        commit=COMMIT,
        version="0.1.0",
        output=output,
        runner=runner,
    )

    assert commands == [("docker", "image", "inspect", "stonks-agent-core:release")]
    assert output.read_bytes().endswith(b"\n")
    assert json.loads(output.read_text(encoding="utf-8"))["subject"] == SUBJECT


def test_local_candidate_is_explicitly_not_registry_verified() -> None:
    payload = _inspect()
    payload[0]["RepoDigests"] = []

    report = build_image_report(
        payload,
        subject=SUBJECT,
        repository="acme/stonks-agent",
        commit=COMMIT,
        version="0.1.0",
        require_registry_digest=False,
    )

    assert report["registry_verified"] is False


def test_generator_rejects_mutable_subject_and_docker_failure(
    tmp_path: Path,
) -> None:
    with pytest.raises(ImageReportError, match="exact registry"):
        generate_image_report(
            local_reference="stonks-agent-core:release",
            subject="ghcr.io/acme/stonks-agent:latest",
            repository="acme/stonks-agent",
            commit=COMMIT,
            version="0.1.0",
            output=tmp_path / "report.json",
        )

    def failed(
        command: tuple[str, ...],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "daemon failure")

    with pytest.raises(ImageReportError, match="inspect failed"):
        generate_image_report(
            local_reference="stonks-agent-core:release",
            subject=SUBJECT,
            repository="acme/stonks-agent",
            commit=COMMIT,
            version="0.1.0",
            output=tmp_path / "report.json",
            runner=failed,
        )
