from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from stonks_agent.adapters.market_data.replay import (
    ReplayFixtureIntegrityError,
    ReplayMarketDataAdapter,
)
from stonks_agent.application.data.fetch_evidence import FetchDataRequest
from stonks_agent.domain.data_quality import ProviderDataState

FIXTURE_ROOT = Path("tests/fixtures/market_data")
MANIFEST = FIXTURE_ROOT / "manifest.yaml"
GOLDEN = Path("tests/golden/replay_observations.json")


def _request(
    *,
    market: str,
    symbol: str,
    interval: str,
    scenario: str,
    as_of: datetime,
) -> FetchDataRequest:
    return FetchDataRequest(
        market=market,
        capability="prices",
        as_of=as_of,
        query={"symbol": symbol, "interval": interval, "scenario": scenario},
    )


def _summary(adapter: ReplayMarketDataAdapter) -> dict[str, object]:
    result: dict[str, object] = {}
    for fixture in adapter.fixtures:
        entry = fixture.entry
        observation = adapter.fetch(
            _request(
                market=entry.market,
                symbol=entry.symbol,
                interval=entry.interval,
                scenario=entry.scenario,
                as_of=entry.as_of,
            )
        )
        dataset = fixture.dataset
        result[entry.fixture_id] = {
            "state": observation.state.value,
            "completeness": format(observation.completeness, "f"),
            "observation_items": len(observation.data),
            "bar_times": [bar.timeline.event_time.isoformat() for bar in dataset.bars],
            "corporate_actions": [
                action.kind.value for action in dataset.corporate_actions
            ],
            "conflict_fields": [conflict.field for conflict in dataset.conflicts],
            "reasons": list(observation.reasons),
        }
    return result


def test_golden_replay_covers_regions_intervals_dst_actions_and_quality() -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)

    assert _summary(adapter) == json.loads(GOLDEN.read_text(encoding="utf-8"))
    tags = {tag for fixture in adapter.fixtures for tag in fixture.entry.tags}
    assert {"US", "HK", "TW", "daily", "intraday"} <= tags
    assert {"dst", "split", "dividend", "stale", "partial", "conflict"} <= tags


@pytest.mark.parametrize(
    ("market", "symbol", "interval", "scenario", "expected_state"),
    [
        ("US", "AAPL", "1d", "canonical", ProviderDataState.AVAILABLE),
        ("HK", "0700", "5m", "partial", ProviderDataState.PARTIAL),
        ("TW", "2330", "1d", "stale", ProviderDataState.STALE),
        ("US", "AAPL", "5m", "conflict", ProviderDataState.CONFLICT),
    ],
)
def test_replay_returns_explicit_typed_states(
    market: str,
    symbol: str,
    interval: str,
    scenario: str,
    expected_state: ProviderDataState,
) -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)
    entry = next(
        item.entry for item in adapter.fixtures if item.entry.scenario == scenario
    )

    observation = adapter.fetch(
        _request(
            market=market,
            symbol=symbol,
            interval=interval,
            scenario=scenario,
            as_of=entry.as_of,
        )
    )

    assert observation.state is expected_state
    if expected_state is ProviderDataState.CONFLICT:
        assert observation.data == ()
        assert observation.completeness == 0
        assert observation.reasons == ("reconciliation_threshold_exceeded",)
    else:
        assert observation.data


def test_us_daily_fixture_proves_dst_shift_and_corporate_actions() -> None:
    fixture = next(
        item
        for item in ReplayMarketDataAdapter(MANIFEST).fixtures
        if item.entry.fixture_id == "us-daily-dst-actions"
    )
    event_times = tuple(bar.timeline.event_time for bar in fixture.dataset.bars)

    assert event_times == (
        datetime(2026, 3, 6, 21, tzinfo=UTC),
        datetime(2026, 3, 9, 20, tzinfo=UTC),
    )
    local_times = tuple(
        time.astimezone(ZoneInfo(fixture.dataset.timezone)) for time in event_times
    )
    assert tuple(time.hour for time in local_times) == (16, 16)
    assert tuple(time.utcoffset() for time in local_times) == (
        -timedelta(hours=5),
        -timedelta(hours=4),
    )
    split, dividend = fixture.dataset.corporate_actions
    assert (split.kind.value, split.ratio) == ("split", Decimal("2"))
    assert split.cash_amount is None and split.currency is None
    assert (dividend.kind.value, dividend.cash_amount) == (
        "dividend",
        Decimal("0.25"),
    )
    assert dividend.currency == "USD" and dividend.ratio is None


def test_hk_intraday_fixture_is_partial_and_preserves_five_minute_bars() -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)
    fixture = next(
        item
        for item in adapter.fixtures
        if item.entry.fixture_id == "hk-intraday-partial"
    )
    observation = adapter.fetch(
        _request(
            market="HK",
            symbol="0700",
            interval="5m",
            scenario="partial",
            as_of=fixture.entry.as_of,
        )
    )
    first, second = fixture.dataset.bars

    assert fixture.dataset.timezone == "Asia/Hong_Kong"
    assert second.timeline.event_time - first.timeline.event_time == timedelta(
        minutes=5
    )
    assert observation.state is ProviderDataState.PARTIAL
    assert observation.completeness == Decimal("0.5")
    assert observation.reasons == ("missing_intraday_bars",)
    assert observation.data == (fixture.dataset,)


