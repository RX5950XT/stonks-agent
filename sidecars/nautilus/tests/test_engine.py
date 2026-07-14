from __future__ import annotations

import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sidecars.nautilus import engine as engine_module  # noqa: E402
from sidecars.nautilus.adapter import (  # noqa: E402
    AdapterPolicy,
    EngineFailure,
    NautilusAdapter,
    WorkerSuccess,
)
from sidecars.nautilus.engine import NautilusEngineBackend  # noqa: E402
from stonks_contracts.backtest import (  # noqa: E402
    BacktestCashBalance,
    BacktestEngineKind,
    BacktestJob,
    BacktestOrder,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestRuntimeIdentity,
    BacktestTimeInForce,
)
from tests.contracts.backtest.test_backtest_contract import (  # noqa: E402
    DAY_1_CLOSE,
    DAY_1_OPEN,
    DAY_2_CLOSE,
    INSTRUMENT_ID,
    ORDER_ID,
    REQUESTED,
    canonical_result,
    job,
    job_with_order,
)


def runtime() -> BacktestRuntimeIdentity:
    return BacktestRuntimeIdentity(
        engine=BacktestEngineKind.NAUTILUS,
        engine_version="1.230.0",
        adapter_version="0.1.0",
        runtime_hash="c" * 64,
        image_digest="sha256:" + "d" * 64,
        deterministic=True,
    )


def request() -> BacktestJob:
    return job(BacktestEngineKind.NAUTILUS).model_copy(update={"runtime": runtime()})


def run_actual(target: BacktestJob) -> WorkerSuccess:
    result = NautilusAdapter(
        policy=AdapterPolicy(runtime=target.runtime, max_orders=10, max_bars=100),
        backend=NautilusEngineBackend(),
        clock=lambda: REQUESTED,
    ).run(target)
    assert isinstance(result, WorkerSuccess)
    result.value.validate_against(target)
    return result


def test_real_engine_replays_to_canonical_semantics() -> None:
    target = request()
    first = run_actual(target)
    second = run_actual(target)

    assert first.value.semantic_hash == second.value.semantic_hash
    assert first.value.result_hash == second.value.result_hash
    assert first.value.semantic_hash == canonical_result(target).semantic_hash
    assert first.value.fills[0].external_ref == second.value.fills[0].external_ref
    assert first.value.fills[0].external_ref.startswith("nautilus:T-")
    assert ":raw-sha256:" in first.value.fills[0].external_ref


def test_real_engine_rejects_unpinned_job_version() -> None:
    target = request().model_copy(
        update={"runtime": runtime().model_copy(update={"engine_version": "1.229.0"})}
    )

    result = NautilusEngineBackend().run(target)

    assert isinstance(result, EngineFailure)
    assert result.code == "runtime_mismatch"


def test_backend_allows_identified_interface_compatible_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "version", lambda _: "1.230.0+modified.1")

    backend = NautilusEngineBackend()

    assert backend.engine_version == "1.230.0+modified.1"


def test_backend_rejects_incompatible_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "version", lambda _: "1.231.0")

    with pytest.raises(RuntimeError, match="unexpected NautilusTrader version"):
        NautilusEngineBackend()


def test_real_engine_does_not_backfill_intrabar_only_limit_touch() -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("99.50"),
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_CLOSE,
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:intrabar-touch",
    )
    target = job_with_order(selected, initial_quantity=Decimal("0")).model_copy(
        update={"runtime": runtime()}
    )

    result = run_actual(target).value

    assert result.fills == ()
    assert result.order_outcomes[0].status is BacktestOrderStatus.CANCELLED


