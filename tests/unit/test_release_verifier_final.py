from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.release_verifier_common import ReleaseError
from scripts.release_verifier_final import verify_formal_evidence

IMAGE = "ghcr.io/acme/stonks-agent@sha256:" + ("a" * 64)
COMMIT = "b" * 40
REPORT = {
    "schema_version": "stonks-agent/release-verification/v1",
    "success": True,
    "status": "passed",
}
SBOM = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.6",
    "serialNumber": "urn:uuid:87ad2edb-6a1e-5aee-bb8a-ee169762e3ab",
    "version": 1,
    "components": [{"type": "library", "name": "stonks-agent", "version": "1.2.3"}],
}


def _policy() -> dict[str, object]:
    return {
        "sbom": {"path": "payload/release/core.cdx.json"},
        "signing": {
            "issuer": "https://token.actions.githubusercontent.com",
            "workflow": ".github/workflows/release.yml",
            "image_bundle": "signatures/core-image.sigstore.json",
            "manifest_bundle": "signatures/release-manifest.sigstore.json",
            "verification_report_bundle": (
                "signatures/verification-report.sigstore.json"
            ),
            "final_evidence": {
                "image_bundle": "signatures/core-image.sigstore.json",
                "manifest_bundle": "signatures/release-manifest.sigstore.json",
                "verification_report_bundle": (
                    "signatures/verification-report.sigstore.json"
                ),
                "provenance_bundle": "signatures/github-provenance.sigstore.json",
                "sbom_bundle": "signatures/github-sbom.sigstore.json",
                "provenance_predicate_type": "https://slsa.dev/provenance/v1",
                "sbom_predicate_type": "https://cyclonedx.org/bom",
                "max_bundle_bytes": 33_554_432,
            },
        },
    }


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    release = bundle / "payload" / "release"
    release.mkdir(parents=True)
    (release / "core.cdx.json").write_text(json.dumps(SBOM), encoding="utf-8")
    signatures = bundle / "signatures"
    signatures.mkdir()
    (bundle / "release-manifest.json").write_text("{}", encoding="utf-8")
    (bundle / "verification-report.json").write_text(
        json.dumps(REPORT), encoding="utf-8"
    )
    for name in (
        "core-image.sigstore.json",
        "release-manifest.sigstore.json",
        "verification-report.sigstore.json",
        "github-provenance.sigstore.json",
        "github-sbom.sigstore.json",
    ):
        (signatures / name).write_text("{}", encoding="utf-8")
    return bundle


def _gh_result(predicate: str) -> str:
    predicate_payload = (
        SBOM
        if predicate == "https://cyclonedx.org/bom"
        else {"buildDefinition": {}, "runDetails": {}}
    )
    return json.dumps(
        [
            {
                "attestation": {},
                "verificationResult": {
                    "statement": {
                        "subject": [
                            {
                                "name": "ghcr.io/acme/stonks-agent",
                                "digest": {"sha256": "a" * 64},
                            }
                        ],
                        "predicateType": predicate,
                        "predicate": predicate_payload,
                    }
                },
            }
        ]
    )


def _successful_runner(
    command: tuple[str, ...], **_: object
) -> subprocess.CompletedProcess[str]:
    predicate = (
        "https://cyclonedx.org/bom"
        if "https://cyclonedx.org/bom" in command
        else "https://slsa.dev/provenance/v1"
    )
    stdout = _gh_result(predicate) if command[0] == "gh" else ""
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_final_verifier_rechecks_exact_five_evidence_and_identity(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        predicate = (
            "https://cyclonedx.org/bom"
            if "https://cyclonedx.org/bom" in command
            else "https://slsa.dev/provenance/v1"
        )
        stdout = _gh_result(predicate) if command[0] == "gh" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    result = verify_formal_evidence(
        bundle,
        _policy(),
        image=IMAGE,
        repository="acme/stonks-agent",
        tag="v1.2.3",
        commit=COMMIT,
        expected_report=REPORT,
        runner=runner,
    )

    assert result == {
        "schema_version": "stonks-agent/formal-release-verification/v1",
        "status": "passed",
        "evidence_count": 5,
        "image": IMAGE,
        "repository": "acme/stonks-agent",
        "ref": "refs/tags/v1.2.3",
        "commit": COMMIT,
    }
    assert len(commands) == 5
    assert [command[:2] for command in commands] == [
        ("cosign", "verify"),
        ("cosign", "verify-blob"),
        ("cosign", "verify-blob"),
        ("gh", "attestation"),
        ("gh", "attestation"),
    ]
    identity = (
        "https://github.com/acme/stonks-agent/.github/workflows/"
        "release.yml@refs/tags/v1.2.3"
    )
    for command in commands:
        assert "acme/stonks-agent" in command
        assert "refs/tags/v1.2.3" in command
        assert COMMIT in command
        if command[0] == "cosign":
            assert identity in command
            assert "https://token.actions.githubusercontent.com" in command
        else:
            assert "acme/stonks-agent/.github/workflows/release.yml" in command
            assert "--deny-self-hosted-runners" in command
            assert "--bundle" in command
            assert "--format" in command


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/acme/stonks-agent-evil@sha256:" + ("a" * 64),
        "ghcr.io/acme/stonks-agent/child@sha256:" + ("a" * 64),
    ],
)
def test_final_verifier_rejects_non_exact_repository_image(
    tmp_path: Path,
    image: str,
) -> None:
    def unexpected_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"identity mismatch reached external tool: {command!r}")

    with pytest.raises(
        ReleaseError,
        match="formal image repository does not match formal release repository",
    ):
        verify_formal_evidence(
            _bundle(tmp_path),
            _policy(),
            image=image,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=unexpected_runner,
        )


