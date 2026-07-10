from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stonks_contracts.common import Money, stable_payload_hash


def test_contract_is_frozen_and_rejects_extra_fields() -> None:
    money = Money(currency="USD", amount=Decimal("1.20"))

    with pytest.raises(ValidationError, match="frozen"):
        money.amount = Decimal("2.00")  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Money.model_validate({"currency": "USD", "amount": "1.20", "secret": "nope"})


def test_decimal_accepts_only_strings_or_decimal_and_serializes_as_string() -> None:
    from_decimal = Money(currency="USD", amount=Decimal("1.20"))
    from_string = Money.model_validate({"currency": "USD", "amount": "1.20"})

    assert from_decimal.amount == from_string.amount == Decimal("1.20")
    assert from_decimal.model_dump(mode="json")["amount"] == "1.20"
    with pytest.raises(ValidationError, match="Decimal input must be a string"):
        Money.model_validate({"currency": "USD", "amount": 1.2})


def test_utc_datetime_rejects_naive_and_normalizes_aware_values() -> None:
    from stonks_contracts.workflow import Run

    with pytest.raises(ValidationError, match="timezone-aware"):
        Run.model_validate(
            {
                "run_id": "00000000-0000-4000-8000-000000000001",
                "state": "pending",
                "as_of": datetime(2026, 7, 10),
                "created_at": "2026-07-10T00:00:00Z",
                "deadline": "2026-07-11T00:00:00Z",
                "policy_snapshot_ref": "sha256:" + "a" * 64,
                "config_snapshot_ref": "sha256:" + "b" * 64,
            }
        )

    run = Run.model_validate(
        {
            "run_id": "00000000-0000-4000-8000-000000000001",
            "state": "pending",
            "as_of": datetime(2026, 7, 10, 16, tzinfo=timezone(timedelta(hours=8))),
            "created_at": "2026-07-10T08:00:00Z",
            "deadline": "2026-07-11T08:00:00Z",
            "policy_snapshot_ref": "sha256:" + "a" * 64,
            "config_snapshot_ref": "sha256:" + "b" * 64,
        }
    )

    assert run.as_of == datetime(2026, 7, 10, 8, tzinfo=UTC)
    assert run.model_dump(mode="json")["as_of"] == "2026-07-10T08:00:00Z"


def test_payload_hash_is_stable_across_mapping_order_and_round_trip() -> None:
    first = {"schema_version": "1.0.0", "nested": {"b": "2", "a": "1"}}
    second = {"nested": {"a": "1", "b": "2"}, "schema_version": "1.0.0"}
    money = Money(currency="USD", amount=Decimal("1.20"))

    assert stable_payload_hash(first) == stable_payload_hash(second)
    assert money.payload_hash() == Money.model_validate_json(money.model_dump_json()).payload_hash()
    assert len(money.payload_hash()) == 64
    assert money.schema_version == "1.0.0"