def test_real_engine_ioc_does_not_search_past_first_attemptable_bar() -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("95"),
        time_in_force=BacktestTimeInForce.IOC,
        issued_at=DAY_1_OPEN - timedelta(minutes=1),
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:ioc-first-bar-only",
    )
    target = job_with_order(selected, initial_quantity=Decimal("0")).model_copy(
        update={"runtime": runtime()}
    )
    second = target.dataset.bars[1].model_copy(
        update={
            "open": Decimal("95"),
            "high": Decimal("96"),
            "low": Decimal("94"),
            "close": Decimal("95"),
        }
    )
    dataset = target.dataset.model_copy(
        update={"bars": (target.dataset.bars[0], second)}
    )
    payload = target.model_dump(mode="json")
    payload["dataset"] = dataset.model_dump(mode="json")
    payload["dataset_artifact_ref"] = f"sha256:{dataset.payload_hash()}"
    target = BacktestJob.model_validate(payload)

    result = run_actual(target).value

    assert result.fills == ()
    assert result.order_outcomes[0].status is BacktestOrderStatus.CANCELLED


def test_real_engine_ioc_halt_consumes_its_only_opportunity() -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("10"),
        limit_price=None,
        time_in_force=BacktestTimeInForce.IOC,
        issued_at=DAY_1_OPEN - timedelta(minutes=1),
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:ioc-halt",
    )
    target = job_with_order(selected, initial_quantity=Decimal("0")).model_copy(
        update={"runtime": runtime()}
    )
    halted = target.dataset.bars[0].model_copy(update={"tradable": False})
    dataset = target.dataset.model_copy(
        update={"bars": (halted, target.dataset.bars[1])}
    )
    payload = target.model_dump(mode="json")
    payload["dataset"] = dataset.model_dump(mode="json")
    payload["dataset_artifact_ref"] = f"sha256:{dataset.payload_hash()}"
    target = BacktestJob.model_validate(payload)

    result = run_actual(target).value

    assert result.fills == ()
    assert result.order_outcomes[0].status is BacktestOrderStatus.CANCELLED


def test_real_engine_day_order_does_not_cross_nontradable_session() -> None:
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
        strategy_event_ref="signal:aapl:day-halt",
    )
    target = job_with_order(selected, initial_quantity=Decimal("0")).model_copy(
        update={"runtime": runtime()}
    )
    halted = target.dataset.bars[0].model_copy(update={"tradable": False})
    dataset = target.dataset.model_copy(
        update={"bars": (halted, target.dataset.bars[1])}
    )
    payload = target.model_dump(mode="json")
    payload["dataset"] = dataset.model_dump(mode="json")
    payload["dataset_artifact_ref"] = f"sha256:{dataset.payload_hash()}"
    target = BacktestJob.model_validate(payload)

    result = run_actual(target).value

    assert result.fills == ()
    assert result.order_outcomes[0].status is BacktestOrderStatus.CANCELLED


@pytest.mark.parametrize(
    "value",
    [BacktestTimeInForce.DAY, BacktestTimeInForce.GTC, BacktestTimeInForce.IOC],
)
def test_native_child_time_in_force_is_mapped(value: BacktestTimeInForce) -> None:
    assert engine_module._time_in_force(value).name == value.name


@pytest.mark.parametrize(
    ("side", "order_type", "limit_price", "initial_quantity", "price"),
    [
        (
            BacktestOrderSide.SELL,
            BacktestOrderType.MARKET,
            None,
            Decimal("10"),
            Decimal("99.96"),
        ),
        (
            BacktestOrderSide.BUY,
            BacktestOrderType.LIMIT,
            Decimal("100.02"),
            Decimal("0"),
            Decimal("100.02"),
        ),
    ],
)
def test_real_engine_maps_sell_and_limit_to_canonical_costs(
    side: BacktestOrderSide,
    order_type: BacktestOrderType,
    limit_price: Decimal | None,
    initial_quantity: Decimal,
    price: Decimal,
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
        strategy_event_ref="signal:aapl:actual-engine",
    )
    target = job_with_order(selected, initial_quantity=initial_quantity).model_copy(
        update={"runtime": runtime()}
    )

    result = run_actual(target).value

    assert result.fills[0].price == price
    assert result.order_outcomes[0].status is BacktestOrderStatus.FILLED


