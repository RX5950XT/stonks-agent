from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.reporting.artifact_reader import ArtifactReportReader
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_contracts.evidence import Sensitivity

NOW = datetime(2026, 7, 13, 8, tzinfo=UTC)
REPORT_ID = UUID("37000000-0000-4000-8000-000000000001")


def test_reader_accepts_only_typed_renderer_artifact() -> None:
    store = MemoryArtifactStore()
    stored = store.finalize(
        b"# report\n",
        metadata=metadata(),
        finalized_at=NOW,
    )
    assert isinstance(stored, Success)

    result = ArtifactReportReader(store).read(stored.value.content_hash)

    assert isinstance(result, Success)
    assert result.value.report_id == REPORT_ID
    assert result.value.format == "markdown_full"
    assert result.value.content == "# report\n"


def test_reader_rejects_arbitrary_artifact_and_invalid_utf8() -> None:
    store = MemoryArtifactStore()
    arbitrary = store.finalize(
        b"private prompt",
        metadata=metadata(source="llm-raw-response"),
        finalized_at=NOW,
    )
    invalid_utf8 = store.finalize(
        b"\xff",
        metadata=metadata(),
        finalized_at=NOW,
    )
    assert isinstance(arbitrary, Success)
    assert isinstance(invalid_utf8, Success)

    denied = ArtifactReportReader(store).read(arbitrary.value.content_hash)
    invalid = ArtifactReportReader(store).read(invalid_utf8.value.content_hash)

    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.CAPABILITY_DENIED
    assert isinstance(invalid, Failure)
    assert invalid.error.code is ErrorCode.INVALID_INPUT


def metadata(*, source: str = "stonks-agent-report-renderer") -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="text/markdown",
        license_tag="Apache-2.0",
        sensitivity=Sensitivity.INTERNAL,
        source=source,
        attributes=(
            ("format", "markdown_full"),
            ("report_id", str(REPORT_ID)),
            ("template_version", "stonks-report-templates/1.0.0"),
        ),
    )
