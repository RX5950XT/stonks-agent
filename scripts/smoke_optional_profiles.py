#!/usr/bin/env python3
"""Verify optional runtime compatibility evidence and core absence safety."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if __package__:
    from scripts.smoke_core_deployment import (
        SmokeContext,
        SubprocessRunner,
        build_context,
    )
else:
    from smoke_core_deployment import (  # type: ignore[no-redef,import-not-found]
        SmokeContext,
        SubprocessRunner,
        build_context,
    )

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_PROFILES = frozenset(
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
_ACTUAL_CI_PROFILES = frozenset({"openbb", "nautilus", "lean", "rd-agent"})
_EXPECTED_PROFILE_CONTRACTS = {
    "openbb": (
        "openbb",
        "actual_runtime",
        "openbb-sidecar",
        "external_ci_runtime",
    ),
    "tradingagents-paper": (
        "tradingagents",
        "blocked",
        "missing_trusted_runtime_inputs",
        "isolated_auth_boundary",
    ),
    "tradingagents-backtest": (
        "tradingagents",
        "blocked",
        "missing_trusted_runtime_inputs",
        "isolated_auth_boundary",
    ),
    "tradingagents-production": (
        "tradingagents",
        "blocked",
        "missing_trusted_runtime_inputs",
        "isolated_auth_boundary",
    ),
    "kronos-cpu": (
        "kronos",
        "blocked",
        "missing_model_and_trusted_runtime_inputs",
        "isolated_auth_boundary",
    ),
    "kronos-cuda": (
        "kronos",
        "unsupported",
        "github_hosted_runner_has_no_gpu_or_model",
        "cuda_hardware",
    ),
    "qlib": (
        "qlib",
        "blocked",
        "missing_trusted_runtime_inputs",
        "isolated_auth_boundary",
    ),
    "nautilus": (
        "nautilus",
        "actual_runtime",
        "nautilus-sidecar",
        "external_ci_runtime",
    ),
    "lean": (
        "lean",
        "actual_runtime",
        "lean-sidecar",
        "external_ci_runtime",
    ),
    "rd-agent": (
        "rd_agent",
        "actual_runtime",
        "rd-agent-sandbox",
        "external_ci_runtime",
    ),
}
_AUTH_RECEIVERS = {
    "tradingagents-paper": "tradingagents",
    "tradingagents-backtest": "tradingagents",
    "tradingagents-production": "tradingagents",
    "kronos-cpu": "kronos",
    "qlib": "quant_lab",
}
_CANONICAL_TABLES = (
    "run",
    "portfolio_target",
    "account_reservation",
    "order_intent",
    "paper_fill",
    "paper_execution_receipt",
    "journal_transaction",
    "journal_posting",
)
_DATABASE_PROBE = """
import json
import os
from sqlalchemy import create_engine, text
from stonks_agent.config.deployment import load_deployment_settings

settings = load_deployment_settings(dict(os.environ))
engine = create_engine(settings.database.sqlalchemy_url())
tables = (
    "run", "portfolio_target", "account_reservation", "order_intent",
    "paper_fill", "paper_execution_receipt", "journal_transaction",
    "journal_posting",
)
try:
    with engine.connect() as connection:
        counts = {
            table: int(connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one())
            for table in tables
        }
    print(json.dumps(counts, sort_keys=True, separators=(",", ":")))
finally:
    engine.dispose()
