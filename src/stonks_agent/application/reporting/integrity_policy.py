"""Deterministic citation, quality, and authority checks for report drafts."""

from __future__ import annotations

import re
from uuid import UUID, uuid5

from stonks_agent.domain.analysis_context import AnalysisContext
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.report import ReportDraft
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.market_data import DataQualityStatus
from stonks_contracts.report import ClaimCertainty, ReportClaim

ACTION_GUARDRAILS = (
    "research_only_no_execution_authority",
    "paper_only_execution_mode",
    "deterministic_portfolio_and_risk_required",
)

_FORBIDDEN_AUTHORITY = re.compile(
    r"(?:\b(?:buy|sell|short|cover|execute|place)\b.{0,32}"
    r"\b(?:shares?|units?|orders?)\b)|"
    r"\b(?:order_quantity|portfolio[_ -]?target|risk[_ -]?override)\b",
    re.IGNORECASE,
)
_QUALITY_PRIORITY = {
    DataQualityStatus.AVAILABLE: 0,
    DataQualityStatus.FALLBACK: 1,
    DataQualityStatus.ESTIMATED: 2,
    DataQualityStatus.STALE: 3,
    DataQualityStatus.PARTIAL: 4,
    DataQualityStatus.MISSING: 5,
    DataQualityStatus.NOT_SUPPORTED: 5,
    DataQualityStatus.FETCH_FAILED: 6,
    DataQualityStatus.CONFLICT: 7,
}


def validate_report_draft(
    report_id: UUID,
    context: AnalysisContext,
    draft: ReportDraft,
) -> Result[tuple[ReportClaim, ...]]:
    known_ids = {item.evidence_id for item in context.evidence}
    quality_by_id = _quality_by_evidence(context)
    claims: list[ReportClaim] = []
    for index, claim in enumerate(draft.claims):
        if _FORBIDDEN_AUTHORITY.search(claim.assertion):
            return _invalid("claim_contains_execution_authority")
        refs = set(claim.evidence_refs)
        if not refs <= known_ids:
            return _invalid("claim_cites_unknown_evidence")
        if claim.certainty is ClaimCertainty.HYPOTHESIS:
            if refs or claim.data_quality is not None:
                return _invalid("hypothesis_has_fact_metadata")
        else:
            if not refs or claim.data_quality is None:
                return _invalid("claim_missing_citation")
            effective = max((quality_by_id[item] for item in refs), key=_quality_rank)
            if claim.data_quality is not effective:
                return _invalid("claim_quality_mismatch")
            expected = (
                ClaimCertainty.OBSERVED
                if effective is DataQualityStatus.AVAILABLE
                else ClaimCertainty.QUALIFIED
            )
            if claim.certainty is not expected:
                return _invalid("claim_certainty_mismatch")
        claims.append(
            ReportClaim(
                claim_id=uuid5(
                    report_id,
                    f"claim:{index}:{stable_payload_hash(claim.model_dump(mode='json'))}",
                ),
                assertion=claim.assertion,
                certainty=claim.certainty,
                data_quality=claim.data_quality,
                evidence_refs=tuple(sorted(refs, key=str)),
            )
        )
    return Success(tuple(claims))


def _quality_by_evidence(context: AnalysisContext) -> dict[UUID, DataQualityStatus]:
    result: dict[UUID, DataQualityStatus] = {}
    for block in context.blocks:
        for evidence_id in block.evidence_refs:
            current = result.get(evidence_id)
            if current is None or _quality_rank(block.status) > _quality_rank(current):
                result[evidence_id] = block.status
    return result


def _quality_rank(status: DataQualityStatus) -> int:
    return _QUALITY_PRIORITY[status]


def _invalid(reason: str) -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.MODEL_OUTPUT_INVALID,
            message="Report draft failed integrity policy",
            details={"reason": reason},
        )
    )