@pytest.mark.parametrize("failure", ["missing", "extra", "root_extra", "symlink"])
def test_final_verifier_rejects_missing_extra_and_symlink_evidence(
    tmp_path: Path, failure: str
) -> None:
    bundle = _bundle(tmp_path)
    signatures = bundle / "signatures"
    if failure == "missing":
        (signatures / "github-sbom.sigstore.json").unlink()
    elif failure == "extra":
        (signatures / "untrusted.sigstore.json").write_text("{}", encoding="utf-8")
    elif failure == "root_extra":
        (bundle / "untrusted.json").write_text("{}", encoding="utf-8")
    else:
        target = signatures / "target"
        target.write_text("{}", encoding="utf-8")
        evidence = signatures / "github-sbom.sigstore.json"
        evidence.unlink()
        try:
            evidence.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable")

    with pytest.raises(ReleaseError):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=_successful_runner,
        )


def test_final_verifier_rejects_payload_targeted_evidence_symlink_before_tools(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    target = bundle / "payload" / "existing-evidence.json"
    target.write_text("{}", encoding="utf-8")
    evidence = bundle / "signatures" / "github-sbom.sigstore.json"
    evidence.unlink()
    try:
        evidence.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    def unexpected_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"evidence symlink reached external tool: {command!r}")

    with pytest.raises(ReleaseError, match="formal evidence file is invalid"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=unexpected_runner,
        )


def test_final_verifier_rejects_report_and_attestation_semantic_drift(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    drifted = {**REPORT, "status": "failed"}
    (bundle / "verification-report.json").write_text(
        json.dumps(drifted), encoding="utf-8"
    )
    with pytest.raises(ReleaseError, match="verification report drifted"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
        )

    (bundle / "verification-report.json").write_text(
        json.dumps(REPORT), encoding="utf-8"
    )

    def drift_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        output = _gh_result("https://example.invalid/predicate")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(ReleaseError, match="attestation predicate drifted"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=drift_runner,
        )

    def digest_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        predicate = (
            "https://cyclonedx.org/bom"
            if "https://cyclonedx.org/bom" in command
            else "https://slsa.dev/provenance/v1"
        )
        output = _gh_result(predicate).replace("a" * 64, "c" * 64)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(ReleaseError, match="attestation subject digest drifted"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=digest_runner,
        )


@pytest.mark.parametrize("mutation", ["missing", "different"])
def test_final_verifier_rejects_sbom_predicate_drift(
    tmp_path: Path, mutation: str
) -> None:
    bundle = _bundle(tmp_path)

    def drift_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        predicate_type = (
            "https://cyclonedx.org/bom"
            if "https://cyclonedx.org/bom" in command
            else "https://slsa.dev/provenance/v1"
        )
        payload = json.loads(_gh_result(predicate_type))
        if predicate_type == "https://cyclonedx.org/bom":
            statement = payload[0]["verificationResult"]["statement"]
            if mutation == "missing":
                statement.pop("predicate")
            else:
                statement["predicate"]["components"][0]["version"] = "9.9.9"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    with pytest.raises(ReleaseError, match="SBOM attestation predicate drifted"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=drift_runner,
        )


def test_final_verifier_rejects_non_image_bound_canonical_sbom_serial(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    sbom_path = bundle / "payload" / "release" / "core.cdx.json"
    drifted_sbom = {**SBOM, "serialNumber": "urn:uuid:" + ("1" * 32)}
    sbom_path.write_text(json.dumps(drifted_sbom), encoding="utf-8")

    def matching_drift_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        predicate_type = (
            "https://cyclonedx.org/bom"
            if "https://cyclonedx.org/bom" in command
            else "https://slsa.dev/provenance/v1"
        )
        payload = json.loads(_gh_result(predicate_type))
        if predicate_type == "https://cyclonedx.org/bom":
            payload[0]["verificationResult"]["statement"]["predicate"] = drifted_sbom
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    with pytest.raises(ReleaseError, match="canonical SBOM identity is invalid"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=matching_drift_runner,
        )


def test_final_verifier_rejects_tool_failure_and_policy_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    def failed_runner(
        command: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failure")

    with pytest.raises(ReleaseError, match="formal evidence verification failed"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=failed_runner,
        )

    policy = _policy()
    signing = policy["signing"]
    assert isinstance(signing, dict)
    final = signing["final_evidence"]
    assert isinstance(final, dict)
    final["unexpected"] = True
    with pytest.raises(ReleaseError, match="final evidence policy fields drifted"):
        verify_formal_evidence(
            bundle,
            policy,
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
        )

    with pytest.raises(ReleaseError, match="formal tag identity is invalid"):
        verify_formal_evidence(
            bundle,
            _policy(),
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3/unsafe",
            commit=COMMIT,
            expected_report=REPORT,
        )

    policy = _policy()
    signing = policy["signing"]
    assert isinstance(signing, dict)
    signing["image_bundle"] = "signatures/drifted.sigstore.json"
    with pytest.raises(ReleaseError, match="formal evidence path policy diverged"):
        verify_formal_evidence(
            bundle,
            policy,
            image=IMAGE,
            repository="acme/stonks-agent",
            tag="v1.2.3",
            commit=COMMIT,
            expected_report=REPORT,
            runner=_successful_runner,
        )
