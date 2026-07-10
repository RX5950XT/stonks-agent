from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from stonks_contracts.common import ContractModel, Money
from stonks_contracts.execution import (
    ExecutionCommand,
    OrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
)
from stonks_contracts.portfolio import PortfolioTarget, TargetAllocation
from stonks_contracts.report import AnalysisReport
from stonks_contracts.risk import AccountReservation, ReservationKind, RiskDecision
from stonks_contracts.signal import AlphaSignal, PromotionState, SignalDirection

NOW = datetime(2026, 7, 10, 8, 30, tzinfo=UTC)
IDS = [UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(1, 20)]


@pytest.fixture
def canonical_models() -> tuple[ContractModel, ...]:
    signal = AlphaSignal(
        signal_id=IDS[0],
        strategy_id="fixture-momentum",
        strategy_version="1.0.0",
        instrument_id=IDS[1],
        as_of=NOW,
        horizon="5d",
        value=Decimal("0.4"),
        confidence=Decimal("0.7"),
        expires_at=NOW + timedelta(days=1),
        direction=SignalDirection.LONG,
        evidence_refs=(IDS[2],),
        reason_codes=("positive_revision",),
        promotion_state=PromotionState.PAPER_ELIGIBLE,
    )
    target = PortfolioTarget(
        target_id=IDS[3],
        account_id="paper-main",
        portfolio_snapshot_id=IDS[4],
        as_of=NOW,
        allocations=(
            TargetAllocation(
                instrument_id=IDS[1],
                target_weight=Decimal("0.2"),
                current_quantity=Decimal("0"),
                target_quantity=Decimal("2"),
                delta_quantity=Decimal("2"),
            ),
        ),
        input_signal_ids=(signal.signal_id,),
        policy_version="equal-weight/1.0.0",
        expected_turnover=Decimal("0.2"),
        expected_cost=Money(currency="USD", amount=Decimal("0.25")),
        calculation_hash="a" * 64,
    )
    risk = RiskDecision(
        decision_id=IDS[5],
        portfolio_target_id=target.target_id,
        account_id=target.account_id,
        approved=True,
        normalized_target=target,
        reasons=("within_limits",),
        limits_snapshot_hash="b" * 64,
        policy_version="hard-risk/1.0.0",
        policy_hash="c" * 64,
        decided_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    reservation = AccountReservation(
        reservation_id=IDS[6],
        account_id="paper-main",
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=Decimal("202.25"),
        risk_decision_id=risk.decision_id,
        portfolio_sequence=7,
        order_intent_id=IDS[7],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    intent = OrderIntent(
        intent_id=IDS[7],
        run_id=IDS[8],
        account_id="paper-main",
        instrument_id=IDS[1],
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),
        time_in_force=TimeInForce.DAY,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=8),
        risk_decision_id=risk.decision_id,
        reservation_id=reservation.reservation_id,
        portfolio_snapshot_id=target.portfolio_snapshot_id,
        aggregate_sequence=7,
        idempotency_key="run:fixture:buy",
        execution_model_version="next-bar/1.0.0",
        created_at=NOW,
    )
    command = ExecutionCommand(
        command_id=IDS[9],
        intent=intent,
        attempt_generation=1,
        attempt_nonce="fixture-nonce",
        issued_at=NOW,
    )
    report = AnalysisReport(
        report_id=IDS[10],
        subject="AAPL",
        as_of=NOW,
        language="zh-TW",
        report_type="cycle",
        conclusion="通過 paper risk gate。",
        score=Decimal("0.4"),
        confidence=Decimal("0.7"),
        risks=("模型可能失準",),
        catalysts=("財測上修",),
        evidence_refs=(IDS[2],),
        signal_ids=(signal.signal_id,),
        action_guardrails=("僅限 paper trading",),
        generator_version="deterministic/1.0.0",
        policy_version="report/1.0.0",
    )
    return signal, target, risk, reservation, intent, command, report


def test_pydantic_json_round_trip_preserves_models(
    canonical_models: tuple[ContractModel, ...],
) -> None:
    for model in canonical_models:
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model
        assert restored.payload_hash() == model.payload_hash()
