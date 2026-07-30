from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.application.research.context_builder import build_research_context
from stonks_agent.domain.errors import ErrorCode, Failure, Success

from .helpers import EVIDENCE_ID, NOW, DictArtifactReader, evidence, request


def test_context_reads_scoped_artifact_and_marks_external_text_untrusted() -> None:
    result = build_research_context(
        request(),
        (evidence(),),
        DictArtifactReader({"a" * 64: b'\x7b"revenue":"up"\x7d'}),
    )

    assert isinstance(result, Success)
    assert result.value.evidence_ids == (EVIDENCE_ID,)
    assert result.value.blocks[0].untrusted_content is True
    assert result.value.blocks[0].content == '{"revenue":"up"}'


def test_context_rejects_out_of_scope_future_and_oversized_evidence() -> None:
    attempts = (
        (
            (evidence(evidence_id=uuid4()),),
            DictArtifactReader({"a" * 64: b"content"}),
            ErrorCode.CAPABILITY_DENIED,
        ),
        (
            (
                evidence(
                    available_at=NOW + timedelta(minutes=1),
                    observed_at=NOW + timedelta(minutes=2),
                    as_of=NOW + timedelta(minutes=2),
                ),
            ),
            DictArtifactReader({"a" * 64: b"content"}),
            ErrorCode.DATA_UNAVAILABLE,
        ),
        (
            (evidence(),),
            DictArtifactReader({"a" * 64: b"x" * 32_769}),
            ErrorCode.PAYLOAD_TOO_LARGE,
        ),
    )

    for items, reader, code in attempts:
        result = build_research_context(request(), items, reader)
        assert isinstance(result, Failure)
        assert result.error.code is code


def test_context_does_not_turn_missing_or_invalid_artifact_into_empty_success() -> None:
    for reader in (
        DictArtifactReader({}),
        DictArtifactReader({"a" * 64: b"\xff"}),
    ):
        result = build_research_context(request(), (evidence(),), reader)
        assert isinstance(result, Failure)


def test_compact_context_archives_inventory_without_loading_repeated_raw_payload() -> (
    None
):
    store = MemoryArtifactStore()
    items = tuple(
        evidence(evidence_id=uuid4(), raw_artifact_ref=f"sha256:{'a' * 64}")
        for _ in range(130)
    )
    scoped = request(allowed_evidence_ids=frozenset(item.evidence_id for item in items))

    result = build_research_context(
        scoped,
        items,
        DictArtifactReader({}),
        compact_store=store,
    )

    assert isinstance(result, Success)
    assert len(result.value.blocks) > 1
    assert all(
        store.is_finalized(block.source_ref.removeprefix("sha256:"))
        for block in result.value.blocks
    )
    assert result.value.total_bytes <= 262_144
