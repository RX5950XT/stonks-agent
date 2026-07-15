from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_contracts.candidate_scan import scan_candidate_source
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.rd_agent import (
    REQUIRED_EVALUATION_CHECKS,
    CandidateSandboxPolicy,
    CandidateScanResult,
    DraftArtifact,
    DraftArtifactKind,
    DraftEvaluationRequest,
    RDAgentCandidateKind,
    RDAgentProposal,
    RDSandboxDataset,
    RDSandboxDatasetRow,
    RDSandboxJob,
    RDSandboxResult,
    RDSandboxRunResponse,
    RDSandboxRunResult,
    RDSandboxRuntimeIdentity,
    RDSandboxWorkerResponse,
    SandboxPrediction,
    sandbox_prediction_byte_count,
)

NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
COMMIT = "4f9ecb005881cddc08df0124a2e894c018007679"
SAFE_SOURCE = """def compute(rows):
    return [
        {"observation_id": row["observation_id"], "predicted_return": row["features"][0]}
        for row in rows
    ]
"""


def policy() -> CandidateSandboxPolicy:
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


def dataset() -> RDSandboxDataset:
    instrument_id = UUID("40000000-0000-4000-8000-000000000001")
    return RDSandboxDataset(
        dataset_snapshot_id=UUID("40000000-0000-4000-8000-000000000002"),
        source_data_hash=HASH_A,
        as_of=NOW,
        feature_spec_hash=HASH_A,
        label_spec_hash=HASH_B,
        universe_spec_hash=HASH_C,
        cost_model_hash=HASH_A,
        split_policy_hash=HASH_B,
        rows=tuple(
            RDSandboxDatasetRow(
                observation_id=UUID(f"40000000-0000-4000-9000-{index:012d}"),
                instrument_id=instrument_id,
                event_at=NOW - timedelta(days=4 - index),
                feature_available_at=NOW - timedelta(days=4 - index, minutes=-1),
                prediction_at=NOW - timedelta(days=3 - index),
                features=(Decimal(f"0.0{index}"), Decimal("1")),
            )
            for index in range(1, 4)
        ),
    )


def runtime() -> RDSandboxRuntimeIdentity:
    return RDSandboxRuntimeIdentity(
        worker_version="0.1.0",
        adapter_version="0.1.0",
        rd_agent_commit=COMMIT,
        rd_agent_source_hash=HASH_A,
        runtime_hash=HASH_B,
        image_digest=f"sha256:{HASH_C}",
        python_version="3.12.9",
        deterministic=True,
    )


def proposal() -> RDAgentProposal:
    return RDAgentProposal.create(
        proposal_id=UUID("40000000-0000-4000-8000-000000000003"),
        candidate_id="rd-factor-momentum/1.0.0",
        candidate_kind=RDAgentCandidateKind.FACTOR,
        rd_agent_commit=COMMIT,
        generation_runtime_hash=HASH_A,
        generation_config_hash=HASH_B,
        generation_input_artifact_ref=f"sha256:{HASH_C}",
        raw_generation_artifact_ref=f"sha256:{HASH_A}",
        source=SAFE_SOURCE,
        generated_at=NOW + timedelta(minutes=1),
    )


def job() -> RDSandboxJob:
    selected_dataset = dataset()
    active = policy()
    return RDSandboxJob(
        request_id=UUID("40000000-0000-4000-8000-000000000004"),
        run_id=UUID("40000000-0000-4000-8000-000000000005"),
        job_id=UUID("40000000-0000-4000-8000-000000000006"),
        attempt_generation=1,
        attempt_nonce="rd-agent-attempt-1",
        execution_mode="sandbox",
        proposal=proposal(),
        dataset_artifact_ref=f"sha256:{selected_dataset.payload_hash()}",
        dataset=selected_dataset,
        evaluation_policy_hash=HASH_C,
        sandbox_policy_hash=active.policy_hash,
        runtime=runtime(),
        requested_at=NOW + timedelta(minutes=2),
        deadline=NOW + timedelta(minutes=10),
        promotion_allowed=False,
    )


