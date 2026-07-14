from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from stonks_agent.application.evaluation.backtest import run_backtest
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.backtest_engine import BacktestEnginePort
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
from stonks_contracts.backtest_math import canonical_fill_fee, canonical_fill_price

DAY_1_OPEN = datetime(2026, 7, 13, 14, 30, tzinfo=UTC)
DAY_1_CLOSE = datetime(2026, 7, 13, 21, tzinfo=UTC)
DAY_2_OPEN = datetime(2026, 7, 14, 14, 30, tzinfo=UTC)
DAY_2_CLOSE = datetime(2026, 7, 14, 21, tzinfo=UTC)
REQUESTED = datetime(2026, 7, 14, 22, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-4000-8000-000000000001")
JOB_ID = UUID("00000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000003")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000004")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000005")
BAR_1_ID = UUID("00000000-0000-4000-8000-000000000006")
BAR_2_ID = UUID("00000000-0000-4000-8000-000000000007")
FILL_ID = UUID("00000000-0000-4000-8000-000000000008")
HASH_A = "a" * 64
HASH_B = "b" * 64


class StaticEngine:
    def __init__(self, response: Result[BacktestResult]) -> None:
        self.response = response
        self.jobs: list[BacktestJob] = []

    def run(self, job: BacktestJob) -> Result[BacktestResult]:
        self.jobs.append(job)
        return self.response


def runtime(
    engine: BacktestEngineKind = BacktestEngineKind.REFERENCE,
) -> BacktestRuntimeIdentity:
    return BacktestRuntimeIdentity(
        engine=engine,
        engine_version="1.0.0",
        adapter_version="1.0.0",
        runtime_hash=HASH_A if engine is BacktestEngineKind.REFERENCE else HASH_B,
        image_digest=None
        if engine is BacktestEngineKind.REFERENCE
        else f"sha256:{HASH_B}",
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


def sessions() -> tuple[BacktestSession, ...]:
    return (
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
    )


def bar(
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
        source_ref=f"fixture:{bar_id}",
        source_hash=source_hash,
        tradable=True,
    )


def dataset() -> BacktestDataset:
    calendar = BacktestCalendar(
        calendar_id="xnas-fixture",
        version="2026.07",
        timezone="America/New_York",
        sessions=sessions(),
    )
    return BacktestDataset(
        dataset_id=uuid4(),
        as_of=DAY_2_CLOSE + timedelta(minutes=1),
        interval="1d",
        adjustment="split_dividend_adjusted",
        instruments=(instrument(),),
        calendar=calendar,
        bars=(
            bar(BAR_1_ID, DAY_1_OPEN, DAY_1_CLOSE, HASH_A),
            bar(BAR_2_ID, DAY_2_OPEN, DAY_2_CLOSE, HASH_B),
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
        strategy_event_ref="signal:aapl:2026-07-13",
    )


def job(
    engine: BacktestEngineKind = BacktestEngineKind.REFERENCE,
) -> BacktestJob:
    data = dataset()
    return BacktestJob(
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        job_id=JOB_ID,
        attempt_generation=1,
        attempt_nonce="nonce-1",
        runtime=runtime(engine),
        strategy_artifact_ref=f"sha256:{HASH_A}",
        strategy_content_hash=HASH_A,
        dataset_artifact_ref=f"sha256:{data.payload_hash()}",
        dataset=data,
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
        requested_at=REQUESTED,
        deadline=REQUESTED + timedelta(minutes=5),
    )


def canonical_fill(
    *,
    source_bar_id: UUID = BAR_2_ID,
    occurred_at: datetime = DAY_2_OPEN,
    fees: Decimal = Decimal("0.21"),
) -> BacktestFill:
    return BacktestFill.create(
        fill_id=FILL_ID,
        order_id=ORDER_ID,
        order_hash=order().order_hash,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        quantity=Decimal("10"),
        quantity_quantum=Decimal("1"),
        price=Decimal("100.04"),
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=fees,
        fee_quantum=Decimal("0.01"),
        slippage=Decimal("0.04"),
        occurred_at=occurred_at,
        source_bar_id=source_bar_id,
        external_ref="fixture-fill-1",
    )


def canonical_result(
    target_job: BacktestJob,
    *,
    fill: BacktestFill | None = None,
    cash: Decimal = Decimal("8999.39"),
    quantity: Decimal = Decimal("10"),
) -> BacktestResult:
    selected_fill = fill or canonical_fill()
    outcome = BacktestOrderOutcome(
        order_id=ORDER_ID,
        order_hash=order().order_hash,
        status=BacktestOrderStatus.FILLED,
        command_quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        remaining_quantity=Decimal("0"),
    )
    return BacktestResult.create(
        result_id=uuid4(),
        job=target_job,
        order_outcomes=(outcome,),
        fills=(selected_fill,),
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
                quantity=quantity,
                quantity_quantum=Decimal("1"),
            ),
        ),
        total_fees=selected_fill.fees,
        generated_at=REQUESTED + timedelta(seconds=5),
    )


