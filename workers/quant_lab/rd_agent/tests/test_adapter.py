from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from stonks_contracts.common import stable_payload_hash
from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    RDAgentCandidateKind,
    RDAgentProposal,
    RDSandboxDataset,
    RDSandboxDatasetRow,
    RDSandboxJob,
    RDSandboxRuntimeIdentity,
    SandboxPrediction,
)
from workers.quant_lab.rd_agent.adapter import (
    CandidateProcessError,
    CandidateRunOutput,
    RDAgentSandboxWorker,
    SandboxWorkerPolicy,
    WorkerFailure,
    WorkerSuccess,
)

NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
COMMIT = "4f9ecb005881cddc08df0124a2e894c018007679"
INSTANCE_ID = UUID("50000000-0000-4000-8000-000000000010")
SAFE_SOURCE = """def compute(rows):
    return [
        {"observation_id": row["observation_id"], "predicted_return": row["features"][0]}
        for row in rows
    ]
"""


def sandbox_policy() -> CandidateSandboxPolicy:
    return CandidateSandboxPolicy(
        policy_id="rd-agent-sandbox/1",
        platform="linux",
        network_mode="none",
        root_filesystem="read_only",
        dataset_mount="read_only",
        entrypoint="compute",
        repetitions=2,
        max_source_bytes=65_536,
        max_ast_nodes=4_096,
        max_rows=10_000,
        max_output_bytes=2_000_000,
        timeout_seconds=5,
        memory_megabytes=256,
        cpu_cores=Decimal("1"),
        max_processes=1,
        max_open_files=16,
        max_log_bytes=65_536,
        writable_tmpfs_megabytes=32,
        run_as_uid=65532,
        run_as_gid=65532,
        privileged=False,
        capability_mode="drop_all",
        no_new_privileges=True,
        seccomp_profile="runtime/default",
        apparmor_profile="docker-default",
        device_access="none",
        host_namespace_mode="private",
        unix_socket_access="none",
        environment_mode="allowlist",
        source_mount="read_only",
        output_tmpfs_options="rw,noexec,nosuid,nodev",
        fixed_argv=True,
        shell_allowed=False,
        isolation_scope="fresh_container_per_repetition",
        python_hash_seed=0,
        allowed_calls=(
            "abs",
            "all",
            "any",
            "bool",
            "dict",
            "enumerate",
            "float",
            "int",
            "len",
            "list",
            "max",
            "min",
            "range",
            "round",
            "sorted",
            "str",
            "sum",
            "tuple",
            "zip",
        ),
        forbidden_names=(
            "__import__",
            "breakpoint",
            "compile",
            "eval",
            "exec",
            "getattr",
            "globals",
            "input",
            "locals",
            "open",
            "setattr",
            "vars",
        ),
        core_static_rescan_required=True,
        core_full_evaluation_required=True,
        promotion_allowed=False,
    )


def runtime() -> RDSandboxRuntimeIdentity:
    return RDSandboxRuntimeIdentity(
        worker_version="rd-agent-factor-sandbox/0.1.0",
        adapter_version="factor-expression-v1",
        rd_agent_commit=COMMIT,
        rd_agent_source_hash=HASH_A,
        runtime_hash=HASH_B,
        image_digest=f"sha256:{HASH_C}",
        python_version="3.12.9",
        deterministic=True,
    )


def dataset() -> RDSandboxDataset:
    return RDSandboxDataset(
        dataset_snapshot_id=UUID("50000000-0000-4000-8000-000000000001"),
        source_data_hash=HASH_A,
        as_of=NOW,
        feature_spec_hash=HASH_A,
        label_spec_hash=HASH_B,
        universe_spec_hash=HASH_C,
        cost_model_hash=HASH_A,
        split_policy_hash=HASH_B,
        rows=tuple(
            RDSandboxDatasetRow(
                observation_id=UUID(f"50000000-0000-4000-9000-{index:012d}"),
                instrument_id=UUID("50000000-0000-4000-8000-000000000002"),
                event_at=NOW - timedelta(days=5 - index),
                feature_available_at=NOW - timedelta(days=5 - index, minutes=-1),
                prediction_at=NOW - timedelta(days=4 - index),
                features=(Decimal(f"0.0{index}"), Decimal("1")),
            )
            for index in range(1, 4)
        ),
    )


def proposal(source: str = SAFE_SOURCE) -> RDAgentProposal:
    return RDAgentProposal.create(
        proposal_id=UUID("50000000-0000-4000-8000-000000000003"),
        candidate_id="rd-factor-momentum/1.0.0",
        candidate_kind=RDAgentCandidateKind.FACTOR,
        rd_agent_commit=COMMIT,
        generation_runtime_hash=HASH_A,
        generation_config_hash=HASH_B,
        generation_input_artifact_ref=f"sha256:{HASH_C}",
        raw_generation_artifact_ref=f"sha256:{HASH_A}",
        source=source,
        generated_at=NOW + timedelta(minutes=1),
    )