def response(target: RDSandboxJob | None = None) -> RDSandboxWorkerResponse:
    selected = target or job()
    active = policy()
    scan = scan_candidate_source(selected.proposal.source, active)
    predictions = tuple(
        SandboxPrediction(
            observation_id=row.observation_id,
            predicted_return=row.features[0],
        )
        for row in selected.dataset.rows
    )
    prediction_hash = stable_payload_hash(
        [item.model_dump(mode="json") for item in predictions]
    )
    draft = DraftEvaluationRequest(
        candidate_id=selected.proposal.candidate_id,
        source_hash=selected.proposal.source_hash,
        prediction_hash=prediction_hash,
        dataset_snapshot_id=selected.dataset.dataset_snapshot_id,
        source_data_hash=selected.dataset.source_data_hash,
        as_of=selected.dataset.as_of,
        feature_spec_hash=selected.dataset.feature_spec_hash,
        label_spec_hash=selected.dataset.label_spec_hash,
        universe_spec_hash=selected.dataset.universe_spec_hash,
        cost_model_hash=selected.dataset.cost_model_hash,
        split_policy_hash=selected.dataset.split_policy_hash,
        evaluation_policy_hash=selected.evaluation_policy_hash,
        required_checks=REQUIRED_EVALUATION_CHECKS,
        core_full_evaluation_required=True,
        promotion_allowed=False,
    )
    result = RDSandboxResult(
        proposal_id=selected.proposal.proposal_id,
        candidate_id=selected.proposal.candidate_id,
        runtime=selected.runtime,
        sandbox_policy_hash=active.policy_hash,
        scan=scan,
        source_artifact=DraftArtifact(
            kind=DraftArtifactKind.SOURCE,
            content_hash=selected.proposal.source_hash,
            byte_count=len(selected.proposal.source.encode("utf-8")),
            media_type="text/x-python",
            draft_only=True,
        ),
        prediction_artifact=DraftArtifact(
            kind=DraftArtifactKind.PREDICTIONS,
            content_hash=prediction_hash,
            byte_count=sandbox_prediction_byte_count(predictions),
            media_type="application/json",
            draft_only=True,
        ),
        predictions=predictions,
        first_sandbox_instance_id=UUID("40000000-0000-4000-8000-000000000007"),
        replay_sandbox_instance_id=UUID("40000000-0000-4000-8000-000000000008"),
        first_output_hash=prediction_hash,
        replay_output_hash=prediction_hash,
        draft_evaluation_request=draft,
        deterministic=True,
        generated_at=selected.requested_at + timedelta(minutes=1),
    )
    return RDSandboxWorkerResponse(
        request_id=selected.request_id,
        run_id=selected.run_id,
        job_id=selected.job_id,
        attempt_generation=selected.attempt_generation,
        attempt_nonce=selected.attempt_nonce,
        result_artifact_hash=result.payload_hash(),
        result=result,
    )


def run_response(
    sandbox_instance_id: UUID,
    target: RDSandboxJob | None = None,
) -> RDSandboxRunResponse:
    selected = target or job()
    active = policy()
    scan = scan_candidate_source(selected.proposal.source, active)
    predictions = tuple(
        SandboxPrediction(
            observation_id=row.observation_id,
            predicted_return=row.features[0],
        )
        for row in selected.dataset.rows
    )
    prediction_hash = stable_payload_hash(
        [item.model_dump(mode="json") for item in predictions]
    )
    result = RDSandboxRunResult(
        sandbox_instance_id=sandbox_instance_id,
        proposal_id=selected.proposal.proposal_id,
        candidate_id=selected.proposal.candidate_id,
        runtime=selected.runtime,
        sandbox_policy_hash=active.policy_hash,
        scan=scan,
        source_artifact=DraftArtifact(
            kind=DraftArtifactKind.SOURCE,
            content_hash=selected.proposal.source_hash,
            byte_count=len(selected.proposal.source.encode("utf-8")),
            media_type="text/x-python",
            draft_only=True,
        ),
        prediction_artifact=DraftArtifact(
            kind=DraftArtifactKind.PREDICTIONS,
            content_hash=prediction_hash,
            byte_count=sandbox_prediction_byte_count(predictions),
            media_type="application/json",
            draft_only=True,
        ),
        predictions=predictions,
        output_hash=prediction_hash,
        process_isolation="fresh_container",
        generated_at=selected.requested_at + timedelta(minutes=1),
    )
    return RDSandboxRunResponse(
        request_id=selected.request_id,
        run_id=selected.run_id,
        job_id=selected.job_id,
        attempt_generation=selected.attempt_generation,
        attempt_nonce=selected.attempt_nonce,
        result_artifact_hash=result.payload_hash(),
        result=result,
    )


