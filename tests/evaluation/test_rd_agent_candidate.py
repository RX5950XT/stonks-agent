from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from stonks_agent.application.evaluation.rd_agent import (
    aggregate_sandbox_runs,
    evaluate_rd_agent_candidate,
)
from stonks_agent.domain.errors import Failure, Result, Success
from stonks_agent.domain.evaluation import EvaluationRequest
from stonks_agent.domain.strategy import StrategyKind
from stonks_contracts.candidate_scan import scan_candidate_source
from stonks_contracts.rd_agent import (
    CandidateSandboxPolicy,
    DraftArtifact,
    DraftArtifactKind,
    RDAgentCandidateKind,
    RDAgentProposal,
    RDSandboxDataset,
    RDSandboxDatasetRow,
    RDSandboxJob,
    RDSandboxRunResponse,
    RDSandboxRunResult,
    RDSandboxRuntimeIdentity,
    RDSandboxWorkerResponse,
    SandboxPrediction,
    sandbox_prediction_byte_count,
    sandbox_prediction_hash,
)

from .helpers import HASH_A, HASH_B, HASH_C, NOW, baselines, dataset, policy, request

COMMIT = "4f9ecb005881cddc08df0124a2e894c018007679"
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


def proposal() -> RDAgentProposal:
    return RDAgentProposal.create(
        proposal_id=UUID("60000000-0000-4000-8000-000000000001"),
        candidate_id="candidate-alpha/1.0.0",
        candidate_kind=RDAgentCandidateKind.FACTOR,
        rd_agent_commit=COMMIT,
        generation_runtime_hash=HASH_A,
        generation_config_hash=HASH_B,
        generation_input_artifact_ref=f"sha256:{HASH_C}",
        raw_generation_artifact_ref=f"sha256:{HASH_A}",
        source=SAFE_SOURCE,
        generated_at=NOW,
    )


def sandbox_dataset() -> RDSandboxDataset:
    evaluation = dataset()
    return RDSandboxDataset(
        dataset_snapshot_id=evaluation.dataset_snapshot_id,
        source_data_hash=evaluation.data_hash,
        as_of=evaluation.as_of,
        feature_spec_hash=HASH_C,
        label_spec_hash="d" * 64,
        universe_spec_hash="e" * 64,
        cost_model_hash=HASH_A,
        split_policy_hash=HASH_B,
        rows=tuple(
            RDSandboxDatasetRow(
                observation_id=value.observation_id,
                instrument_id=value.instrument_id,
                event_at=value.event_at,
                feature_available_at=value.feature_available_at,
                prediction_at=value.prediction_at,
                features=(value.predicted_return, Decimal("1")),
            )
            for value in evaluation.observations
        ),
    )


def job() -> RDSandboxJob:
    selected_dataset = sandbox_dataset()
    active = sandbox_policy()
    return RDSandboxJob(
        request_id=UUID("60000000-0000-4000-8000-000000000002"),
        run_id=UUID("60000000-0000-4000-8000-000000000003"),
        job_id=UUID("60000000-0000-4000-8000-000000000004"),
        attempt_generation=1,
        attempt_nonce="rd-agent-attempt-1",
        execution_mode="sandbox",
        proposal=proposal(),
        dataset_artifact_ref=f"sha256:{selected_dataset.payload_hash()}",
        dataset=selected_dataset,
        evaluation_policy_hash=policy().policy_hash,
        sandbox_policy_hash=active.policy_hash,
        runtime=runtime(),
        requested_at=NOW + timedelta(minutes=1),
        deadline=NOW + timedelta(minutes=9),
        promotion_allowed=False,
    )


def run_response(instance: int, *, invert: bool = False) -> RDSandboxRunResponse:
    selected = job()
    active = sandbox_policy()
    predictions = tuple(
        SandboxPrediction(
            observation_id=row.observation_id,
            predicted_return=(-row.features[0] if invert else row.features[0]),
        )
        for row in selected.dataset.rows
    )
    prediction_hash = sandbox_prediction_hash(predictions)
    result = RDSandboxRunResult(
        sandbox_instance_id=UUID(f"60000000-0000-4000-9000-{instance:012d}"),
        proposal_id=selected.proposal.proposal_id,
        candidate_id=selected.proposal.candidate_id,
        runtime=selected.runtime,
        sandbox_policy_hash=active.policy_hash,
        scan=scan_candidate_source(selected.proposal.source, active),
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
        generated_at=selected.requested_at + timedelta(minutes=instance),
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


def evaluation_request() -> EvaluationRequest:
    base = request()
    candidate = proposal()
    manifest = base.manifest.model_copy(
        update={
            "kind": StrategyKind.QUANT_MODEL,
            "source_artifact_ref": candidate.source_artifact_ref,
            "parameters_hash": candidate.proposal_payload_hash,
        }
    )
    return base.model_copy(
        update={
            "manifest": manifest,
            "requested_at": NOW + timedelta(minutes=2),
        }
    )


def aggregate() -> Result[RDSandboxWorkerResponse]:
    return aggregate_sandbox_runs(
        job=job(),
        first=run_response(1),
        replay=run_response(2),
        sandbox_policy=sandbox_policy(),
    )


def test_two_fresh_sandboxes_are_aggregated_then_fully_evaluated() -> None:
    combined = aggregate()

    assert isinstance(combined, Success)
    result = evaluate_rd_agent_candidate(
        job=job(),
        response=combined.value,
        sandbox_policy=sandbox_policy(),
        request=evaluation_request(),
        dataset=dataset(),
        baselines=baselines(),
        policy=policy(),
        report_id=UUID("60000000-0000-4000-8000-000000000005"),
        report_artifact_ref=f"sha256:{HASH_A}",
        created_at=NOW + timedelta(minutes=3),
    )

    assert isinstance(result, Success)
    assert result.value.passed is True
    assert result.value.strategy_id == "candidate-alpha"
    assert result.value.runtime_hash == runtime().runtime_hash


def test_non_reproducible_or_same_instance_runs_fail_before_evaluation() -> None:
    non_reproducible = aggregate_sandbox_runs(
        job=job(),
        first=run_response(1),
        replay=run_response(2, invert=True),
        sandbox_policy=sandbox_policy(),
    )
    same_instance = aggregate_sandbox_runs(
        job=job(),
        first=run_response(1),
        replay=run_response(1),
        sandbox_policy=sandbox_policy(),
    )

    assert isinstance(non_reproducible, Failure)
    assert isinstance(same_instance, Failure)


def test_core_rejects_manifest_policy_or_dataset_binding_drift() -> None:
    combined = aggregate()
    assert isinstance(combined, Success)
    changed = evaluation_request().model_copy(
        update={
            "manifest": evaluation_request().manifest.model_copy(
                update={"parameters_hash": HASH_C}
            )
        }
    )

    result = evaluate_rd_agent_candidate(
        job=job(),
        response=combined.value,
        sandbox_policy=sandbox_policy(),
        request=changed,
        dataset=dataset(),
        baselines=baselines(),
        policy=policy(),
        report_id=UUID("60000000-0000-4000-8000-000000000005"),
        report_artifact_ref=f"sha256:{HASH_A}",
        created_at=NOW + timedelta(minutes=3),
    )

    assert isinstance(result, Failure)
