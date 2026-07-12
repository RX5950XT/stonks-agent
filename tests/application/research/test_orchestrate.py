from __future__ import annotations

from uuid import uuid4

from stonks_agent.adapters.research.deterministic import (
    DeterministicResearchArtifactBuilder,
)
from stonks_agent.application.research.orchestrate import orchestrate_research
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.research import ResearchClaimKind

from .helpers import (
    NOW,
    DictArtifactReader,
    RecordingTool,
    ScriptedLLM,
    evidence,
    request,
)
from .test_tool_loop import final_turn, policy, principal


def test_orchestrator_builds_deterministic_artifact_and_downgrades_uncited_claim() -> (
    None
):
    reader = DictArtifactReader({"a" * 64: b'{"source":"filing"}'})

    first = orchestrate_research(
        request=request(),
        evidence_items=(evidence(),),
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([final_turn()]),
        tool=RecordingTool(),
        artifacts=reader,
        builder=DeterministicResearchArtifactBuilder(),
        clock=lambda: NOW,
    )
    second = orchestrate_research(
        request=request(),
        evidence_items=(evidence(),),
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([final_turn()]),
        tool=RecordingTool(),
        artifacts=reader,
        builder=DeterministicResearchArtifactBuilder(),
        clock=lambda: NOW,
    )

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value.artifact_id == second.value.artifact_id
    assert first.value.claims[0].kind is ResearchClaimKind.EVIDENCED
    assert first.value.claims[1].kind is ResearchClaimKind.HYPOTHESIS
    assert first.value.raw_output_artifact_ref == f"sha256:{'c' * 64}"


def test_final_draft_cannot_cite_evidence_outside_request_scope() -> None:
    reader = DictArtifactReader({"a" * 64: b'{"source":"filing"}'})
    unsafe = final_turn()
    claim = unsafe["claims"][0]  # type: ignore[index]
    assert isinstance(claim, dict)
    claim["evidence_ids"] = [str(uuid4())]

    result = orchestrate_research(
        request=request(),
        evidence_items=(evidence(),),
        principal=principal(),
        policy=policy(),
        llm=ScriptedLLM([unsafe]),
        tool=RecordingTool(),
        artifacts=reader,
        builder=DeterministicResearchArtifactBuilder(),
        clock=lambda: NOW,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID
