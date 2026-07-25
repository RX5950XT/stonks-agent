from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import smoke_optional_profiles
from scripts.smoke_optional_profiles import (
    CanonicalCounts,
    CiProvenance,
    CoreReadiness,
    OptionalSmokeError,
    ProfileObservation,
    SmokeHarness,
    _run_ci_smoke,
    _run_fail_closed_auth_boundary,
    _write_report,
    build_report,
    load_policy,
    main,
    verify_report,
)

ROOT = Path(__file__).resolve().parents[2]


class FakeHarness(SmokeHarness):
    def __init__(self) -> None:
        self.counts = CanonicalCounts.zero()
        self.unready = False
        self.drift = False

    def provenance(self) -> CiProvenance:
        return CiProvenance(
            github_run_id="123456",
            github_sha="a" * 40,
            github_workflow_ref=(
                "example/stonks-agent/.github/workflows/ci.yml@refs/heads/main"
            ),
        )

    def readiness(self) -> CoreReadiness:
        return CoreReadiness(
            ready=not self.unready,
            execution_mode="paper",
            migration_revision="0017",
            build_revision="abcdef1",
        )

    def canonical_counts(self) -> CanonicalCounts:
        if self.drift:
            return self.counts.model_copy(update={"order_intent": 1})
        return self.counts

    def observe(self, profile: str, expectation: str) -> ProfileObservation:
        if expectation == "actual_runtime":
            return ProfileObservation(
                status="actual_passed",
                evidence_class="independent_ci_actual_runtime",
                runtime_compatibility_verified=True,
                bounded_exit_code=0,
            )
        if expectation == "blocked":
            return ProfileObservation(
                status="blocked",
                evidence_class="auth_boundary_fail_closed",
                runtime_compatibility_verified=False,
                bounded_exit_code=78,
            )
        assert profile == "kronos-cuda"
        return ProfileObservation(
            status="unsupported",
            evidence_class="unsupported_ci_hardware",
            runtime_compatibility_verified=False,
            bounded_exit_code=None,
        )


def test_policy_is_exactly_the_ten_compose_profiles() -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")

    assert len(policy.profiles) == 10
    assert {profile.profile for profile in policy.profiles} == {
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
    assert policy.execution_mode == "paper"


def test_report_separates_compatibility_from_absence_safety() -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    report = build_report(policy, FakeHarness())

    verified = verify_report(policy, report)

    assert verified.matrix_contract_status == "passed"
    assert verified.absence_safety_verified is True
    assert verified.runtime_compatibility_complete is False
    assert verified.actual_runtime_count == 4
    assert verified.blocked_count == 5
    assert verified.unsupported_count == 1
    assert all(item.isolation_verified for item in verified.profiles)
    assert all(
        item.compatibility.runtime_compatibility_verified is False
        for item in verified.profiles
        if item.compatibility.status != "actual_passed"
    )


def test_forged_pass_for_blocked_profile_is_rejected() -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    report = build_report(policy, FakeHarness())
    profiles = list(report.profiles)
    index = next(
        index for index, item in enumerate(profiles) if item.profile == "kronos-cpu"
    )
    profiles[index] = profiles[index].model_copy(
        update={
            "compatibility": ProfileObservation(
                status="actual_passed",
                evidence_class="independent_ci_actual_runtime",
                runtime_compatibility_verified=True,
                bounded_exit_code=0,
            )
        }
    )

    with pytest.raises(OptionalSmokeError):
        verify_report(policy, report.model_copy(update={"profiles": tuple(profiles)}))


def test_core_readiness_failure_fails_closed() -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    harness = FakeHarness()
    harness.unready = True

    with pytest.raises(OptionalSmokeError):
        build_report(policy, harness)


def test_canonical_paper_side_effect_fails_closed() -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    harness = FakeHarness()
    calls = 0

    def drifting_counts() -> CanonicalCounts:
        nonlocal calls
        calls += 1
        if calls == 4:
            return CanonicalCounts.zero().model_copy(update={"paper_fill": 1})
        return CanonicalCounts.zero()

    harness.canonical_counts = drifting_counts  # type: ignore[method-assign]

    with pytest.raises(OptionalSmokeError):
        build_report(policy, harness)


def test_unknown_or_duplicate_profile_policy_fails_closed(tmp_path: Path) -> None:
    source = (ROOT / "config" / "optional-smoke.yaml").read_text(encoding="utf-8")
    path = tmp_path / "optional-smoke.yaml"
    path.write_text(
        source.replace("profile: rd-agent", "profile: openbb"), encoding="utf-8"
    )

    with pytest.raises(OptionalSmokeError):
        load_policy(path)


def test_profile_to_integration_mapping_drift_fails_closed(tmp_path: Path) -> None:
    source = (ROOT / "config" / "optional-smoke.yaml").read_text(encoding="utf-8")
    drifted = source.replace(
        "  - profile: qlib\n    integration: qlib",
        "  - profile: qlib\n    integration: openbb",
    )
    assert drifted != source
    path = tmp_path / "optional-smoke.yaml"
    path.write_text(drifted, encoding="utf-8")

    with pytest.raises(OptionalSmokeError):
        load_policy(path)


def test_auth_boundary_probe_runs_argv_only_with_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(
        command: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command, 78, stdout=b"secret", stderr=b"token"
        )

    monkeypatch.setenv("UNSAFE_AMBIENT_TOKEN", "must-not-propagate")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_fail_closed_auth_boundary("tradingagents-paper")

    assert result.status == "blocked"
    assert result.runtime_compatibility_verified is False
    assert result.bounded_exit_code == 78
    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert "UNSAFE_AMBIENT_TOKEN" not in observed["env"]
    assert "secret" not in result.model_dump_json()


def test_auth_boundary_probe_rejects_unexpected_process_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=b"", stderr=b"ModuleNotFoundError"
        ),
    )

    with pytest.raises(OptionalSmokeError):
        _run_fail_closed_auth_boundary("qlib")


