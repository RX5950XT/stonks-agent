"""Deterministic mapping from a validated final draft to a research artifact."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid5

from pydantic import ValidationError

from stonks_agent.application.research.tool_loop import ResearchLoopResult
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.research import (
    ResearchArtifact,
    ResearchClaim,
    ResearchClaimKind,
    ResearchRequest,
)
from stonks_contracts.common import stable_payload_hash


class DeterministicResearchArtifactBuilder:
    def build(
        self,
        request: ResearchRequest,
        result: ResearchLoopResult,
        *,
        created_at: datetime,
    ) -> Result[ResearchArtifact]:
        draft = result.draft
        assert draft.confidence is not None
        claims = tuple(
            ResearchClaim(
                claim_id=uuid5(
                    request.request_id, f"claim:{index}:{stable_payload_hash(claim)}"
                ),
                kind=(
                    ResearchClaimKind.EVIDENCED
                    if claim.evidence_ids
                    else ResearchClaimKind.HYPOTHESIS
                ),
                text=claim.text,
                evidence_ids=claim.evidence_ids,
            )
            for index, claim in enumerate(draft.claims)
        )
        identity = stable_payload_hash(
            {
                "request_id": str(request.request_id),
                "claims": tuple(claim.model_dump(mode="json") for claim in claims),
                "confidence": str(draft.confidence),
                "raw_output": result.raw_output_artifact_ref,
                "tool_outputs": result.tool_output_artifact_refs,
            }
        )
        try:
            artifact = ResearchArtifact(
                artifact_id=uuid5(request.request_id, identity),
                request_id=request.request_id,
                run_id=request.run_id,
                instrument_ids=request.instrument_ids,
                as_of=request.as_of,
                allowed_evidence_ids=request.allowed_evidence_ids,
                claims=claims,
                counterarguments=draft.counterarguments,
                risks=draft.risks,
                warnings=draft.warnings,
                confidence=draft.confidence,
                raw_output_artifact_ref=result.raw_output_artifact_ref,
                tool_output_artifact_refs=result.tool_output_artifact_refs,
                producer="bounded-research-orchestrator",
                producer_version="1.0.0",
                model_versions=result.model_versions,
                tool_versions=result.tool_versions,
                usage=result.usage,
                created_at=created_at,
            )
        except ValidationError:
            return Failure(
                StructuredError(
                    code=ErrorCode.MODEL_OUTPUT_INVALID,
                    message="Final research draft violates artifact policy",
                )
            )
        return Success(artifact)
