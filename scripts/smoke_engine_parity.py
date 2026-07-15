#!/usr/bin/env python3
"""Replay canonical fixtures through pinned Nautilus and LEAN sidecars."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

from scripts.smoke_nautilus import INSTRUMENT_ID, _job
from stonks_contracts.backtest import (
    BacktestCashBalance,
    BacktestDataset,
    BacktestEngineKind,
    BacktestJob,
    BacktestOrder,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestPosition,
    BacktestResult,
    BacktestRuntimeIdentity,
    BacktestTimeInForce,
)

_NAUTILUS_VERSION = "1.230.0"
_LEAN_VERSION = "17917+c22774e49ee80ecef5ca84f57616f6b66fad8bc5"


@dataclass(frozen=True, slots=True)
class Endpoint:
    engine: BacktestEngineKind
    base_url: str
    token: str
    runtime: BacktestRuntimeIdentity


@dataclass(frozen=True, slots=True)
class ExpectedOrder:
    status: BacktestOrderStatus
    fill_quantities: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class ParityCase:
    name: str
    job: BacktestJob
    expected: tuple[ExpectedOrder, ...]


def _order(
    name: str,
    sequence: int,
    base: BacktestJob,
    *,
    side: BacktestOrderSide,
    order_type: BacktestOrderType,
    quantity: str,
    time_in_force: BacktestTimeInForce,
    issued_at: datetime,
    limit_price: str | None = None,
) -> BacktestOrder:
    return BacktestOrder.create(
        order_id=uuid5(NAMESPACE_URL, f"stonks-parity:{name}:{sequence}"),
        sequence=sequence,
        instrument_id=INSTRUMENT_ID,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit_price) if limit_price is not None else None,
        time_in_force=time_in_force,
        issued_at=issued_at,
        valid_until=base.dataset.calendar.sessions[-1].closes_at,
        strategy_event_ref=f"parity:{name}:{sequence}",
    )


def _case_job(
    base: BacktestJob,
    name: str,
    orders: tuple[BacktestOrder, ...],
    *,
    initial_cash: str = "100000",
    initial_quantity: str = "0",
    dataset: BacktestDataset | None = None,
) -> BacktestJob:
    selected_dataset = dataset or base.dataset
    payload = base.model_dump(mode="json")
    payload.update(
        job_id=str(uuid5(NAMESPACE_URL, f"stonks-parity:{name}:nautilus")),
        attempt_nonce=f"parity-{name}-nautilus",
        dataset=selected_dataset.model_dump(mode="json"),
        dataset_artifact_ref=f"sha256:{selected_dataset.payload_hash()}",
        orders=[item.model_dump(mode="json") for item in orders],
        initial_cash=[
            BacktestCashBalance(
                currency="USD", amount=Decimal(initial_cash), quantum=Decimal("0.01")
            ).model_dump(mode="json")
        ],
        initial_positions=[
            BacktestPosition(
                instrument_id=INSTRUMENT_ID,
                quantity=Decimal(initial_quantity),
                quantity_quantum=Decimal("1"),
            ).model_dump(mode="json")
        ],
    )
    return BacktestJob.model_validate(payload)


def _cases(base: BacktestJob) -> tuple[ParityCase, ...]:
    first_open = base.dataset.bars[0].opens_at
    first_close = base.dataset.bars[0].closes_at
    pre_open = first_open - timedelta(minutes=1)
    market_day = _order(
        "market-day-buy",
        1,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity="10",
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=first_close,
    )
    gtc_partial = _order(
        "market-gtc-partial",
        1,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity="150",
        time_in_force=BacktestTimeInForce.GTC,
        issued_at=pre_open,
    )
    ioc_partial = _order(
        "market-ioc-partial",
        1,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity="150",
        time_in_force=BacktestTimeInForce.IOC,
        issued_at=pre_open,
    )
    sell_limit = _order(
        "sell-limit-gtc",
        1,
        base,
        side=BacktestOrderSide.SELL,
        order_type=BacktestOrderType.LIMIT,
        quantity="150",
        time_in_force=BacktestTimeInForce.GTC,
        issued_at=pre_open,
        limit_price="99",
    )
    missed_limit = _order(
        "buy-limit-day-unfilled",
        1,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.LIMIT,
        quantity="10",
        time_in_force=BacktestTimeInForce.DAY,
        issued_at=pre_open,
        limit_price="98",
    )
    halted_ioc = _order(
        "halted-ioc",
        1,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity="10",
        time_in_force=BacktestTimeInForce.IOC,
        issued_at=pre_open,
    )
    halted_bar = base.dataset.bars[0].model_copy(update={"tradable": False})
    halted_dataset = base.dataset.model_copy(
        update={"bars": (halted_bar, base.dataset.bars[1])}
    )
    shared_first = _order(
        "shared-volume-gtc",
        1,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity="75",
        time_in_force=BacktestTimeInForce.GTC,
        issued_at=pre_open,
    )
    shared_second = _order(
        "shared-volume-gtc",
        2,
        base,
        side=BacktestOrderSide.BUY,
        order_type=BacktestOrderType.MARKET,
        quantity="75",
        time_in_force=BacktestTimeInForce.GTC,
        issued_at=pre_open,
    )
    return (
        ParityCase(
            "market-day-buy",
            _case_job(base, "market-day-buy", (market_day,)),
            (ExpectedOrder(BacktestOrderStatus.FILLED, (Decimal("10"),)),),
        ),
        ParityCase(
            "market-gtc-partial",
            _case_job(base, "market-gtc-partial", (gtc_partial,)),
            (
                ExpectedOrder(
                    BacktestOrderStatus.FILLED,
                    (Decimal("100"), Decimal("50")),
                ),
            ),
        ),
        ParityCase(
            "market-ioc-partial",
            _case_job(base, "market-ioc-partial", (ioc_partial,)),
            (
                ExpectedOrder(
                    BacktestOrderStatus.PARTIALLY_FILLED,
                    (Decimal("100"),),
                ),
            ),
        ),
        ParityCase(
            "sell-limit-gtc",
            _case_job(
                base,
                "sell-limit-gtc",
                (sell_limit,),
                initial_quantity="150",
            ),
            (
                ExpectedOrder(
                    BacktestOrderStatus.FILLED,
                    (Decimal("100"), Decimal("50")),
                ),
            ),
        ),
        ParityCase(
            "buy-limit-day-unfilled",
            _case_job(base, "buy-limit-day-unfilled", (missed_limit,)),
            (ExpectedOrder(BacktestOrderStatus.CANCELLED, ()),),
        ),
        ParityCase(
            "halted-ioc",
            _case_job(base, "halted-ioc", (halted_ioc,), dataset=halted_dataset),
            (ExpectedOrder(BacktestOrderStatus.CANCELLED, ()),),
        ),
        ParityCase(
            "shared-volume-gtc",
            _case_job(base, "shared-volume-gtc", (shared_first, shared_second)),
            (
                ExpectedOrder(BacktestOrderStatus.FILLED, (Decimal("75"),)),
                ExpectedOrder(
                    BacktestOrderStatus.FILLED,
                    (Decimal("25"), Decimal("50")),
                ),
            ),
        ),
    )


def _engine_job(case: ParityCase, endpoint: Endpoint) -> BacktestJob:
    return case.job.model_copy(
        update={
            "job_id": uuid5(
                NAMESPACE_URL, f"stonks-parity:{case.name}:{endpoint.engine.value}"
            ),
            "attempt_nonce": f"parity-{case.name}-{endpoint.engine.value}",
            "runtime": endpoint.runtime,
        }
    )


def _send(endpoint: Endpoint, job: BacktestJob, timeout: float) -> BacktestResult:
    request = Request(
        f"{endpoint.base_url.rstrip('/')}/v1/backtests",
        data=job.model_dump_json().encode("utf-8"),
        headers={
            "Authorization": f"Bearer {endpoint.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except HTTPError as request_error:
        try:
            failure = json.loads(request_error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            failure = {}
        error_payload = failure.get("error") if isinstance(failure, dict) else None
        code = (
            error_payload.get("code", "unknown")
            if isinstance(error_payload, dict)
            else "unknown"
        )
        raise RuntimeError(
            f"{endpoint.engine.value} sidecar returned {code}"
        ) from request_error
    except (URLError, TimeoutError) as request_error:
        raise RuntimeError(
            f"{endpoint.engine.value} sidecar request failed"
        ) from request_error
    if payload.get("success") is not True:
        error_payload = payload.get("error") or {}
        code = (
            error_payload.get("code", "unknown")
            if isinstance(error_payload, dict)
            else "unknown"
        )
        raise RuntimeError(f"{endpoint.engine.value} sidecar returned {code}")
    result = BacktestResult.model_validate(payload["data"])
    result.validate_against(job)
    return result


def _assert_expected(case: ParityCase, result: BacktestResult) -> None:
    outcomes = {item.order_id: item for item in result.order_outcomes}
    fills: dict[UUID, list[Decimal]] = {item.order_id: [] for item in case.job.orders}
    for fill in result.fills:
        fills[fill.order_id].append(fill.quantity)
    for order, expected in zip(case.job.orders, case.expected, strict=True):
        if outcomes[order.order_id].status is not expected.status:
            raise RuntimeError(f"{case.name} outcome status changed")
        if tuple(fills[order.order_id]) != expected.fill_quantities:
            raise RuntimeError(f"{case.name} fill schedule changed")


def _external_ref_hash(result: BacktestResult) -> str:
    payload = json.dumps(
        [item.external_ref for item in result.fills],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _evaluate(
    case: ParityCase,
    endpoints: tuple[Endpoint, Endpoint],
    timeout: float,
) -> dict[str, object]:
    jobs = tuple(_engine_job(case, endpoint) for endpoint in endpoints)
    if len({item.input_hash for item in jobs}) != 1:
        raise RuntimeError(f"{case.name} engine input hashes differ")
    results: list[BacktestResult] = []
    evidence: dict[str, object] = {}
    for endpoint, job in zip(endpoints, jobs, strict=True):
        try:
            first = _send(endpoint, job, timeout)
            replay = _send(endpoint, job, timeout)
        except RuntimeError as error:
            raise RuntimeError(f"{case.name}: {error}") from error
        _assert_expected(case, first)
        if first.semantic_hash != replay.semantic_hash:
            raise RuntimeError(f"{case.name} semantic replay changed")
        if _external_ref_hash(first) != _external_ref_hash(replay):
            raise RuntimeError(f"{case.name} fill provenance replay changed")
        results.append(first)
        evidence[endpoint.engine.value] = {
            "runtime_hash": first.runtime.runtime_hash,
            "image_digest": first.runtime.image_digest,
            "result_hash": first.result_hash,
            "semantic_hash": first.semantic_hash,
            "fill_count": len(first.fills),
            "fill_provenance_hash": _external_ref_hash(first),
        }
    if results[0].semantic_hash != results[1].semantic_hash:
        raise RuntimeError(f"{case.name} canonical economics differ")
    return {
        "case": case.name,
        "input_hash": jobs[0].input_hash,
        "semantic_hash": results[0].semantic_hash,
        "engines": evidence,
    }


def _endpoint(
    engine: BacktestEngineKind,
    base_url: str,
    token: str,
    runtime_hash: str,
    image_digest: str,
) -> Endpoint:
    version = (
        _NAUTILUS_VERSION if engine is BacktestEngineKind.NAUTILUS else _LEAN_VERSION
    )
    return Endpoint(
        engine=engine,
        base_url=base_url,
        token=token,
        runtime=BacktestRuntimeIdentity(
            engine=engine,
            engine_version=version,
            adapter_version="0.1.0",
            runtime_hash=runtime_hash,
            image_digest=image_digest,
            deterministic=True,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nautilus-url", default="http://127.0.0.1:7400")
    parser.add_argument("--nautilus-runtime-hash", required=True)
    parser.add_argument("--nautilus-image-digest", required=True)
    parser.add_argument("--nautilus-token", required=True)
    parser.add_argument("--lean-url", default="http://127.0.0.1:7410")
    parser.add_argument("--lean-runtime-hash", required=True)
    parser.add_argument("--lean-image-digest", required=True)
    parser.add_argument("--lean-token", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    endpoints = (
        _endpoint(
            BacktestEngineKind.NAUTILUS,
            args.nautilus_url,
            args.nautilus_token,
            args.nautilus_runtime_hash,
            args.nautilus_image_digest,
        ),
        _endpoint(
            BacktestEngineKind.LEAN,
            args.lean_url,
            args.lean_token,
            args.lean_runtime_hash,
            args.lean_image_digest,
        ),
    )
    now = datetime.now(UTC)
    base = _job(
        args.nautilus_runtime_hash,
        args.nautilus_image_digest,
        now,
    )
    reports = [_evaluate(case, endpoints, args.timeout) for case in _cases(base)]
    print(
        json.dumps(
            {
                "claim_scope": "fixture_canonical_semantics_only",
                "case_count": len(reports),
                "reports": reports,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