def test_report_publish_is_no_overwrite_and_cleanup_error_is_public_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    report = build_report(policy, FakeHarness())
    output = tmp_path / "optional-smoke.json"

    _write_report(output, report)

    assert output.is_file()
    assert "runtime_compatibility_complete" in output.read_text(encoding="utf-8")
    with pytest.raises(OptionalSmokeError):
        _write_report(output, report)

    cleanup_output = tmp_path / "cleanup-failure.json"
    monkeypatch.setattr(
        Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OptionalSmokeError) as raised:
        _write_report(cleanup_output, report)
    assert str(raised.value) == "Optional integration smoke failed"


def test_ci_smoke_passes_a_new_owned_secret_directory_to_build_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_policy(ROOT / "config" / "optional-smoke.yaml")
    observed: dict[str, Path] = {}

    def fake_build_context(**kwargs: object) -> object:
        secret_directory = kwargs["secret_directory"]
        assert isinstance(secret_directory, Path)
        observed["secret_directory"] = secret_directory
        assert secret_directory.name == "secrets"
        assert secret_directory.parent.is_dir()
        assert not secret_directory.exists()
        secret_directory.mkdir()
        return SimpleNamespace(
            compose_prefix=("docker", "compose"),
            environment={},
            secret_values=("owner-secret", "runtime-secret"),
        )

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def run(self, command: object, **kwargs: object) -> str:
            del command, kwargs
            return ""

    monkeypatch.setattr(smoke_optional_profiles, "build_context", fake_build_context)
    monkeypatch.setattr(smoke_optional_profiles, "SubprocessRunner", FakeRunner)
    monkeypatch.setattr(
        smoke_optional_profiles, "_start_core", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        smoke_optional_profiles,
        "DockerCiHarness",
        lambda *args, **kwargs: FakeHarness(),
    )
    monkeypatch.setattr(smoke_optional_profiles, "_write_report", lambda *args: None)

    _run_ci_smoke(
        Namespace(
            core_port=18_100,
            skip_core_build=True,
            output=tmp_path / "report.json",
        ),
        policy,
    )

    assert observed["secret_directory"].name == "secrets"


@pytest.mark.parametrize("failure", [OSError("credential-path"), RuntimeError("token")])
def test_ci_setup_failure_emits_only_public_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    def fail_build_context(**kwargs: object) -> object:
        del kwargs
        raise failure

    monkeypatch.setattr(smoke_optional_profiles, "build_context", fail_build_context)

    exit_code = main(
        [
            "--core-port",
            "18100",
            "--output",
            str(tmp_path / "optional-smoke.json"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "Optional integration smoke failed" in captured.err
    assert "Traceback" not in captured.err
    assert "credential-path" not in captured.err
    assert "token" not in captured.err