def test_tw_daily_fixture_is_explicitly_stale_but_keeps_evidence() -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)
    fixture = next(
        item for item in adapter.fixtures if item.entry.fixture_id == "tw-daily-stale"
    )
    observation = adapter.fetch(
        _request(
            market="TW",
            symbol="2330",
            interval="1d",
            scenario="stale",
            as_of=fixture.entry.as_of,
        )
    )
    bar = fixture.dataset.bars[0]

    assert fixture.dataset.timezone == "Asia/Taipei"
    assert bar.timeline.available_at < fixture.dataset.as_of - timedelta(days=3)
    assert observation.state is ProviderDataState.STALE
    assert observation.reasons == ("freshness_threshold_exceeded",)
    assert observation.data == (fixture.dataset,)


def test_us_intraday_conflict_is_typed_and_never_exposes_ambiguous_data() -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)
    fixture = next(
        item
        for item in adapter.fixtures
        if item.entry.fixture_id == "us-intraday-conflict"
    )
    observation = adapter.fetch(
        _request(
            market="US",
            symbol="AAPL",
            interval="5m",
            scenario="conflict",
            as_of=fixture.entry.as_of,
        )
    )
    conflict = fixture.dataset.conflicts[0]

    assert fixture.dataset.timezone == "America/New_York"
    assert (conflict.field, conflict.primary_value, conflict.secondary_value) == (
        "close",
        Decimal("101.00"),
        Decimal("103.00"),
    )
    assert observation.state is ProviderDataState.CONFLICT
    assert observation.data == ()
    assert observation.completeness == 0
    assert observation.reasons == ("reconciliation_threshold_exceeded",)


def test_manifest_source_time_and_sha256_match_synthetic_fixtures() -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)

    for fixture in adapter.fixtures:
        entry = fixture.entry
        raw = (FIXTURE_ROOT / entry.path).read_bytes()
        assert entry.source == "stonks-agent-synthetic"
        assert entry.source_time <= entry.observed_at
        assert hashlib.sha256(raw).hexdigest() == entry.sha256
        assert entry.license_tag == "CC0-1.0"
        assert entry.redistribution_tag == "synthetic-unrestricted"


def test_tampered_fixture_hash_fails_closed(tmp_path: Path) -> None:
    copied = tmp_path / "market_data"
    shutil.copytree(FIXTURE_ROOT, copied)
    target = copied / "us_daily_dst_actions.json"
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(ReplayFixtureIntegrityError, match="SHA-256 mismatch"):
        ReplayMarketDataAdapter(copied / "manifest.yaml")


def test_incoherent_manifest_state_is_typed_integrity_failure(tmp_path: Path) -> None:
    copied = tmp_path / "market_data"
    shutil.copytree(FIXTURE_ROOT, copied)
    manifest_path = copied / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["fixtures"][0]["completeness"] = "0.5"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(
        ReplayFixtureIntegrityError, match="fixture state validation failed"
    ):
        ReplayMarketDataAdapter(manifest_path)


def test_conflict_state_requires_recorded_conflict_evidence(tmp_path: Path) -> None:
    copied = tmp_path / "market_data"
    shutil.copytree(FIXTURE_ROOT, copied)
    fixture_path = copied / "us_intraday_conflict.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["conflicts"] = []
    fixture_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    manifest_path = copied / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in manifest["fixtures"]
        if item["fixture_id"] == "us-intraday-conflict"
    )
    entry["sha256"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ReplayFixtureIntegrityError, match="conflict evidence"):
        ReplayMarketDataAdapter(manifest_path)


def test_future_fixture_is_fetch_failed_not_empty_success() -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)
    entry = adapter.fixtures[0].entry

    observation = adapter.fetch(
        _request(
            market=entry.market,
            symbol=entry.symbol,
            interval=entry.interval,
            scenario=entry.scenario,
            as_of=entry.as_of - timedelta(microseconds=1),
        )
    )

    assert observation.state is ProviderDataState.FETCH_FAILED
    assert observation.data == ()
    assert observation.completeness == Decimal("0")
    assert observation.reasons == ("replay_fixture_not_available_at_as_of",)


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"symbol": "AAPL", "interval": "1d", "scenario": "canonical", "token": "x"},
        {"symbol": "UNKNOWN", "interval": "1d", "scenario": "canonical"},
    ],
)
def test_invalid_or_unknown_query_never_becomes_empty_success(
    query: dict[str, object],
) -> None:
    adapter = ReplayMarketDataAdapter(MANIFEST)

    observation = adapter.fetch(
        FetchDataRequest(
            market="US",
            capability="prices",
            as_of=datetime(2026, 3, 10, 22, tzinfo=UTC),
            query=query,
        )
    )

    assert observation.state in {
        ProviderDataState.FETCH_FAILED,
        ProviderDataState.NOT_SUPPORTED,
    }
    assert observation.data == ()
    assert observation.reasons


def test_fixture_tree_contains_no_secret_material_or_restricted_data() -> None:
    forbidden_keys = {"api_key", "token", "password", "secret", "credential", "auth"}
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE_ROOT.glob("*.json")
    ]

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return {str(key).lower() for key in value} | {
                nested for item in value.values() for nested in keys(item)
            }
        if isinstance(value, list):
            return {nested for item in value for nested in keys(item)}
        return set()

    assert (
        not (keys(manifest) | {key for payload in payloads for key in keys(payload)})
        & forbidden_keys
    )
    assert all(
        item["source"] == "stonks-agent-synthetic" for item in manifest["fixtures"]
    )
