from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from stonks_contracts.backtest import (
    BacktestBar,
    BacktestCalendar,
    BacktestCashBalance,
    BacktestCostModel,
    BacktestDataset,
    BacktestEngineKind,
    BacktestFill,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderOutcome,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestPosition,
    BacktestResult,
    BacktestRuntimeIdentity,
    BacktestSession,
    BacktestTimeInForce,
)

DAY_1_OPEN = datetime(2026, 7, 13, 14, 30, tzinfo=UTC)
DAY_1_CLOSE = datetime(2026, 7, 13, 21, tzinfo=UTC)
DAY_2_OPEN = datetime(2026, 7, 14, 14, 30, tzinfo=UTC)
DAY_2_CLOSE = datetime(2026, 7, 14, 21, tzinfo=UTC)
REQUESTED_AT = DAY_2_CLOSE + timedelta(minutes=2)
DEADLINE = REQUESTED_AT + timedelta(minutes=5)

RUN_ID = UUID("20000000-0000-4000-8000-000000000001")
REQUEST_ID = UUID("20000000-0000-4000-8000-000000000002")
DATASET_ID = UUID("20000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("20000000-0000-4000-8000-000000000004")
ORDER_ID = UUID("20000000-0000-4000-8000-000000000005")
BAR_1_ID = UUID("20000000-0000-4000-8000-000000000006")
BAR_2_ID = UUID("20000000-0000-4000-8000-000000000007")

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

_ENGINE_INDEX = {
    BacktestEngineKind.REFERENCE: 1,
    BacktestEngineKind.NAUTILUS: 2,
    BacktestEngineKind.LEAN: 3,
}
_ENGINE_HASH = {
    BacktestEngineKind.REFERENCE: HASH_A,
    BacktestEngineKind.NAUTILUS: HASH_B,
    BacktestEngineKind.LEAN: HASH_C,
}


def runtime(engine: BacktestEngineKind) -> BacktestRuntimeIdentity:
    digest = _ENGINE_HASH[engine]
    return BacktestRuntimeIdentity(
        engine=engine,
        engine_version=f"fixture-{engine.value}-1",
        adapter_version="1.0.0",
        runtime_hash=digest,
        image_digest=None
        if engine is BacktestEngineKind.REFERENCE
        else f"sha256:{digest}",
        deterministic=True,
    )


def instrument() -> BacktestInstrument:
    return BacktestInstrument(
        instrument_id=INSTRUMENT_ID,
        symbol="AAPL",
        mic="XNAS",
        asset_class="equity",
        currency="USD",
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("1"),
    )


def calendar() -> BacktestCalendar:
    return BacktestCalendar(
        calendar_id="parity-xnas",
        version="2026.07",
        timezone="America/New_York",
        sessions=(
            BacktestSession(
                session_date=date(2026, 7, 13),
                mic="XNAS",
                opens_at=DAY_1_OPEN,
                closes_at=DAY_1_CLOSE,
            ),
            BacktestSession(
                session_date=date(2026, 7, 14),
                mic="XNAS",
                opens_at=DAY_2_OPEN,
                closes_at=DAY_2_CLOSE,
            ),
        ),
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
        source_ref=f"parity:{bar_id}",
        source_hash=source_hash,
        tradable=True,
    )


def dataset() -> BacktestDataset:
    selected_calendar = calendar()
    return BacktestDataset(
        dataset_id=DATASET_ID,
        as_of=DAY_2_CLOSE + timedelta(minutes=1),
        interval="1d",
        adjustment="split_dividend_adjusted",
        instruments=(instrument(),),
        calendar=selected_calendar,
        bars=(
            _bar(BAR_1_ID, DAY_1_OPEN, DAY_1_CLOSE, HASH_A),
            _bar(BAR_2_ID, DAY_2_OPEN, DAY_2_CLOSE, HASH_B),
        ),
    )


def cost_model() -> BacktestCostModel:
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


def order() -> BacktestOrder:
    return BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("10"),
        limit_price=None,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_CLOSE,
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="parity:buy-market-day",
    )


def job(engine: BacktestEngineKind) -> BacktestJob:
    selected_dataset = dataset()
    index = _ENGINE_INDEX[engine]
    return BacktestJob(
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        job_id=UUID(f"20000000-0000-4000-8000-{index:012d}"),
        attempt_generation=1,
        attempt_nonce=f"parity-{engine.value}-1",
        runtime=runtime(engine),
        strategy_artifact_ref=f"sha256:{HASH_A}",
        strategy_content_hash=HASH_A,
        dataset_artifact_ref=f"sha256:{selected_dataset.payload_hash()}",
        dataset=selected_dataset,
        cost_model=cost_model(),
        orders=(order(),),
        initial_cash=(
            BacktestCashBalance(
                currency="USD",
                amount=Decimal("10000"),
                quantum=Decimal("0.01"),
            ),
        ),
        initial_positions=(
            BacktestPosition(
                instrument_id=INSTRUMENT_ID,
                quantity=Decimal("0"),
                quantity_quantum=Decimal("1"),
            ),
        ),
        requested_at=REQUESTED_AT,
        deadline=DEADLINE,
    )


def jobs() -> tuple[BacktestJob, ...]:
    return tuple(job(engine) for engine in BacktestEngineKind)


def result(
    selected_job: BacktestJob,
    *,
    warnings: tuple[str, ...] = (),
    price_delta: Decimal = Decimal("0"),
) -> BacktestResult:
    index = _ENGINE_INDEX[selected_job.runtime.engine]
    price = Decimal("100.04") + price_delta
    cash = Decimal("8999.39") - price_delta * Decimal("10")
    fill = BacktestFill.create(
        fill_id=UUID(f"20000000-0000-4000-9000-{index:012d}"),
        order_id=ORDER_ID,
        order_hash=order().order_hash,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        quantity=Decimal("10"),
        quantity_quantum=Decimal("1"),
        price=price,
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=Decimal("0.21"),
        fee_quantum=Decimal("0.01"),
        slippage=Decimal("0.04") + price_delta,
        occurred_at=DAY_2_OPEN,
        source_bar_id=BAR_2_ID,
        external_ref=f"{selected_job.runtime.engine.value}:native-fill:{index}",
    )
    outcome = BacktestOrderOutcome(
        order_id=ORDER_ID,
        order_hash=order().order_hash,
        status=BacktestOrderStatus.FILLED,
        command_quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        remaining_quantity=Decimal("0"),
        reason=None,
    )
    return BacktestResult.create(
        result_id=UUID(f"20000000-0000-4000-a000-{index:012d}"),
        job=selected_job,
        order_outcomes=(outcome,),
        fills=(fill,),
        final_cash=(
            BacktestCashBalance(
                currency="USD",
                amount=cash,
                quantum=Decimal("0.01"),
            ),
        ),
        final_positions=(
            BacktestPosition(
                instrument_id=INSTRUMENT_ID,
                quantity=Decimal("10"),
                quantity_quantum=Decimal("1"),
            ),
        ),
        total_fees=Decimal("0.21"),
        generated_at=REQUESTED_AT + timedelta(minutes=1),
        warnings=warnings,
    )
