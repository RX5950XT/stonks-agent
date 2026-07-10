from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from stonks_agent.application.execution.submit import submit_paper_execution
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.execution import (
    ExecutionCommand,
    ExecutionReceipt,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.research import AgentOpinion
from stonks_contracts.signal import ForecastSignal

NOW = datetime(2026, 7, 10, 8, 30, tzinfo=UTC)
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000001")


class RecordingExecutionPort:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, command: ExecutionCommand) -> Result[ExecutionReceipt]:
        self.calls += 1
        receipt = ExecutionReceipt(
            receipt_id=UUID("00000000-0000-4000-8000-000000000030"),
            command_id=command.command_id,
            order_intent_id=command.intent.intent_id,
            status=OrderStatus.ACCEPTED,
            occurred_at=command.issued_at,
            sequence=1,
            filled_quantity=Decimal("0"),
            remaining_quantity=command.intent.quantity,
            command_quantity=command.intent.quantity,
        )
        return Success(receipt)


@pytest.mark.parametrize(
    "candidate",
    [
        AgentOpinion(
            opinion_id=UUID("00000000-0000-4000-8000-000000000031"),
            instrument_id=INSTRUMENT_ID,
            as_of=NOW,
            horizon="5d",
            recommendation="bullish",
            thesis="Evidence-backed test opinion.",
            confidence=Decimal("0.7"),
            calibration=ConfidenceCalibration.UNCALIBRATED,
            producer="test",
            model_version="test/1",
        ),
        ForecastSignal(
            forecast_id=UUID("00000000-0000-4000-8000-000000000032"),
            instrument_id=INSTRUMENT_ID,
            as_of=NOW,
            interval="1d",
            horizon_bars=5,
            expected_return=Decimal("0.02"),
            median_return=Decimal("0.01"),
            direction_probability=Decimal("0.6"),
            expected_volatility=Decimal("0.2"),
            downside_quantile=Decimal("-0.1"),
            max_drawdown_quantile=Decimal("-0.2"),
            path_count=10,
            dispersion=Decimal("0.1"),
            input_quality=DataQuality(
                status=DataQualityStatus.AVAILABLE,
                completeness=Decimal("1"),
            ),
            model_id="test-model",
            model_revision="1",
            tokenizer_id="test-tokenizer",
            tokenizer_revision="1",
            device="cpu",
            seed_policy="fixed",
            inference_code_version="1",
            dataset_snapshot_id=UUID("00000000-0000-4000-8000-000000000033"),
            input_window_start=NOW - timedelta(days=30),
            input_window_end=NOW,
            generated_at=NOW,
            latency_ms=1,
        ),
    ],
)
def test_research_outputs_cannot_reach_execution_port(candidate: object) -> None:
    port = RecordingExecutionPort()
    operator = LocalPrincipal(
        subject="paper-operator",
        roles=frozenset({Role.PAPER_OPERATOR}),
    )

    result = submit_paper_execution(
        principal=operator,
        candidate=candidate,
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert port.calls == 0


def test_unauthorized_principal_cannot_reach_execution_port() -> None:
    port = RecordingExecutionPort()
    researcher = LocalPrincipal(
        subject="researcher",
        roles=frozenset({Role.RESEARCHER}),
    )
    command = _command(_order_intent())

    result = submit_paper_execution(
        principal=researcher,
        candidate=command,
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert port.calls == 0


def test_authorized_execution_command_reaches_port_once() -> None:
    port = RecordingExecutionPort()
    operator = LocalPrincipal(
        subject="paper-operator",
        roles=frozenset({Role.PAPER_OPERATOR}),
    )
    command = _command(_order_intent())

    result = submit_paper_execution(
        principal=operator,
        candidate=command,
        port=port,
    )

    assert isinstance(result, Success)
    assert port.calls == 1


def _command(order_intent: OrderIntent) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=UUID("00000000-0000-4000-8000-000000000034"),
        intent=order_intent,
        attempt_generation=1,
        attempt_nonce="nonce-1",
        issued_at=NOW,
    )


def _order_intent() -> OrderIntent:
    return OrderIntent(
        intent_id=UUID("00000000-0000-4000-8000-000000000035"),
        run_id=UUID("00000000-0000-4000-8000-000000000036"),
        account_id="paper-main",
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        time_in_force=TimeInForce.DAY,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=8),
        risk_decision_id=UUID("00000000-0000-4000-8000-000000000037"),
        reservation_id=UUID("00000000-0000-4000-8000-000000000038"),
        portfolio_snapshot_id=UUID("00000000-0000-4000-8000-000000000039"),
        aggregate_sequence=1,
        idempotency_key="execution-authority",
        execution_model_version="test/1",
        created_at=NOW,
    )
