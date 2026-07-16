from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.reporting.jinja import TEMPLATE_VERSION, JinjaReportRenderer
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_contracts.market_data import DataQualityStatus
from stonks_contracts.report import (
    AnalysisReport,
    ClaimCertainty,
    ReportClaim,
)

NOW = datetime(2026, 7, 13, 6, tzinfo=UTC)
REPORT_ID = UUID("34000000-0000-4000-8000-000000000001")
MARKET_ID = UUID("34000000-0000-4000-8000-000000000002")
NEWS_ID = UUID("34000000-0000-4000-8000-000000000003")
TEMPLATES = Path("templates")
GOLDEN = Path("tests/golden/reports")


def report(**overrides: object) -> AnalysisReport:
    values: dict[str, object] = {
        "report_id": REPORT_ID,
        "run_id": UUID("34000000-0000-4000-8000-000000000009"),
        "owner_subject": "research-owner",
        "subject": "AAPL",
        "as_of": NOW,
        "language": "zh-TW",
        "report_type": "equity_research",
        "conclusion": "bullish_outlook",
        "score": Decimal("0.7"),
        "confidence": Decimal("0.5"),
        "risks": ("Evidence coverage is narrow.",),
        "catalysts": ("Product cycle",),
        "scenarios": (),
        "signal_attribution": (),
        "action_guardrails": (
            "research_only_no_execution_authority",
            "paper_only_execution_mode",
        ),
        "data_limitations": ("news:stale", "market:conflict"),
        "claims": (
            ReportClaim(
                claim_id=UUID("34000000-0000-4000-8000-000000000004"),
                assertion="Canonical close is 100.",
                certainty=ClaimCertainty.OBSERVED,
                data_quality=DataQualityStatus.AVAILABLE,
                evidence_refs=(MARKET_ID,),
            ),
            ReportClaim(
                claim_id=UUID("34000000-0000-4000-8000-000000000005"),
                assertion="Old product news may remain relevant.",
                certainty=ClaimCertainty.QUALIFIED,
                data_quality=DataQualityStatus.STALE,
                evidence_refs=(NEWS_ID,),
            ),
        ),
        "evidence_refs": (MARKET_ID, NEWS_ID),
        "signal_ids": (),
        "generator_version": "structured-report-generator/1.0.0",
        "model_version": "fake-structured-v1",
        "prompt_version": "analysis-report/1.0.0",
        "generation_artifact_ref": f"sha256:{'c' * 64}",
        "policy_version": "report-policy/1.0.0",
        "renderings": (),
    }
    values.update(overrides)
    return AnalysisReport.model_validate(values)


def renderer(
    store: MemoryArtifactStore | None = None,
) -> tuple[JinjaReportRenderer, MemoryArtifactStore]:
    artifacts = store or MemoryArtifactStore()
    return (
        JinjaReportRenderer(
            template_directory=TEMPLATES,
            artifacts=artifacts,
            clock=lambda: NOW,
        ),
        artifacts,
    )


def rendered_content(
    value: AnalysisReport, store: MemoryArtifactStore
) -> dict[str, str]:
    result: dict[str, str] = {}
    for rendering in value.renderings:
        stored = store.read(rendering.content_hash)
        assert isinstance(stored, Success)
        result[rendering.format] = stored.value.decode("utf-8")
    return result


def test_all_channels_match_golden_and_are_content_addressed() -> None:
    subject, store = renderer()

    result = subject.render(report())

    assert isinstance(result, Success)
    contents = rendered_content(result.value, store)
    assert set(contents) == {"markdown_full", "markdown_brief", "email_html"}
    for format_name, content in contents.items():
        expected = (GOLDEN / f"{format_name}.txt").read_text("utf-8")
        assert content == expected
    assert all(
        item.template_version == TEMPLATE_VERSION for item in result.value.renderings
    )
    assert all(
        item.content_ref == f"sha256:{item.content_hash}"
        for item in result.value.renderings
    )


def test_same_report_rebuilds_identical_hashes_from_json_truth() -> None:
    subject, store = renderer()
    first = subject.render(report())
    assert isinstance(first, Success)

    replay = subject.render(first.value)

    assert isinstance(replay, Success)
    assert replay.value.renderings == first.value.renderings
    assert rendered_content(replay.value, store) == rendered_content(first.value, store)


def test_markdown_html_escaping_quality_qualifiers_and_long_subject() -> None:
    malicious = "<script>alert(1)</script> *AAPL*" + "X" * 200
    claim = (
        report()
        .claims[1]
        .model_copy(update={"assertion": "<img src=x onerror=alert(1)> *stale*"})
    )
    subject, store = renderer()

    result = subject.render(
        report(subject=malicious, claims=(report().claims[0], claim))
    )

    assert isinstance(result, Success)
    contents = rendered_content(result.value, store)
    assert "<script>" not in contents["email_html"]
    assert "&lt;script&gt;" in contents["email_html"]
    assert "\\<script\\>" in contents["markdown_full"]
    assert "\\*stale\\*" in contents["markdown_full"]
    assert "qualified/stale" in contents["markdown_brief"]
    title = contents["email_html"].split("<h1>", 1)[1].split("</h1>", 1)[0]
    assert len(title) < len(malicious)
    assert title.endswith("…")


def test_english_labels_and_unsupported_language_fail_closed() -> None:
    subject, store = renderer()
    english = subject.render(report(language="en"))
    unsupported = subject.render(report(language="ko"))

    assert isinstance(english, Success)
    assert "Data limitations" in rendered_content(english.value, store)["markdown_full"]
    assert isinstance(unsupported, Failure)
    assert unsupported.error.code is ErrorCode.INVALID_INPUT


def test_channel_limit_is_checked_before_any_artifact_write() -> None:
    claims = tuple(
        ReportClaim(
            claim_id=UUID(int=100 + index),
            assertion="x" * 4_000,
            certainty=ClaimCertainty.OBSERVED,
            data_quality=DataQualityStatus.AVAILABLE,
            evidence_refs=(MARKET_ID,),
        )
        for index in range(20)
    )
    store = RecordingStore()
    subject, _ = renderer(store)

    result = subject.render(report(claims=claims))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert store.finalize_calls == 0


def test_missing_template_directory_or_file_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises((FileNotFoundError, ValueError)):
        JinjaReportRenderer(
            template_directory=tmp_path / "missing",
            artifacts=MemoryArtifactStore(),
            clock=lambda: NOW,
        )
    empty = tmp_path / "templates"
    empty.mkdir()
    with pytest.raises((FileNotFoundError, ValueError)):
        JinjaReportRenderer(
            template_directory=empty,
            artifacts=MemoryArtifactStore(),
            clock=lambda: NOW,
        )


class RecordingStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.finalize_calls = 0

    def finalize(
        self, content: object, *, metadata: object, finalized_at: object
    ) -> object:
        self.finalize_calls += 1
        return super().finalize(content, metadata=metadata, finalized_at=finalized_at)