def job(**changes: object) -> RDSandboxJob:
    selected_dataset = dataset()
    active = sandbox_policy()
    payload: dict[str, object] = {
        "request_id": UUID("50000000-0000-4000-8000-000000000004"),
        "run_id": UUID("50000000-0000-4000-8000-000000000005"),
        "job_id": UUID("50000000-0000-4000-8000-000000000006"),
        "attempt_generation": 1,
        "attempt_nonce": "rd-agent-attempt-1",
        "execution_mode": "sandbox",
        "proposal": proposal(),
        "dataset_artifact_ref": f"sha256:{selected_dataset.payload_hash()}",
        "dataset": selected_dataset,
        "evaluation_policy_hash": HASH_C,
        "sandbox_policy_hash": active.policy_hash,
        "runtime": runtime(),
        "requested_at": NOW + timedelta(minutes=2),
        "deadline": NOW + timedelta(minutes=10),
        "promotion_allowed": False,
    }
    return RDSandboxJob.model_validate(payload | changes)


class FakeRunner:
    def __init__(self, *, error: CandidateProcessError | None = None) -> None:
        self.error = error
        self.calls = 0
        self.seen_rows: tuple[RDSandboxDatasetRow, ...] = ()

    def run(
        self,
        *,
        source: str,
        rows: tuple[RDSandboxDatasetRow, ...],
        policy: CandidateSandboxPolicy,
    ) -> CandidateRunOutput:
        self.calls += 1
        self.seen_rows = rows
        if self.error is not None:
            raise self.error
        return CandidateRunOutput(
            predictions=tuple(
                SandboxPrediction(
                    observation_id=row.observation_id,
                    predicted_return=row.features[0],
                )
                for row in rows
            )
        )


def worker(
    runner: FakeRunner,
    *,
    now: datetime = NOW + timedelta(minutes=3),
) -> RDAgentSandboxWorker:
    return RDAgentSandboxWorker(
        policy=SandboxWorkerPolicy(
            runtime=runtime(),
            sandbox=sandbox_policy(),
        ),
        runner=runner,
        clock=lambda: now,
        platform_name=lambda: "Linux",
    )


def test_one_shot_worker_returns_fenced_draft_run_without_labels() -> None:
    runner = FakeRunner()
    selected = job()

    outcome = worker(runner).run(selected, sandbox_instance_id=INSTANCE_ID)

    assert isinstance(outcome, WorkerSuccess)
    outcome.value.validate_against(selected)
    assert outcome.value.result.sandbox_instance_id == INSTANCE_ID
    assert outcome.value.result.output_hash == stable_payload_hash(
        [item.model_dump(mode="json") for item in outcome.value.result.predictions]
    )
    assert all(not hasattr(row, "label") for row in runner.seen_rows)
    assert all(not hasattr(row, "actual_return") for row in runner.seen_rows)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"sandbox_policy_hash": HASH_A}, "sandbox_policy_mismatch"),
        (
            {"runtime": runtime().model_copy(update={"runtime_hash": HASH_A})},
            "runtime_mismatch",
        ),
    ],
)
def test_worker_rejects_policy_or_runtime_drift_before_execution(
    change: dict[str, object], code: str
) -> None:
    runner = FakeRunner()

    outcome = worker(runner).run(job(**change), sandbox_instance_id=INSTANCE_ID)

    assert isinstance(outcome, WorkerFailure)
    assert outcome.error.code == code
    assert runner.calls == 0


def test_expired_job_and_rejected_source_never_reach_runner() -> None:
    runner = FakeRunner()
    expired = worker(runner, now=NOW + timedelta(minutes=11)).run(
        job(), sandbox_instance_id=INSTANCE_ID
    )
    bad = job(proposal=proposal("def compute(rows):\n    return open('/secret')\n"))
    rejected = worker(runner).run(bad, sandbox_instance_id=INSTANCE_ID)

    assert isinstance(expired, WorkerFailure)
    assert expired.error.code == "deadline_expired"
    assert isinstance(rejected, WorkerFailure)
    assert rejected.error.code == "candidate_rejected"
    assert runner.calls == 0


def test_runner_failure_is_structured_and_does_not_echo_sensitive_detail() -> None:
    secret = "provider-token-should-not-leak"
    runner = FakeRunner(error=CandidateProcessError("candidate_timeout", secret))

    outcome = worker(runner).run(job(), sandbox_instance_id=INSTANCE_ID)

    assert isinstance(outcome, WorkerFailure)
    assert outcome.error.code == "candidate_timeout"
    assert secret not in outcome.error.message


def test_misaligned_output_is_rejected_without_receipt() -> None:
    class MisalignedRunner(FakeRunner):
        def run(
            self,
            *,
            source: str,
            rows: tuple[RDSandboxDatasetRow, ...],
            policy: CandidateSandboxPolicy,
        ) -> CandidateRunOutput:
            output = super().run(source=source, rows=rows, policy=policy)
            return output.model_copy(
                update={"predictions": tuple(reversed(output.predictions))}
            )

    outcome = worker(MisalignedRunner()).run(job(), sandbox_instance_id=INSTANCE_ID)

    assert isinstance(outcome, WorkerFailure)
    assert outcome.error.code == "candidate_output_invalid"
