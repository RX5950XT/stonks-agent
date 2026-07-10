from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.instrument import Instrument, ProviderSymbolMapping
from stonks_contracts.instrument import AssetClass

INSTRUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")


def mapping(
    symbol: str,
    valid_from: datetime,
    valid_to: datetime | None = None,
) -> ProviderSymbolMapping:
    return ProviderSymbolMapping(
        provider="example",
        symbol=symbol,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def test_provider_symbol_changes_are_resolved_point_in_time() -> None:
    changed_at = datetime(2025, 1, 1, tzinfo=UTC)
    instrument = Instrument(
        instrument_id=INSTRUMENT_ID,
        asset_class=AssetClass.EQUITY,
        primary_symbol="NEW",
        exchange_mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        provider_symbols=(
            mapping("OLD", datetime(2020, 1, 1, tzinfo=UTC), changed_at),
            mapping("NEW", changed_at),
        ),
    )

    assert instrument.provider_symbol("example", changed_at) == "NEW"
    assert (
        instrument.provider_symbol(
            "example",
            datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        == "OLD"
    )


def test_overlapping_provider_symbol_windows_fail_closed() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        Instrument(
            instrument_id=INSTRUMENT_ID,
            asset_class=AssetClass.EQUITY,
            primary_symbol="AAPL",
            exchange_mic="XNAS",
            currency="USD",
            timezone="America/New_York",
            provider_symbols=(
                mapping(
                    "AAPL",
                    datetime(2020, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 2, tzinfo=UTC),
                ),
                mapping("AAPL2", datetime(2025, 1, 1, tzinfo=UTC)),
            ),
        )


def test_unknown_symbol_at_as_of_is_not_silently_fallback() -> None:
    instrument = Instrument(
        instrument_id=INSTRUMENT_ID,
        asset_class=AssetClass.EQUITY,
        primary_symbol="AAPL",
        exchange_mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        provider_symbols=(mapping("AAPL", datetime(2025, 1, 1, tzinfo=UTC)),),
    )

    with pytest.raises(LookupError, match="no provider symbol"):
        instrument.provider_symbol(
            "example",
            datetime(2024, 12, 31, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("exchange_mic", "nasdaq"), ("currency", "US"), ("timezone", "Mars/Base")],
)
def test_instrument_reference_data_is_validated(field: str, value: str) -> None:
    payload = {
        "instrument_id": INSTRUMENT_ID,
        "asset_class": AssetClass.EQUITY,
        "primary_symbol": "AAPL",
        "exchange_mic": "XNAS",
        "currency": "USD",
        "timezone": "America/New_York",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        Instrument.model_validate(payload)
