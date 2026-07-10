from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from stonks_agent.domain.provenance import ProvenanceRecord

NOW = datetime(2026, 1, 2, 21, tzinfo=UTC)


def test_provenance_preserves_raw_artifact_provider_and_versions() -> None:
    record = ProvenanceRecord(
        provider="financial_datasets",
        provider_version="2026-01",
        endpoint="/prices",
        request_id="request-123",
        source_url="https://api.example.test/prices?symbol=AAPL",
        raw_artifact_hash="a" * 64,
        payload_hash="b" * 64,
        observed_at=NOW,
        license_tag="contract-only",
        redistribution_tag="none",
    )

    assert record.raw_artifact_ref == f"sha256:{'a' * 64}"
    assert record.provider == "financial_datasets"
    assert record.provider_version == "2026-01"


@pytest.mark.parametrize(
    "source_url",
    [
        "http://api.example.test/data",
        "https://user:password@api.example.test/data",
        "file:///etc/passwd",
    ],
)
def test_provenance_rejects_unsafe_source_urls(source_url: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        ProvenanceRecord(
            provider="provider",
            provider_version="1",
            endpoint="/data",
            request_id="request-123",
            source_url=source_url,
            raw_artifact_hash="a" * 64,
            payload_hash="b" * 64,
            observed_at=NOW,
            license_tag="test",
            redistribution_tag="none",
        )


def test_provenance_rejects_arbitrary_endpoint_url() -> None:
    with pytest.raises(ValidationError, match="relative path"):
        ProvenanceRecord(
            provider="provider",
            provider_version="1",
            endpoint="https://attacker.test/data",
            request_id="request-123",
            source_url="https://api.example.test/data",
            raw_artifact_hash="a" * 64,
            payload_hash="b" * 64,
            observed_at=NOW,
            license_tag="test",
            redistribution_tag="none",
        )