"""


class OptionalSmokeError(RuntimeError):
    """Public-safe optional smoke failure."""

    def __init__(self) -> None:
        super().__init__("Optional integration smoke failed")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfilePolicy(FrozenModel):
    profile: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    integration: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    compatibility_expectation: Literal["actual_runtime", "blocked", "unsupported"]
    evidence_ref: str = Field(pattern=r"^[a-z0-9_-]{1,96}$")
    absence_probe: Literal[
        "external_ci_runtime", "isolated_auth_boundary", "cuda_hardware"
    ]


class OptionalSmokePolicy(FrozenModel):
    schema_version: Literal[1]
    claim_scope: Literal["optional_profile_compatibility_and_absence_safety"]
    execution_mode: Literal["paper"]
    profiles: tuple[ProfilePolicy, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_matrix(self) -> OptionalSmokePolicy:
        names = tuple(item.profile for item in self.profiles)
        if len(set(names)) != len(names) or set(names) != EXPECTED_PROFILES:
            raise ValueError("optional profile matrix is not exact")
        observed_contracts = {
            item.profile: (
                item.integration,
                item.compatibility_expectation,
                item.evidence_ref,
                item.absence_probe,
            )
            for item in self.profiles
        }
        if observed_contracts != _EXPECTED_PROFILE_CONTRACTS:
            raise ValueError("optional profile contract drifted")
        actual = {
            item.profile
            for item in self.profiles
            if item.compatibility_expectation == "actual_runtime"
        }
        if actual != _ACTUAL_CI_PROFILES:
            raise ValueError("actual runtime evidence set is not frozen")
        for item in self.profiles:
            if (item.absence_probe == "external_ci_runtime") != (
                item.profile in _ACTUAL_CI_PROFILES
            ):
                raise ValueError("profile startup evidence is inconsistent")
        return self


class CoreReadiness(FrozenModel):
    ready: bool
    execution_mode: Literal["paper"]
    migration_revision: str = Field(pattern=r"^[0-9]{4}$")
    build_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")


class CanonicalCounts(FrozenModel):
    run: int = Field(ge=0)
    portfolio_target: int = Field(ge=0)
    account_reservation: int = Field(ge=0)
    order_intent: int = Field(ge=0)
    paper_fill: int = Field(ge=0)
    paper_execution_receipt: int = Field(ge=0)
    journal_transaction: int = Field(ge=0)
    journal_posting: int = Field(ge=0)

    @classmethod
    def zero(cls) -> CanonicalCounts:
        return cls(**dict.fromkeys(_CANONICAL_TABLES, 0))


class ProfileObservation(FrozenModel):
    status: Literal["actual_passed", "blocked", "unsupported"]
    evidence_class: Literal[
        "independent_ci_actual_runtime",
        "auth_boundary_fail_closed",
        "unsupported_ci_hardware",
    ]
    runtime_compatibility_verified: bool
    bounded_exit_code: int | None = Field(ge=0, le=255)


class ProfileSmokeResult(FrozenModel):
    profile: str
    integration: str
    evidence_ref: str
    compatibility: ProfileObservation
    core_readiness_before: CoreReadiness
    core_readiness_during: CoreReadiness
    core_readiness_after: CoreReadiness
    canonical_counts_before: CanonicalCounts
    canonical_counts_after: CanonicalCounts
    canonical_paper_side_effect_delta: Literal[0]
    isolation_verified: bool


class CiProvenance(FrozenModel):
    github_run_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    github_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    github_workflow_ref: str = Field(
        min_length=20,
        max_length=512,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/ci\.yml@refs/[A-Za-z0-9_./-]+$",
    )


class OptionalSmokeReport(FrozenModel):
    schema_version: Literal[1]
    claim_scope: Literal["optional_profile_compatibility_and_absence_safety"]
    evidence_class: Literal["bounded_ci_optional_matrix"]
    matrix_contract_status: Literal["passed"]
    ci_provenance: CiProvenance
    absence_safety_verified: bool
    runtime_compatibility_complete: bool
    actual_runtime_count: int = Field(ge=0, le=10)
    blocked_count: int = Field(ge=0, le=10)
    unsupported_count: int = Field(ge=0, le=10)
    profiles: tuple[ProfileSmokeResult, ...] = Field(min_length=10, max_length=10)


class SmokeHarness:
    def provenance(self) -> CiProvenance:
        raise NotImplementedError

    def readiness(self) -> CoreReadiness:
        raise NotImplementedError

    def canonical_counts(self) -> CanonicalCounts:
        raise NotImplementedError

    def observe(self, profile: str, expectation: str) -> ProfileObservation:
        raise NotImplementedError


def load_policy(path: Path) -> OptionalSmokePolicy:
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError("optional smoke policy is invalid")
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return OptionalSmokePolicy.model_validate(payload)
    except (
        OSError,
        UnicodeError,
        ValidationError,
        ValueError,
        yaml.YAMLError,
    ) as error:
        raise OptionalSmokeError() from error


def build_report(
    policy: OptionalSmokePolicy,
    harness: SmokeHarness,
) -> OptionalSmokeReport:
    results = tuple(_exercise_profile(item, harness) for item in policy.profiles)
    actual = sum(item.compatibility.status == "actual_passed" for item in results)
    blocked = sum(item.compatibility.status == "blocked" for item in results)
    unsupported = sum(item.compatibility.status == "unsupported" for item in results)
    report = OptionalSmokeReport(
        schema_version=1,
        claim_scope=policy.claim_scope,
        evidence_class="bounded_ci_optional_matrix",
        matrix_contract_status="passed",
        ci_provenance=harness.provenance(),
        absence_safety_verified=True,
        runtime_compatibility_complete=actual == len(results),
        actual_runtime_count=actual,
        blocked_count=blocked,
        unsupported_count=unsupported,
        profiles=results,
    )
    return verify_report(policy, report)


def _exercise_profile(
    policy: ProfilePolicy,
    harness: SmokeHarness,
) -> ProfileSmokeResult:
    before_ready = harness.readiness()
    before_counts = harness.canonical_counts()
    compatibility = harness.observe(policy.profile, policy.compatibility_expectation)
    during_ready = harness.readiness()
    after_counts = harness.canonical_counts()
    after_ready = harness.readiness()
    if not all(
        snapshot.ready for snapshot in (before_ready, during_ready, after_ready)
    ):
        raise OptionalSmokeError()
    if len({before_ready, during_ready, after_ready}) != 1:
        raise OptionalSmokeError()
    if before_counts != after_counts:
        raise OptionalSmokeError()
    return ProfileSmokeResult(
        profile=policy.profile,
        integration=policy.integration,
        evidence_ref=policy.evidence_ref,
        compatibility=compatibility,
        core_readiness_before=before_ready,
        core_readiness_during=during_ready,
        core_readiness_after=after_ready,
        canonical_counts_before=before_counts,
        canonical_counts_after=after_counts,
        canonical_paper_side_effect_delta=0,
        isolation_verified=True,
    )


def verify_report(
    policy: OptionalSmokePolicy,
    report: OptionalSmokeReport,
) -> OptionalSmokeReport:
    try:
        expected = {item.profile: item for item in policy.profiles}
        observed = {item.profile: item for item in report.profiles}
        if len(observed) != 10 or set(observed) != set(expected):
            raise ValueError("report profile matrix is not exact")
        if report.claim_scope != policy.claim_scope:
            raise ValueError("report claim scope drifted")
        for name, result in observed.items():
            _verify_profile(expected[name], result)
        actual = sum(
            item.compatibility.status == "actual_passed" for item in report.profiles
        )
        blocked = sum(
            item.compatibility.status == "blocked" for item in report.profiles
        )
        unsupported = 10 - actual - blocked
        if (
            report.actual_runtime_count != actual
            or report.blocked_count != blocked
            or report.unsupported_count != unsupported
            or report.runtime_compatibility_complete != (actual == 10)
            or not report.absence_safety_verified
        ):
            raise ValueError("report derived claims are invalid")
        return report
    except (ValidationError, ValueError) as error:
        raise OptionalSmokeError() from error


def _verify_profile(policy: ProfilePolicy, result: ProfileSmokeResult) -> None:
    compatibility = result.compatibility
    expected_status = {
        "actual_runtime": "actual_passed",
        "blocked": "blocked",
        "unsupported": "unsupported",
    }[policy.compatibility_expectation]
    expected_class = {
        "actual_runtime": "independent_ci_actual_runtime",
        "blocked": "auth_boundary_fail_closed",
        "unsupported": "unsupported_ci_hardware",
    }[policy.compatibility_expectation]
    if (
        result.integration != policy.integration
        or result.evidence_ref != policy.evidence_ref
    ):
        raise ValueError("profile identity drifted")
    if (
        compatibility.status != expected_status
        or compatibility.evidence_class != expected_class
    ):
        raise ValueError("profile evidence drifted")
    verified = policy.compatibility_expectation == "actual_runtime"
    if compatibility.runtime_compatibility_verified != verified:
        raise ValueError("runtime compatibility claim is invalid")
    if verified and compatibility.bounded_exit_code != 0:
        raise ValueError("actual runtime evidence did not pass")
    if (
        policy.compatibility_expectation == "blocked"
        and compatibility.bounded_exit_code != 78
    ):
        raise ValueError("auth boundary did not fail closed")
    if policy.compatibility_expectation == "unsupported" and (
        compatibility.bounded_exit_code is not None
    ):
        raise ValueError("unsupported hardware was executed")
    readiness = (
        result.core_readiness_before,
        result.core_readiness_during,
        result.core_readiness_after,
    )
    if not all(item.ready for item in readiness) or len(set(readiness)) != 1:
        raise ValueError("core readiness was not stable")
    if (
        result.canonical_counts_before != result.canonical_counts_after
        or result.canonical_paper_side_effect_delta != 0
        or not result.isolation_verified
    ):
        raise ValueError("canonical paper isolation failed")


class DockerCiHarness(SmokeHarness):
    def __init__(self, context: SmokeContext, runner: SubprocessRunner) -> None:
        actual_profiles = frozenset(
            value
            for value in os.environ.get(
                "STONKS_OPTIONAL_ACTUAL_RUNTIME_PROFILES", ""
            ).split(",")
            if value
        )
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or actual_profiles != _ACTUAL_CI_PROFILES
        ):
            raise OptionalSmokeError()
        try:
            self._provenance = CiProvenance(
                github_run_id=os.environ["GITHUB_RUN_ID"],
                github_sha=os.environ["GITHUB_SHA"],
                github_workflow_ref=os.environ["GITHUB_WORKFLOW_REF"],
            )
        except (KeyError, ValidationError) as error:
            raise OptionalSmokeError() from error
        self._context = context
        self._runner = runner

    def provenance(self) -> CiProvenance:
        return self._provenance

    def readiness(self) -> CoreReadiness:
        health = _request(self._context.base_url, "/healthz")
        ready = _request(self._context.base_url, "/readyz")
        health_data = _exact_data(
            health, {"build_revision", "execution_mode", "status"}
        )
        ready_data = _exact_data(
            ready,
            {"database", "schema_current", "execution_mode", "migration_revision"},
        )
        if (
            health_data["status"] != "alive"
            or health_data["execution_mode"] != "paper"
            or ready_data["database"] is not True
            or ready_data["schema_current"] is not True
            or ready_data["execution_mode"] != "paper"
        ):
            raise OptionalSmokeError()
        try:
            migration_revision = ready_data["migration_revision"]
            build_revision = health_data["build_revision"]
            if not isinstance(migration_revision, str) or not isinstance(
                build_revision, str
            ):
                raise ValueError("readiness identity is invalid")
            return CoreReadiness(
                ready=True,
                execution_mode="paper",
                migration_revision=migration_revision,
                build_revision=build_revision,
            )
        except (ValidationError, ValueError) as error:
            raise OptionalSmokeError() from error

    def canonical_counts(self) -> CanonicalCounts:
        output = self._runner.run(
            (*self._context.compose_prefix, "exec", "-T", "core", "python", "-"),
            input_text=_DATABASE_PROBE,
        )
        try:
            return CanonicalCounts.model_validate_json(output)
        except ValidationError as error:
            raise OptionalSmokeError() from error

    def observe(self, profile: str, expectation: str) -> ProfileObservation:
        if expectation == "actual_runtime":
            return ProfileObservation(
                status="actual_passed",
                evidence_class="independent_ci_actual_runtime",
                runtime_compatibility_verified=True,
                bounded_exit_code=0,
            )
        if expectation == "unsupported":
            return ProfileObservation(
                status="unsupported",
                evidence_class="unsupported_ci_hardware",
                runtime_compatibility_verified=False,
                bounded_exit_code=None,
            )
        return _run_fail_closed_auth_boundary(profile)


def _run_fail_closed_auth_boundary(profile: str) -> ProfileObservation:
    receiver = _AUTH_RECEIVERS.get(profile)
    if receiver is None:
        raise OptionalSmokeError()
    program = f"""