@pytest.mark.parametrize(
    ("time_in_force", "fill_count", "filled_quantity", "status"),
    [
        (
            BacktestTimeInForce.GTC,
            2,
            Decimal("150"),
            BacktestOrderStatus.FILLED,
        ),
        (
            BacktestTimeInForce.IOC,
            1,
            Decimal("100"),
            BacktestOrderStatus.PARTIALLY_FILLED,
        ),
        (
            BacktestTimeInForce.DAY,
            1,
            Decimal("100"),
            BacktestOrderStatus.PARTIALLY_FILLED,
        ),
    ],
)
def test_real_engine_maps_participation_children(
    time_in_force: BacktestTimeInForce,
    fill_count: int,
    filled_quantity: Decimal,
    status: BacktestOrderStatus,
) -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("150"),
        limit_price=None,
        time_in_force=time_in_force,
        issued_at=DAY_1_OPEN - timedelta(minutes=1),
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:participation",
    )
    base = job_with_order(selected, initial_quantity=Decimal("0"))
    payload = base.model_dump(mode="json")
    payload["runtime"] = runtime().model_dump(mode="json")
    payload["initial_cash"] = [
        BacktestCashBalance(
            currency="USD", amount=Decimal("100000"), quantum=Decimal("0.01")
        ).model_dump(mode="json")
    ]
    target = BacktestJob.model_validate(payload)

    result = run_actual(target).value

    assert len(result.fills) == fill_count
    assert result.order_outcomes[0].status is status
    assert result.order_outcomes[0].filled_quantity == filled_quantity


def test_schedule_child_cap_fails_before_engine_start() -> None:
    selected = BacktestOrder.create(
        order_id=ORDER_ID,
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=Decimal("150"),
        limit_price=None,
        time_in_force=BacktestTimeInForce.GTC,
        issued_at=DAY_1_OPEN - timedelta(minutes=1),
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:schedule-cap",
    )
    base = job_with_order(selected, initial_quantity=Decimal("0"))
    payload = base.model_dump(mode="json")
    payload["runtime"] = runtime().model_dump(mode="json")
    payload["initial_cash"] = [
        BacktestCashBalance(
            currency="USD", amount=Decimal("100000"), quantum=Decimal("0.01")
        ).model_dump(mode="json")
    ]
    target = BacktestJob.model_validate(payload)

    result = NautilusEngineBackend(max_schedule_children=1).run(target)

    assert isinstance(result, EngineFailure)
    assert result.code == "job_too_large"


@pytest.mark.parametrize(
    ("quantity", "opening_cash", "fill_count", "statuses"),
    [
        (
            Decimal("10"),
            Decimal("10000"),
            2,
            (BacktestOrderStatus.FILLED, BacktestOrderStatus.FILLED),
        ),
        (
            Decimal("100"),
            Decimal("30000"),
            1,
            (BacktestOrderStatus.FILLED, BacktestOrderStatus.CANCELLED),
        ),
    ],
)
def test_real_engine_orders_share_same_bar_participation(
    quantity: Decimal,
    opening_cash: Decimal,
    fill_count: int,
    statuses: tuple[BacktestOrderStatus, BacktestOrderStatus],
) -> None:
    target = request()
    first = BacktestOrder.create(
        order_id=UUID(int=101),
        sequence=1,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=quantity,
        limit_price=None,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_CLOSE,
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:same-open-first",
    )
    second = BacktestOrder.create(
        order_id=UUID(int=102),
        sequence=2,
        instrument_id=INSTRUMENT_ID,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity=quantity,
        limit_price=None,
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=DAY_1_CLOSE,
        valid_until=DAY_2_CLOSE,
        strategy_event_ref="signal:aapl:same-open-second",
    )
    payload = target.model_dump(mode="json")
    payload["orders"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]
    payload["initial_cash"] = [
        BacktestCashBalance(
            currency="USD", amount=opening_cash, quantum=Decimal("0.01")
        ).model_dump(mode="json")
    ]
    target = BacktestJob.model_validate(payload)

    result = run_actual(target).value

    assert len(result.fills) == fill_count
    assert tuple(outcome.status for outcome in result.order_outcomes) == statuses
