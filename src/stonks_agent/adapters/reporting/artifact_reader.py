"""Capability-scoped reader for finalized report rendering artifacts."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.research_run import ReportProjection
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.evidence import Sensitivity

_MEDIA_BY_FORMAT = {
    "markdown_full": "text/markdown",
    "markdown_brief": "text/markdown",
    "email_html": "text/html",
}


class ArtifactReportReader:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    def read(self, content_hash: str) -> Result[ReportProjection]:
        manifest = self._artifacts.manifest(content_hash)
        if isinstance(manifest, Failure):
            return manifest
        value = manifest.value
        attributes = dict(value.metadata.attributes)
        if not _is_report_manifest(value.size_bytes, value.metadata, attributes):
            return _failure(
                ErrorCode.CAPABILITY_DENIED, "Artifact is not a readable report"
            )
        content = self._artifacts.read(content_hash)
        if isinstance(content, Failure):
            return content
        try:
            text = content.value.decode("utf-8")
            projection = ReportProjection(
                report_id=UUID(attributes["report_id"]),
                content_hash=content_hash,
                format=attributes["format"],
                media_type=cast(
                    Literal["text/markdown", "text/html"],
                    value.metadata.media_type,
                ),
                content=text,
            )
        except (UnicodeDecodeError, ValidationError, ValueError):
            return _failure(ErrorCode.INVALID_INPUT, "Report artifact is invalid")
        return Success(projection)


def _is_report_manifest(
    size_bytes: int, metadata: object, attributes: dict[str, str]
) -> bool:
    from stonks_agent.domain.artifact import ArtifactMetadata

    if not isinstance(metadata, ArtifactMetadata):
        return False
    expected_media = _MEDIA_BY_FORMAT.get(attributes.get("format", ""))
    return (
        1 <= size_bytes <= 131_072
        and metadata.source == "stonks-agent-report-renderer"
        and metadata.license_tag == "Apache-2.0"
        and metadata.sensitivity is Sensitivity.INTERNAL
        and expected_media == metadata.media_type
        and set(attributes) == {"format", "report_id", "template_version"}
        and attributes["template_version"] == "stonks-report-templates/1.0.0"
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