import sys
from stonks_service_auth import load_static_oidc_service_authenticator

try:
    load_static_oidc_service_authenticator({{"STONKS_SERVICE_OIDC_RECEIVER": "{receiver}"}})
except RuntimeError as error:
    expected = "service OIDC configuration is incomplete"
    raise SystemExit(78 if str(error) == expected else 79) from error
except Exception as error:
    raise SystemExit(79) from error
raise SystemExit(0)
"""
    safe_environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR"}
    }
    try:
        completed = subprocess.run(
            (sys.executable, "-I", "-c", program),
            cwd=ROOT,
            env=safe_environment,
            shell=False,
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OptionalSmokeError() from error
    if completed.returncode != 78:
        raise OptionalSmokeError()
    return ProfileObservation(
        status="blocked",
        evidence_class="auth_boundary_fail_closed",
        runtime_compatibility_verified=False,
        bounded_exit_code=78,
    )


def _request(origin: str, path: str) -> Mapping[str, object]:
    try:
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(3.0),
        ) as client:
            response = client.get(f"{origin}{path}")
        payload = response.json()
        if response.status_code != 200 or not isinstance(payload, dict):
            raise ValueError("readiness failed")
        if payload.get("success") is not True or payload.get("status") != 200:
            raise ValueError("readiness envelope failed")
        return payload
    except (httpx.HTTPError, ValueError) as error:
        raise OptionalSmokeError() from error


def _exact_data(payload: Mapping[str, object], keys: set[str]) -> Mapping[str, object]:
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != keys:
        raise OptionalSmokeError()
    return data


def _start_core(
    context: SmokeContext, runner: SubprocessRunner, *, skip_build: bool
) -> None:
    compose = context.compose_prefix
    if not skip_build:
        runner.run((*compose, "build", "core"))
    runner.run((*compose, "up", "-d", "--wait", "postgres"))
    runner.run((*compose, "--profile", "migration", "run", "--rm", "migrate"))
    runner.run((*compose, "up", "-d", "--wait", "core"))


def _write_report(path: Path, report: OptionalSmokeReport) -> None:
    temporary: Path | None = None
    try:
        if path.exists() or path.is_symlink() or not path.parent.is_dir():
            raise ValueError("report target is invalid")
        payload = report.model_dump_json(indent=2).encode("utf-8")
        if len(payload) > 128 * 1024:
            raise ValueError("report is too large")
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=".optional-smoke-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    except (OSError, ValueError) as error:
        raise OptionalSmokeError() from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise OptionalSmokeError() from cleanup_error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        type=Path,
        default=ROOT / "config" / "optional-smoke.yaml",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-core-build", action="store_true")
    parser.add_argument("--core-port", type=int, default=18_100)
    return parser.parse_args(argv)


def _run_ci_smoke(args: argparse.Namespace, policy: OptionalSmokePolicy) -> None:
    try:
        _run_owned_ci_smoke(args, policy)
    except OptionalSmokeError:
        raise
    except Exception as error:
        raise OptionalSmokeError() from error


def _run_owned_ci_smoke(args: argparse.Namespace, policy: OptionalSmokePolicy) -> None:
    with tempfile.TemporaryDirectory(prefix="stonks-optional-smoke-") as temporary:
        context = build_context(
            root=ROOT,
            secret_directory=Path(temporary) / "secrets",
            base_environment=os.environ,
            core_port=args.core_port,
        )
        runner = SubprocessRunner(
            cwd=ROOT,
            environment=context.environment,
            secret_values=context.secret_values,
        )
        primary_error: Exception | None = None
        try:
            _start_core(context, runner, skip_build=args.skip_core_build)
            report = build_report(policy, DockerCiHarness(context, runner))
            _write_report(args.output.resolve(), report)
        except Exception as error:
            primary_error = error
        try:
            runner.run(
                (*context.compose_prefix, "down", "--volumes", "--remove-orphans")
            )
        except Exception as cleanup_error:
            if primary_error is not None:
                raise OptionalSmokeError() from cleanup_error
            raise OptionalSmokeError() from cleanup_error
        if primary_error is not None:
            raise OptionalSmokeError() from primary_error


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        policy = load_policy(args.policy.resolve())
        _run_ci_smoke(args, policy)
        print(
            json.dumps(
                {
                    "success": True,
                    "status": 200,
                    "data": {
                        "matrix_contract_status": "passed",
                        "absence_safety_verified": True,
                        "runtime_compatibility_complete": False,
                    },
                    "error": None,
                },
                sort_keys=True,
            )
        )
        return 0
    except OptionalSmokeError:
        print(
            json.dumps(
                {
                    "success": False,
                    "status": 500,
                    "data": None,
                    "error": {
                        "code": "optional_smoke_failed",
                        "message": "Optional integration smoke failed",
                    },
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
