"""Read-only assembler from canonical evidence into an analysis context."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from stonks_agent.domain.analysis_context import (
    AnalysisContext,
    AnalysisContextRequest,
    EvidenceBlock,
    EvidenceRequirement,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.evidence_repository import EvidenceRepository
from stonks_contracts.evidence import EvidenceItem
from stonks_contracts.market_data import DataQualityStatus


def assemble_evidence_context(
    request: AnalysisContextRequest,
    repository: EvidenceRepository,
) -> Result[AnalysisContext]:
    queried = repository.query_available(subject=request.subject, as_of=request.as_of)
    if isinstance(queried, Failure):
        return queried
    validated = _validate_repository_output(request, queried.value)
    if isinstance(validated, Failure):
        return validated
    allowed, excluded = _apply_content_policy(request, validated.value)
    blocks = tuple(
        _build_block(requirement, request, allowed)
        for requirement in sorted(
            request.requirements, key=lambda item: item.capability
        )
    )
    selected_ids = {
        evidence_id for block in blocks for evidence_id in block.evidence_refs
    }
    evidence = tuple(
        sorted(
            (item for item in allowed if item.evidence_id in selected_ids),
            key=lambda item: (item.available_at, str(item.evidence_id)),
        )
    )
    limitations = _limitations(request, blocks, excluded)
    return Success(
        AnalysisContext(
            context_id=request.context_id,
            run_id=request.run_id,
            subject=request.subject,
            as_of=request.as_of,
            evidence=evidence,
            blocks=blocks,
            data_limitations=limitations,
        )
    )


def _validate_repository_output(
    request: AnalysisContextRequest,
    items: tuple[EvidenceItem, ...],
) -> Result[tuple[EvidenceItem, ...]]:
    ids = tuple(item.evidence_id for item in items)
    if len(ids) != len(set(ids)):
        return _failure(
            ErrorCode.CONFLICT, "Evidence repository returned duplicate IDs"
        )
    invalid = any(
        item.subject != request.subject
        or item.available_at > request.as_of
        or item.as_of > request.as_of
        for item in items
    )
    if invalid:
        return _failure(
            ErrorCode.CONFLICT,
            "Evidence repository exceeded point-in-time request scope",
        )
    return Success(items)


def _apply_content_policy(
    request: AnalysisContextRequest,
    items: tuple[EvidenceItem, ...],
) -> tuple[tuple[EvidenceItem, ...], tuple[EvidenceItem, ...]]:
    allowed_sensitivities = set(request.allowed_sensitivities)
    allowed_licenses = set(request.allowed_license_tags)
    allowed_redistribution = set(request.allowed_redistribution_tags)
    allowed: list[EvidenceItem] = []
    excluded: list[EvidenceItem] = []
    for item in items:
        permitted = (
            item.sensitivity in allowed_sensitivities
            and item.license_tag in allowed_licenses
            and item.redistribution_tag in allowed_redistribution
        )
        (allowed if permitted else excluded).append(item)
    return tuple(allowed), tuple(excluded)


def _build_block(
    requirement: EvidenceRequirement,
    request: AnalysisContextRequest,
    items: tuple[EvidenceItem, ...],
) -> EvidenceBlock:
    if not requirement.supported:
        return EvidenceBlock(
            capability=requirement.capability,
            status=DataQualityStatus.NOT_SUPPORTED,
            completeness=Decimal(0),
            evidence_refs=(),
            sources=(),
            missing_reason="capability_not_supported",
        )
    matches = tuple(item for item in items if item.kind in requirement.kinds)
    selected = tuple(
        sorted(
            matches,
            key=lambda item: (item.available_at, str(item.evidence_id)),
            reverse=True,
        )[: requirement.maximum_items]
    )
    status = _block_status(requirement, request, selected)
    warnings = tuple(
        sorted({warning for item in selected for warning in item.quality.warnings})
    )
    return EvidenceBlock(
        capability=requirement.capability,
        status=status,
        completeness=_completeness(requirement, selected),
        evidence_refs=tuple(sorted((item.evidence_id for item in selected), key=str)),
        sources=tuple(sorted({f"{item.provider}:{item.source}" for item in selected})),
        latest_available_at=max((item.available_at for item in selected), default=None),
        warnings=warnings,
        missing_reason=_missing_reason(requirement, status, selected),
    )


def _block_status(
    requirement: EvidenceRequirement,
    request: AnalysisContextRequest,
    items: tuple[EvidenceItem, ...],
) -> DataQualityStatus:
    if not items:
        return DataQualityStatus.MISSING
    if _has_conflict(items):
        return DataQualityStatus.CONFLICT
    stale = tuple(_is_stale(requirement, request, item) for item in items)
    statuses = {item.quality.status for item in items}
    if len(items) < requirement.minimum_items:
        return DataQualityStatus.PARTIAL
    if all(stale):
        return DataQualityStatus.STALE
    if any(stale):
        return DataQualityStatus.PARTIAL
    if statuses <= {DataQualityStatus.MISSING, DataQualityStatus.FETCH_FAILED}:
        return (
            DataQualityStatus.FETCH_FAILED
            if DataQualityStatus.FETCH_FAILED in statuses
            else DataQualityStatus.MISSING
        )
    if statuses <= {DataQualityStatus.NOT_SUPPORTED}:
        return DataQualityStatus.NOT_SUPPORTED
    if statuses & {
        DataQualityStatus.MISSING,
        DataQualityStatus.NOT_SUPPORTED,
        DataQualityStatus.PARTIAL,
        DataQualityStatus.FETCH_FAILED,
    }:
        return DataQualityStatus.PARTIAL
    if DataQualityStatus.ESTIMATED in statuses:
        return DataQualityStatus.ESTIMATED
    if DataQualityStatus.FALLBACK in statuses:
        return DataQualityStatus.FALLBACK
    if DataQualityStatus.STALE in statuses:
        return DataQualityStatus.STALE
    return DataQualityStatus.AVAILABLE


def _has_conflict(items: tuple[EvidenceItem, ...]) -> bool:
    if any(item.quality.status is DataQualityStatus.CONFLICT for item in items):
        return True
    hashes_by_event: dict[object, set[str]] = {}
    for item in items:
        hashes_by_event.setdefault(item.event_time, set()).add(item.content_hash)
    return any(len(hashes) > 1 for hashes in hashes_by_event.values())


def _is_stale(
    requirement: EvidenceRequirement,
    request: AnalysisContextRequest,
    item: EvidenceItem,
) -> bool:
    if item.quality.status is DataQualityStatus.STALE:
        return True
    if requirement.freshness_seconds is None:
        return False
    return request.as_of - item.available_at > timedelta(
        seconds=requirement.freshness_seconds
    )


def _completeness(
    requirement: EvidenceRequirement,
    items: tuple[EvidenceItem, ...],
) -> Decimal:
    if not items:
        return Decimal(0)
    evidence_quality = min(item.quality.completeness for item in items)
    if requirement.minimum_items == 0:
        return evidence_quality
    count_quality = min(Decimal(1), Decimal(len(items)) / requirement.minimum_items)
    return min(evidence_quality, count_quality)


def _missing_reason(
    requirement: EvidenceRequirement,
    status: DataQualityStatus,
    items: tuple[EvidenceItem, ...],
) -> str | None:
    if status is DataQualityStatus.MISSING:
        return "no_policy_allowed_evidence"
    if status is DataQualityStatus.NOT_SUPPORTED:
        return "capability_not_supported"
    if len(items) < requirement.minimum_items:
        return "minimum_items_not_met"
    return None


def _limitations(
    request: AnalysisContextRequest,
    blocks: tuple[EvidenceBlock, ...],
    excluded: tuple[EvidenceItem, ...],
) -> tuple[str, ...]:
    limitations: set[str] = set()
    required = {item.capability for item in request.requirements if item.required}
    for block in blocks:
        if block.status is not DataQualityStatus.AVAILABLE:
            limitation = f"{block.capability}:{block.status.value}"
            limitations.add(limitation)
            if block.capability in required:
                limitations.add(f"required:{limitation}")
    for requirement in request.requirements:
        count = sum(item.kind in requirement.kinds for item in excluded)
        if count:
            limitations.add(f"{requirement.capability}:policy_excluded:{count}")
    return tuple(sorted(limitations))


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