def test_proposal_policy_job_and_result_are_hash_bound_and_frozen() -> None:
    active = policy()
    selected = job()
    outcome = response(selected)

    assert selected.proposal.source_artifact_ref == (
        f"sha256:{selected.proposal.source_hash}"
    )
    assert selected.sandbox_policy_hash == active.policy_hash
    outcome.validate_against(selected)
    assert outcome.result.first_output_hash == outcome.result.replay_output_hash
    assert outcome.result.draft_evaluation_request.required_checks == (
        REQUIRED_EVALUATION_CHECKS
    )
    with pytest.raises(ValidationError):
        active.promotion_allowed = True  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RDSandboxJob.model_validate(
            selected.model_dump(mode="json") | {"order_intent": {}}
        )


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef compute(rows):\n    return rows\n",
        "def compute(rows):\n    return open('/etc/passwd').read()\n",
        "def compute(rows):\n    return rows[0].__class__.__mro__\n",
        "def compute(rows):\n    return eval(rows[0]['features'][0])\n",
        "def other(rows):\n    return rows\n",
        "def compute(rows):\n    def len(value):\n        return value\n    return len(rows)\n",
        "def compute(rows):\n    rows[0]['features'] = []\n    return rows\n",
        "def compute(rows):\n    while True:\n        pass\n    return rows\n",
        "def compute(rows):\n    return rows.copy()\n",
    ],
)
def test_static_scan_rejects_escape_surfaces(source: str) -> None:
    with pytest.raises(ValueError, match="candidate source rejected"):
        scan_candidate_source(source, policy())


def test_static_scan_is_deterministic_and_tamper_evident() -> None:
    first = scan_candidate_source(SAFE_SOURCE, policy())
    second = scan_candidate_source(SAFE_SOURCE, policy())

    assert isinstance(first, CandidateScanResult)
    assert first == second
    assert first.scan_hash == second.scan_hash
    with pytest.raises(ValidationError, match="scan hash"):
        CandidateScanResult.model_validate(
            first.model_dump(mode="json") | {"scan_hash": HASH_C}
        )


def test_proposal_artifact_binds_generation_provenance_and_source() -> None:
    original = proposal()

    assert original.proposal_artifact_ref == (
        f"sha256:{original.proposal_payload_hash}"
    )
    with pytest.raises(ValidationError, match="proposal artifact"):
        RDAgentProposal.model_validate(
            original.model_dump(mode="json") | {"generation_runtime_hash": HASH_C}
        )


def test_result_rejects_replay_and_fence_drift() -> None:
    selected = job()
    original = response(selected)
    drifted_result = original.result.model_copy(update={"replay_output_hash": HASH_C})

    with pytest.raises(ValidationError, match="reproducible"):
        RDSandboxResult.model_validate(drifted_result.model_dump(mode="json"))
    with pytest.raises(ValueError, match="fence"):
        original.model_copy(update={"attempt_nonce": "stale"}).validate_against(
            selected
        )


def test_one_shot_run_receipt_is_fenced_and_distinct_instances_are_required() -> None:
    selected = job()
    first = run_response(UUID("40000000-0000-4000-8000-000000000007"), selected)

    first.validate_against(selected)
    with pytest.raises(ValidationError, match="reproducible"):
        RDSandboxResult.model_validate(
            response(selected).result.model_dump(mode="json")
            | {"replay_sandbox_instance_id": str(first.result.sandbox_instance_id)}
        )
