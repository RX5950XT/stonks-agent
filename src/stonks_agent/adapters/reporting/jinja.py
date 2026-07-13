"""Sandboxed fixed-template rendering into content-addressed artifacts."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jinja2 import StrictUndefined, Template, select_autoescape
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.report import AnalysisReport, ReportRendering

TEMPLATE_VERSION = "stonks-report-templates/1.0.0"
_FORMATS = (
    ("markdown_full", "full.md.j2", "text/markdown", 65_536),
    ("markdown_brief", "brief.md.j2", "text/markdown", 4_096),
    ("email_html", "email.html.j2", "text/html", 131_072),
)
_LABELS = {
    "en": {
        "as_of": "As of",
        "outlook": "Outlook",
        "score": "Score",
        "confidence": "Confidence",
        "claims": "Claims",
        "evidence": "Evidence",
        "risks": "Risks",
        "catalysts": "Catalysts",
        "limitations": "Data limitations",
        "guardrails": "Guardrails",
        "none": "None",
    },
    "zh-TW": {
        "as_of": "資料截止",
        "outlook": "研究展望",
        "score": "分數",
        "confidence": "信心",
        "claims": "研究主張",
        "evidence": "證據",
        "risks": "風險",
        "catalysts": "催化因素",
        "limitations": "資料限制",
        "guardrails": "安全界線",
        "none": "無",
    },
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+.!|-])")


class JinjaReportRenderer:
    __slots__ = ("_artifacts", "_clock", "_environment", "_templates")

    def __init__(
        self,
        *,
        template_directory: Path,
        artifacts: ArtifactStore,
        clock: Callable[[], datetime],
    ) -> None:
        resolved = template_directory.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("report template directory is invalid")
        environment = SandboxedEnvironment(
            loader=None,
            autoescape=select_autoescape(enabled_extensions=("html", "xml")),
            undefined=StrictUndefined,
            enable_async=False,
            auto_reload=False,
        )
        environment.filters["md"] = _escape_markdown
        templates: dict[str, Template] = {}
        for _, filename, _, _ in _FORMATS:
            path = (resolved / filename).resolve(strict=True)
            if path.parent != resolved or not path.is_file():
                raise ValueError("report template path escaped template directory")
            templates[filename] = environment.overlay(
                autoescape=filename.endswith(".html.j2")
            ).from_string(path.read_text("utf-8"))
        self._environment = environment
        self._templates = templates
        self._artifacts = artifacts
        self._clock = clock

    def render(self, report: AnalysisReport) -> Result[AnalysisReport]:
        language = report.language if report.language in _LABELS else None
        if language is None:
            return _failure(ErrorCode.INVALID_INPUT, "Report language is unsupported")
        values = _template_values(report, language)
        rendered: list[tuple[str, str, str]] = []
        try:
            for format_name, filename, media_type, maximum in _FORMATS:
                template = self._templates[filename]
                content = template.render(**values).strip() + "\n"
                if len(content.encode("utf-8")) > maximum:
                    return _failure(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        f"Rendered {format_name} exceeds channel limit",
                    )
                rendered.append((format_name, media_type, content))
        except (TemplateError, TypeError, ValueError):
            return _failure(
                ErrorCode.INTERNAL_ERROR, "Report template rendering failed"
            )
        manifests: list[ReportRendering] = []
        finalized_at = self._clock()
        for format_name, media_type, content in rendered:
            stored = self._artifacts.finalize(
                content.encode("utf-8"),
                metadata=ArtifactMetadata(
                    media_type=media_type,
                    license_tag="Apache-2.0",
                    sensitivity=Sensitivity.INTERNAL,
                    source="stonks-agent-report-renderer",
                    attributes=(
                        ("format", format_name),
                        ("report_id", str(report.report_id)),
                        ("template_version", TEMPLATE_VERSION),
                    ),
                ),
                finalized_at=finalized_at,
            )
            if isinstance(stored, Failure):
                return stored
            manifests.append(
                ReportRendering(
                    format=format_name,
                    template_version=TEMPLATE_VERSION,
                    content_hash=stored.value.content_hash,
                    content_ref=f"sha256:{stored.value.content_hash}",
                )
            )
        return Success(report.model_copy(update={"renderings": tuple(manifests)}))


def _template_values(report: AnalysisReport, language: str) -> dict[str, object]:
    return {
        "language": language,
        "labels": _LABELS[language],
        "subject": _truncate(report.subject, 128),
        "as_of": report.model_dump(mode="json")["as_of"],
        "conclusion": report.conclusion,
        "score": str(report.score),
        "confidence": str(report.confidence),
        "claims": tuple(
            {
                **claim.model_dump(mode="json"),
                "brief_assertion": _truncate(claim.assertion, 280),
            }
            for claim in report.claims
        ),
        "risks": report.risks,
        "catalysts": report.catalysts,
        "data_limitations": report.data_limitations,
        "action_guardrails": report.action_guardrails,
    }


def _truncate(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else f"{value[: maximum - 1]}…"


def _escape_markdown(value: object) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(value))


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
