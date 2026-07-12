from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
import yaml

from stonks_agent.adapters.market_data.replay_snapshot import (
    ReplaySnapshotMaterializationSource,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.dataset_snapshot import MAX_RAW_PAYLOAD_BYTES, MAX_REASONS
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.provider_policy import ProviderPolicy, load_provider_policies

MANIFEST = Path("tests/fixtures/market_data/manifest.yaml")
POLICIES = Path("config/providers/default.yaml")
AS_OF = datetime(2026, 3, 10, 22, tzinfo=UTC)


def test_replay_snapshot_source_requires_the_injected_allowlisted_policy() -> None:
    source = ReplaySnapshotMaterializationSource(MANIFEST, us_policy())
    request = FetchDataRequest(
        market="US",
        capability="prices",
        as_of=AS_OF,
        query={"symbol": "AAPL", "interval": "1d", "scenario": "canonical"},
    )

    accepted = source.fetch(request, provider_policy_id="us-prices/1")
    denied = source.fetch(request, provider_policy_id="forged-policy/1")

    assert isinstance(accepted, Success)
    assert accepted.value.provider == "replay"
    assert len(accepted.value.evidence) == 4
    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.CAPABILITY_DENIED


def test_replay_snapshot_stats_before_opening_an_oversized_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ReplaySnapshotMaterializationSource(MANIFEST, us_policy())
    target = (MANIFEST.parent / "us_daily_dst_actions.json").resolve()
    original_stat = Path.stat
    fixture_opens: list[Path] = []

    def bounded_stat(
        path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> object:
        if path == target:
            return SimpleNamespace(st_size=MAX_RAW_PAYLOAD_BYTES + 1)
        return original_stat(path, follow_symlinks=follow_symlinks)

    def tracked_open(path: Path, *args: object, **kwargs: object) -> NoReturn:
        if path == target:
            fixture_opens.append(path)
            raise AssertionError("oversized fixture must not be opened")
        raise AssertionError(f"unexpected file open: {path}")

    monkeypatch.setattr(Path, "stat", bounded_stat)
    monkeypatch.setattr(Path, "open", tracked_open)

    result = source.fetch(canonical_request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert fixture_opens == []


def test_replay_snapshot_uses_bounded_streaming_instead_of_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ReplaySnapshotMaterializationSource(MANIFEST, us_policy())

    def reject_read_bytes(_: Path) -> bytes:
        raise AssertionError("unbounded Path.read_bytes is forbidden")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    result = source.fetch(canonical_request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Success)
    assert len(result.value.evidence) == 4


def test_replay_rejects_reason_count_before_opening_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    payload["fixtures"] = [payload["fixtures"][0]]
    payload["fixtures"][0]["reasons"] = ["reason"] * (MAX_REASONS + 1)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(payload), encoding="utf-8")
    source = ReplaySnapshotMaterializationSource(manifest, us_policy())

    def reject_open(_: Path, *args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("fixture must not be opened for oversized reasons")

    monkeypatch.setattr(Path, "open", reject_open)

    result = source.fetch(canonical_request(), provider_policy_id="us-prices/1")

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT


def canonical_request() -> FetchDataRequest:
    return FetchDataRequest(
        market="US",
        capability="prices",
        as_of=AS_OF,
        query={"symbol": "AAPL", "interval": "1d", "scenario": "canonical"},
    )


def us_policy() -> ProviderPolicy:
    return next(
        policy
        for policy in load_provider_policies(POLICIES)
        if policy.policy_id == "us-prices/1"
    )