def job_with_order(
    selected: BacktestOrder, *, initial_quantity: Decimal
) -> BacktestJob:
    base = job()
    payload = base.model_dump(mode="json")
    payload["orders"] = [selected.model_dump(mode="json")]
    payload["initial_positions"] = [
        BacktestPosition(
            instrument_id=INSTRUMENT_ID,
            quantity=initial_quantity,
            quantity_quantum=Decimal("1"),
        ).model_dump(mode="json")
    ]
    return BacktestJob.model_validate(payload)


def result_for_order(
    target_job: BacktestJob,
    selected: BacktestOrder,
    selected_fill: BacktestFill,
    *,
    cash: Decimal,
    quantity: Decimal,
    generated_at: datetime = REQUESTED + timedelta(seconds=5),
) -> BacktestResult:
    return BacktestResult.create(
        result_id=uuid4(),
        job=target_job,
        order_outcomes=(
            BacktestOrderOutcome(
                order_id=selected.order_id,
                order_hash=selected.order_hash,
                status=BacktestOrderStatus.FILLED,
                command_quantity=selected.quantity,
                filled_quantity=selected.quantity,
                remaining_quantity=Decimal("0"),
            ),
        ),
        fills=(selected_fill,),
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
                quantity=quantity,
                quantity_quantum=Decimal("1"),
            ),
        ),
        total_fees=selected_fill.fees,
        generated_at=generated_at,
    )


def test_job_is_frozen_hash_bound_and_retry_input_hash_is_stable() -> None:
    original = job()
    retry = original.model_copy(
        update={"attempt_generation": 2, "attempt_nonce": "nonce-2"}
    )

    assert original.dataset_artifact_ref == f"sha256:{original.dataset.payload_hash()}"
    assert original.input_hash == retry.input_hash
    assert original.job_hash != retry.job_hash
    assert isinstance(
        StaticEngine(Success(canonical_result(original))), BacktestEnginePort
    )
    with pytest.raises(ValidationError):
        BacktestJob.model_validate(
            original.model_dump(mode="json") | {"execution_mode": "paper"}
        )


def test_valid_result_reduces_cash_positions_and_replays_semantically() -> None:
    request = job()
    candidate = canonical_result(request)
    engine = StaticEngine(Success(candidate))

    accepted = run_backtest(request, engine)

    assert isinstance(accepted, Success)
    assert accepted.value.result_hash == candidate.result_hash
    assert accepted.value.semantic_hash == candidate.expected_semantic_hash()
    assert accepted.value.final_cash[0].amount == Decimal("8999.39")
    assert accepted.value.final_positions[0].quantity == Decimal("10")
    assert engine.jobs == [request]

    other_job = request.model_copy(
        update={"runtime": runtime(BacktestEngineKind.NAUTILUS)}
    )
    other = canonical_result(other_job)
    assert request.input_hash == other_job.input_hash
    assert request.job_hash != other_job.job_hash
    assert candidate.semantic_hash == other.semantic_hash


