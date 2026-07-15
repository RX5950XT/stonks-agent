from __future__ import annotations

from datetime import UTC, datetime

from scripts.smoke_engine_parity import _cases, _endpoint, _engine_job, _job
from stonks_contracts.backtest import BacktestEngineKind, BacktestTimeInForce

HASH_A = "a" * 64
HASH_B = "b" * 64


def test_real_sidecar_smoke_matrix_has_stable_cross_engine_inputs() -> None:
    base = _job(
        HASH_A,
        f"sha256:{HASH_A}",
        datetime(2026, 7, 15, 12, tzinfo=UTC),
    )
    cases = _cases(base)
    nautilus = _endpoint(
        BacktestEngineKind.NAUTILUS,
        "http://nautilus:7400",
        "test-token",
        HASH_A,
        f"sha256:{HASH_A}",
    )
    lean = _endpoint(
        BacktestEngineKind.LEAN,
        "http://lean:7410",
        "test-token",
        HASH_B,
        f"sha256:{HASH_B}",
    )

    assert tuple(item.name for item in cases) == (
        "market-day-buy",
        "market-gtc-partial",
        "market-ioc-partial",
        "sell-limit-gtc",
        "buy-limit-day-unfilled",
        "halted-ioc",
        "shared-volume-gtc",
    )
    assert {order.time_in_force for case in cases for order in case.job.orders} == {
        BacktestTimeInForce.DAY,
        BacktestTimeInForce.GTC,
        BacktestTimeInForce.IOC,
    }
    for case in cases:
        jobs = (_engine_job(case, nautilus), _engine_job(case, lean))
        assert jobs[0].input_hash == jobs[1].input_hash
        assert jobs[0].job_id != jobs[1].job_id
        assert jobs[0].attempt_nonce != jobs[1].attempt_nonce
