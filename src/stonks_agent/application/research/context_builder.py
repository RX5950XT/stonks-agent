"""Build bounded LLM context from immutable point-in-time evidence artifacts."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.research import ResearchRequest, UntrustedContentBlock
from stonks_agent.ports.artifact_store import ArtifactReaderPort
from stonks_contracts.evidence import EvidenceItem

MAX_CONTEXT_BLOCK_BYTES = 32_768
MAX_CONTEXT_TOTAL_BYTES = 262_144
MAX_CONTEXT_BLOCKS = 128


class ResearchContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ids: tuple[UUID, ...]
    blocks: tuple[UntrustedContentBlock, ...] = Field(max_length=MAX_CONTEXT_BLOCKS)
    total_bytes: int = Field(ge=1, le=MAX_CONTEXT_TOTAL_BYTES)


def build_research_context(
    request: ResearchRequest,
    evidence_items: tuple[EvidenceItem, ...],
    artifacts: ArtifactReaderPort,
) -> Result[ResearchContext]:
    if not evidence_items:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Research evidence is unavailable")
    if len(evidence_items) > MAX_CONTEXT_BLOCKS:
        return _failure(
            ErrorCode.PAYLOAD_TOO_LARGE, "Research context has too many blocks"
        )
    ordered = tuple(sorted(evidence_items, key=lambda item: str(item.evidence_id)))
    evidence_ids: list[UUID] = []
    blocks: list[UntrustedContentBlock] = []
    total_bytes = 0
    seen: set[UUID] = set()
    for item in ordered:
        validation = _validate_evidence(request, item, seen)
        if validation is not None:
            return validation
        loaded = load_untrusted_artifact(item.raw_artifact_ref, artifacts)
        if isinstance(loaded, Failure):
            return loaded
        total_bytes += len(loaded.value.content.encode("utf-8"))
        if total_bytes > MAX_CONTEXT_TOTAL_BYTES:
            return _failure(
                ErrorCode.PAYLOAD_TOO_LARGE, "Research context exceeds byte limit"
            )
        evidence_ids.append(item.evidence_id)
        blocks.append(loaded.value)
        seen.add(item.evidence_id)
    return Success(
        ResearchContext(
            evidence_ids=tuple(evidence_ids),
            blocks=tuple(blocks),
            total_bytes=total_bytes,
        )
    )


def load_untrusted_artifact(
    source_ref: str,
    artifacts: ArtifactReaderPort,
    *,
    max_bytes: int = MAX_CONTEXT_BLOCK_BYTES,
) -> Result[UntrustedContentBlock]:
    content_hash = source_ref.removeprefix("sha256:")
    loaded = artifacts.read(content_hash)
    if isinstance(loaded, Failure):
        return loaded
    content = loaded.value
    if len(content) > max_bytes:
        return _failure(
            ErrorCode.PAYLOAD_TOO_LARGE, "Research artifact exceeds byte limit"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _failure(ErrorCode.INVALID_INPUT, "Research artifact must be UTF-8 text")
    if not text:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Research artifact is empty")
    return Success(UntrustedContentBlock(source_ref=source_ref, content=text))


def _validate_evidence(
    request: ResearchRequest,
    item: EvidenceItem,
    seen: set[UUID],
) -> Failure | None:
    if item.evidence_id not in request.allowed_evidence_ids:
        return _failure(
            ErrorCode.CAPABILITY_DENIED, "Evidence is outside request scope"
        )
    if item.evidence_id in seen:
        return _failure(ErrorCode.CONFLICT, "Duplicate evidence identifier")
    if item.available_at > request.as_of:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Future evidence is forbidden")
    return None


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