@pytest.mark.parametrize(
    ("bad_fill", "cash"),
    [
        (canonical_fill(fees=Decimal("0.20")), Decimal("8999.40")),
        (
            canonical_fill(source_bar_id=BAR_1_ID, occurred_at=DAY_1_OPEN),
            Decimal("8999.39"),
        ),
    ],
)
def test_fee_drift_and_same_bar_lookahead_fail_closed(
    bad_fill: BacktestFill, cash: Decimal
) -> None:
    request = job()
    candidate = canonical_result(request, fill=bad_fill, cash=cash)

    result = run_backtest(request, StaticEngine(Success(candidate)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_market_order_cannot_skip_first_tradable_bar() -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("10"),
        limit_price=None,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_OPEN - timedelta(minutes=1),
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:2026-07-13",
    )
    request = job_with_order(selected, initial_quantity=Decimal("0"))
    skipped = BacktestFill.create(
        fill_id=FILL_ID,
        order_id=selected.order_id,
        order_hash=selected.order_hash,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        quantity=Decimal("10"),
        quantity_quantum=Decimal("1"),
        price=Decimal("100.04"),
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=Decimal("0.21"),
        fee_quantum=Decimal("0.01"),
        slippage=Decimal("0.04"),
        occurred_at=DAY_2_OPEN,
        source_bar_id=BAR_2_ID,
        external_ref="skipped-first-bar",
    )
    candidate = result_for_order(
        request,
        selected,
        skipped,
        cash=Decimal("8999.39"),
        quantity=Decimal("10"),
    )

    result = run_backtest(request, StaticEngine(Success(candidate)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_fillable_order_cannot_be_reported_without_its_deterministic_fill() -> None:
    request = job()
    candidate = BacktestResult.create(
        result_id=uuid4(),
        job=request,
        order_outcomes=(
            BacktestOrderOutcome(
                order_id=ORDER_ID,
                order_hash=order().order_hash,
                status=BacktestOrderStatus.EXPIRED,
                command_quantity=Decimal("10"),
                filled_quantity=Decimal("0"),
                remaining_quantity=Decimal("10"),
                reason="engine_dropped_fill",
            ),
        ),
        fills=(),
        final_cash=request.initial_cash,
        final_positions=request.initial_positions,
        total_fees=Decimal("0"),
        generated_at=REQUESTED,
    )

    result = run_backtest(request, StaticEngine(Success(candidate)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_final_projection_and_attempt_fence_drift_fail_closed() -> None:
    request = job()
    wrong_position = canonical_result(request, quantity=Decimal("9"))
    result = run_backtest(request, StaticEngine(Success(wrong_position)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID

    wrong_nonce = canonical_result(request).model_copy(update={"attempt_nonce": "late"})
    result = run_backtest(request, StaticEngine(Success(wrong_nonce)))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_calendar_and_dataset_reject_overlap_future_and_unstable_ordering() -> None:
    first, second = sessions()
    with pytest.raises(ValidationError):
        BacktestCalendar(
            calendar_id="bad",
            version="1",
            timezone="America/New_York",
            sessions=(second, first),
        )

    valid = dataset()
    future = valid.bars[1].model_copy(
        update={"available_at": valid.as_of + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError):
        BacktestDataset.model_validate(
            valid.model_dump(mode="json") | {"bars": [valid.bars[0], future]}
        )


@pytest.mark.parametrize(
    (
        "side",
        "order_type",
        "limit_price",
        "price",
        "fees",
        "slippage",
        "cash",
        "position",
    ),
    [
        (
            BacktestOrderSide.SELL,
            BacktestOrderType.MARKET,
            None,
            Decimal("99.96"),
            Decimal("0.20"),
            Decimal("-0.04"),
            Decimal("10999.40"),
            Decimal("0"),
        ),
        (
            BacktestOrderSide.BUY,
            BacktestOrderType.LIMIT,
            Decimal("100.02"),
            Decimal("100.02"),
            Decimal("0.21"),
            Decimal("0.02"),
            Decimal("8999.59"),
            Decimal("10"),
        ),
    ],
)
def test_sell_and_limit_orders_share_reference_cost_semantics(
    side: BacktestOrderSide,
    order_type: BacktestOrderType,
    limit_price: Decimal | None,
    price: Decimal,
    fees: Decimal,
    slippage: Decimal,
    cash: Decimal,
    position: Decimal,
) -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=side,
        order_type=order_type,
        quantity=Decimal("10"),
        limit_price=limit_price,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_CLOSE,
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:2026-07-13",
    )
    request = job_with_order(
        selected,
        initial_quantity=Decimal("10")
        if side is BacktestOrderSide.SELL
        else Decimal("0"),
    )
    selected_fill = BacktestFill.create(
        fill_id=FILL_ID,
        order_id=selected.order_id,
        order_hash=selected.order_hash,
        instrument_id=INSTRUMENT_ID,
        side=side,
        quantity=Decimal("10"),
        quantity_quantum=Decimal("1"),
        price=price,
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=fees,
        fee_quantum=Decimal("0.01"),
        slippage=slippage,
        occurred_at=DAY_2_OPEN,
        source_bar_id=BAR_2_ID,
        external_ref="fixture-fill-side",
    )
    candidate = result_for_order(
        request,
        selected,
        selected_fill,
        cash=cash,
        quantity=position,
    )

    result = run_backtest(request, StaticEngine(Success(candidate)))

    assert isinstance(result, Success)


def test_engine_failure_deadline_and_projection_quantum_fail_closed() -> None:
    request = job()
    unavailable = Failure(
        StructuredError(code=ErrorCode.INTERNAL_ERROR, message="engine unavailable")
    )
    assert run_backtest(request, StaticEngine(unavailable)) is unavailable

    late = result_for_order(
        request,
        order(),
        canonical_fill(),
        cash=Decimal("8999.39"),
        quantity=Decimal("10"),
        generated_at=request.deadline + timedelta(seconds=1),
    )
    result = run_backtest(request, StaticEngine(Success(late)))
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID

    drifted_cash = canonical_result(request).model_copy(
        update={
            "final_cash": (
                BacktestCashBalance(
                    currency="USD",
                    amount=Decimal("8999.390"),
                    quantum=Decimal("0.001"),
                ),
            )
        }
    )
    result = run_backtest(request, StaticEngine(Success(drifted_cash)))
    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_external_runtime_requires_content_addressed_image() -> None:
    with pytest.raises(ValidationError, match="image digest"):
        BacktestRuntimeIdentity(
            engine=BacktestEngineKind.LEAN,
            engine_version="1.0.0",
            adapter_version="1.0.0",
            runtime_hash=HASH_A,
            deterministic=True,
        )


def test_canonical_cost_math_ignores_ambient_decimal_context() -> None:
    selected_order = order()
    selected_bar = dataset().bars[1]
    selected_instrument = instrument()
    selected_cost = cost_model()
    expected_price = canonical_fill_price(
        selected_order, selected_bar, selected_instrument, selected_cost
    )
    assert expected_price is not None
    expected_fee = canonical_fill_fee(
        selected_order.quantity, expected_price, selected_cost
    )

    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        actual_price = canonical_fill_price(
            selected_order, selected_bar, selected_instrument, selected_cost
        )
        assert actual_price is not None
        actual_fee = canonical_fill_fee(
            selected_order.quantity, actual_price, selected_cost
        )

    assert actual_price == expected_price == Decimal("100.04")
    assert actual_fee == expected_fee == Decimal("0.21")


def test_cost_model_rejects_non_positive_worst_case_sell_price() -> None:
    payload = cost_model().model_dump(mode="json")
    payload.update(
        half_spread_bps="5000",
        base_slippage_bps="3000",
        market_impact_bps_at_max_participation="2000",
    )

    with pytest.raises(ValidationError, match="aggregate adverse"):
        BacktestCostModel.model_validate(payload)


def test_sell_price_that_rounds_to_zero_fails_as_validation_error() -> None:
    base = job()
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.SELL,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("1"),
        limit_price=None,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_CLOSE,
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:coarse-quantum",
    )
    coarse_instrument = instrument().model_copy(
        update={"price_quantum": Decimal("100")}
    )
    coarse_bars = tuple(
        item.model_copy(
            update={
                "open": Decimal("100"),
                "high": Decimal("100"),
                "low": Decimal("100"),
                "close": Decimal("100"),
            }
        )
        for item in base.dataset.bars
    )
    coarse_dataset = base.dataset.model_copy(
        update={"instruments": (coarse_instrument,), "bars": coarse_bars}
    )
    payload = base.model_dump(mode="json")
    payload.update(
        dataset=coarse_dataset.model_dump(mode="json"),
        dataset_artifact_ref=f"sha256:{coarse_dataset.payload_hash()}",
        orders=[selected.model_dump(mode="json")],
        initial_positions=[
            BacktestPosition(
                instrument_id=INSTRUMENT_ID,
                quantity=Decimal("1"),
                quantity_quantum=Decimal("1"),
            ).model_dump(mode="json")
        ],
    )

    with pytest.raises(ValidationError, match="price bounds"):
        BacktestJob.model_validate(payload)


def test_extreme_decimal_operations_fail_as_structured_validation() -> None:
    with pytest.raises(ValidationError, match="cash must match"):
        BacktestCashBalance(
            currency="USD",
            amount=Decimal("1e64"),
            quantum=Decimal("1e-64"),
        )

    payload = cost_model().model_dump(mode="json")
    payload.update(fee_bps="10000", per_unit_fee="0", minimum_fee="0")
    extreme_cost = BacktestCostModel.model_validate(payload)
    with pytest.raises(ValueError, match="supported bounds"):
        canonical_fill_fee(Decimal("1e64"), Decimal("1e64"), extreme_cost)
