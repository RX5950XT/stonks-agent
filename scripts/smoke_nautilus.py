#!/usr/bin/env python3
"""Send a deterministic canonical replay through a running Nautilus sidecar."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from urllib.request import Request, urlopen
from uuid import UUID

from stonks_contracts.backtest import (
    BacktestBar,
    BacktestCalendar,
    BacktestCashBalance,
    BacktestCostModel,
    BacktestDataset,
    BacktestEngineKind,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderSide,
    BacktestOrderType,
    BacktestPosition,
    BacktestResult,
    BacktestRuntimeIdentity,
    BacktestSession,
    BacktestTimeInForce,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
INSTRUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
ORDER_ID = UUID("10000000-0000-4000-8000-000000000002")


def _at(day: date, value: time) -> datetime:
    return datetime.combine(day, value, tzinfo=UTC)


def _job(runtime_hash: str, image_digest: str, now: datetime) -> BacktestJob:
    first_day = (now - timedelta(days=4)).date()
    second_day = (now - timedelta(days=1)).date()
    first_open, first_close = _at(first_day, time(14, 30)), _at(first_day, time(21))
    second_open, second_close = _at(second_day, time(14, 30)), _at(second_day, time(21))
    instrument = BacktestInstrument(
        instrument_id=INSTRUMENT_ID,
        symbol="AAPL",
        mic="XNAS",
        asset_class="equity",
        currency="USD",
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("1"),
    )
    calendar = BacktestCalendar(
        calendar_id="nautilus-smoke-xnas",
        version="1",
        timezone="America/New_York",
        sessions=(
            BacktestSession(
                session_date=first_day,
                mic="XNAS",
                opens_at=first_open,
                closes_at=first_close,
            ),
            BacktestSession(
                session_date=second_day,
                mic="XNAS",
                opens_at=second_open,
                closes_at=second_close,
            ),
        ),
    )
    bars = (
        _bar(
            UUID("10000000-0000-4000-8000-000000000003"),
            first_open,
            first_close,
            HASH_A,
        ),
        _bar(
            UUID("10000000-0000-4000-8000-000000000004"),
            second_open,
            second_close,
            HASH_B,
        ),
    )
    dataset = BacktestDataset(
        dataset_id=UUID("10000000-0000-4000-8000-000000000005"),
        as_of=second_close + timedelta(minutes=1),
        interval="1d",
        adjustment="split_dividend_adjusted",
        instruments=(instrument,),
        calendar=calendar,
        bars=bars,
    )
    order = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("10"),
        limit_price=None,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=first_close,
        valid_until=second_close,
        strategy_event_ref="smoke:prior-close",
    )
    return BacktestJob(
        request_id=UUID("10000000-0000-4000-8000-000000000006"),
        run_id=UUID("10000000-0000-4000-8000-000000000007"),
        job_id=UUID("10000000-0000-4000-8000-000000000008"),
        attempt_generation=1,
        attempt_nonce="nautilus-smoke-1",
        runtime=BacktestRuntimeIdentity(
            engine=BacktestEngineKind.NAUTILUS,
            engine_version="1.230.0",
            adapter_version="0.1.0",
            runtime_hash=runtime_hash,
            image_digest=image_digest,
            deterministic=True,
        ),
        strategy_artifact_ref=f"sha256:{HASH_A}",
        strategy_content_hash=HASH_A,
        dataset_artifact_ref=f"sha256:{dataset.payload_hash()}",
        dataset=dataset,
        cost_model=_cost_model(),
        orders=(order,),
        initial_cash=(
            BacktestCashBalance(
                currency="USD", amount=Decimal("10000"), quantum=Decimal("0.01")
            ),
        ),
        initial_positions=(
            BacktestPosition(
                instrument_id=INSTRUMENT_ID,
                quantity=Decimal("0"),
                quantity_quantum=Decimal("1"),
            ),
        ),
        requested_at=now - timedelta(seconds=1),
        deadline=now + timedelta(minutes=2),
    )


def _bar(
    bar_id: UUID,
    opens_at: datetime,
    closes_at: datetime,
    source_hash: str,
) -> BacktestBar:
    return BacktestBar(
        bar_id=bar_id,
        instrument_id=INSTRUMENT_ID,
        opens_at=opens_at,
        closes_at=closes_at,
        available_at=closes_at + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1000"),
        source_ref=f"smoke:{bar_id}",
        source_hash=source_hash,
        tradable=True,
    )


def _cost_model() -> BacktestCostModel:
    return BacktestCostModel(
        model_kind="deterministic_next_bar",
        realism_claim="reference_model_not_market_replay",
        max_volume_participation=Decimal("0.1"),
        half_spread_bps=Decimal("1"),
        base_slippage_bps=Decimal("2"),
        market_impact_bps_at_max_participation=Decimal("3"),
        fee_bps=Decimal("1"),
        per_unit_fee=Decimal("0.01"),
        minimum_fee=Decimal("0.05"),
        fee_quantum=Decimal("0.01"),
    )


def _send(
    base_url: str, job: BacktestJob, timeout: float, service_token: str
) -> BacktestResult:
    request = Request(
        f"{base_url.rstrip('/')}/v1/backtests",
        data=job.model_dump_json().encode("utf-8"),
        headers={
            "Authorization": f"Bearer {service_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if payload.get("success") is not True:
        raise RuntimeError("Nautilus smoke request failed")
    result = BacktestResult.model_validate(payload["data"])
    result.validate_against(job)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7400")
    parser.add_argument("--runtime-hash", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--service-token", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    job = _job(args.runtime_hash, args.image_digest, datetime.now(UTC))
    first = _send(args.base_url, job, args.timeout, args.service_token)
    second = _send(args.base_url, job, args.timeout, args.service_token)
    if first.semantic_hash != second.semantic_hash or tuple(
        item.external_ref for item in first.fills
    ) != tuple(item.external_ref for item in second.fills):
        raise RuntimeError("Nautilus semantic or raw-reference replay changed")
    print(
        json.dumps(
            {
                "engine": first.runtime.engine.value,
                "fills": len(first.fills),
                "semantic_hash": first.semantic_hash,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
