from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.application.risk.evaluate import (
    HardRiskEvaluator,
    evaluate,
    load_risk_policy,
)
from stonks_agent.domain.errors import Success
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PortfolioTarget,
    PositionBalance,
    TargetAllocation,
)
from stonks_agent.domain.risk import RiskCheck, RiskDecision
from stonks_agent.domain.risk_evaluation import HardRiskPolicy
from stonks_agent.domain.strategy import PromotionState
from stonks_agent.ports.ledger import LedgerHead
from stonks_agent.ports.risk_policy import RiskPolicyPort
from stonks_contracts.instrument import AssetClass

from ..portfolio.helpers import (
    HASH_B,
    INSTRUMENT_A,
    NOW,
    candidates,
)
from .helpers import (
    RISK_AT,
    configured_policy,
    instrument_state,
    risk_command,
    target,
)

EXPECTED_CHECKS = (
    "adv_participation",
    "asset_class",
    "cash_available",
    "daily_loss",
    "data_freshness",
    "drawdown",
    "gross_exposure",
    "kill_switch",
    "ledger_binding",
    "market_session",
    "net_exposure",
    "pending_orders",
    "position_available",
    "reservation_reconciliation",
    "sector_exposure",
    "signal_freshness",
    "single_position",
    "target_binding",
    "turnover",
)


def checks(result: Success[RiskDecision]) -> dict[str, RiskCheck]:
    return {item.code: item for item in result.value.checks}


def test_default_policy_approves_and_hashes_exact_target_deterministically() -> None:
    first = evaluate(risk_command(), configured_policy())
    second = evaluate(risk_command(), configured_policy())

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first == second
    decision = first.value
    assert decision.approved is True
    assert decision.normalized_target == target()
    assert decision.expires_at == RISK_AT + timedelta(seconds=60)
    assert tuple(item.code for item in decision.checks) == EXPECTED_CHECKS
    assert all(item.passed for item in decision.checks)


@pytest.mark.parametrize(
    ("policy_changes", "command_changes", "failed_code"),
    [
        ({"max_single_position_weight": Decimal("0.10")}, {}, "single_position"),
        ({"max_sector_weight": Decimal("0.10")}, {}, "sector_exposure"),
        ({"max_asset_class_weight": Decimal("0.10")}, {}, "asset_class"),
        ({"max_gross_exposure": Decimal("0.10")}, {}, "gross_exposure"),
        ({"max_net_exposure": Decimal("0.10")}, {}, "net_exposure"),
        ({"max_turnover": Decimal("0.05")}, {}, "turnover"),
        ({"max_adv_participation": Decimal("0.0005")}, {}, "adv_participation"),
        ({"max_pending_orders": 0}, {}, "pending_orders"),
        (
            {"max_drawdown": Decimal("0.05")},
            {"high_watermark_nav": Decimal("12000.00")},
            "drawdown",
        ),
        (
            {"max_daily_loss": Decimal("0.05")},
            {"day_start_nav": Decimal("12000.00")},
            "daily_loss",
        ),
    ],
)
def test_each_numeric_limit_rejects_with_auditable_check(
    policy_changes: dict[str, object],
    command_changes: dict[str, object],
    failed_code: str,
) -> None:
    policy = configured_policy().model_copy(update=policy_changes)

    result = evaluate(risk_command(**command_changes), policy)

    assert isinstance(result, Success)
    assert result.value.approved is False
    assert result.value.normalized_target is None
    failed = checks(result)[failed_code]
    assert failed.passed is False
    assert failed.reason


@pytest.mark.parametrize(
    ("command_changes", "failed_code"),
    [
        (
            {
                "kill_switch": {
                    "global_active": True,
                    "account_active": False,
                    "observed_at": RISK_AT,
                }
            },
            "kill_switch",
        ),
        (
            {
                "kill_switch": {
                    "global_active": False,
                    "account_active": False,
                    "observed_at": RISK_AT - timedelta(minutes=1),
                }
            },
            "kill_switch",
        ),
        (
            {
                "ledger_head": LedgerHead(
                    account_id="portfolio-paper",
                    sequence=11,
                    transaction_hash=HASH_B,
                )
            },
            "ledger_binding",
        ),
        (
            {
                "instruments": (
                    instrument_state(mark_as_of=RISK_AT - timedelta(seconds=301)),
                )
            },
            "data_freshness",
        ),
        (
            {
                "instruments": (
                    instrument_state(
                        session={
                            "mic": "XNAS",
                            "session_date": NOW.date(),
                            "opens_at": NOW - timedelta(hours=2),
                            "closes_at": NOW - timedelta(minutes=1),
                        }
                    ),
                )
            },
            "market_session",
        ),
    ],
)
def test_stale_conflicting_or_killed_state_rejects(
    command_changes: dict[str, object],
    failed_code: str,
) -> None:
    result = evaluate(risk_command(**command_changes), configured_policy())

    assert isinstance(result, Success)
    assert result.value.approved is False
    assert checks(result)[failed_code].passed is False


def test_signal_is_rechecked_at_risk_time_and_missing_or_suspended_rejects() -> None:
    values = candidates()
    suspended = values[0].model_copy(
        update={
            "registry": values[0].registry.model_copy(
                update={"state": PromotionState.SUSPENDED}
            )
        }
    )

    missing = evaluate(
        risk_command(signal_candidates=(values[0],)), configured_policy()
    )
    invalid = evaluate(
        risk_command(signal_candidates=(suspended, values[1])), configured_policy()
    )

    for result in (missing, invalid):
        assert isinstance(result, Success)
        assert result.value.approved is False
        assert checks(result)["signal_freshness"].passed is False


def test_cash_position_and_reservation_projections_fail_closed() -> None:
    base = risk_command()
    low_cash = AccountPortfolioSnapshot.model_validate(
        base.snapshot.model_dump()
        | {
            "cash": (
                CashBalance(
                    currency="USD",
                    settled_amount=Decimal("500.00"),
                    reserved_amount=Decimal("0.00"),
                    quantum=Decimal("0.01"),
                ),
            )
        }
    )
    sell_allocation = TargetAllocation(
        instrument_id=INSTRUMENT_A,
        current_quantity=Decimal("10"),
        target_quantity=Decimal("0"),
        delta_quantity=Decimal("-10"),
        quantity_quantum=Decimal("1"),
        target_weight=Decimal("0"),
        constraint_diagnostics=(),
    )
    sell_target = PortfolioTarget.create(
        target_id=base.target.target_id,
        account_id=base.target.account_id,
        portfolio_snapshot_id=base.target.portfolio_snapshot_id,
        account_aggregate_sequence=base.target.account_aggregate_sequence,
        portfolio_sequence=base.target.portfolio_sequence,
        as_of=base.target.as_of,
        allocations=(sell_allocation,),
        input_signal_ids=base.target.input_signal_ids,
        policy_version=base.target.policy_version,
        policy_hash=base.target.policy_hash,
        expected_turnover=Decimal("0.090909090909"),
        expected_cost=Decimal("0.80"),
        cost_currency="USD",
    )
    pending_id = UUID("44000000-0000-4000-8000-000000000099")
    pending_snapshot = AccountPortfolioSnapshot.model_validate(
        base.snapshot.model_dump() | {"pending_order_ids": (pending_id,)}
    )
    constrained_position_snapshot = AccountPortfolioSnapshot.model_validate(
        base.snapshot.model_dump()
        | {
            "positions": (
                PositionBalance(
                    instrument_id=INSTRUMENT_A,
                    quantity=Decimal("10"),
                    sellable_quantity=Decimal("2"),
                    reserved_quantity=Decimal("0"),
                    quantum=Decimal("1"),
                ),
            )
        }
    )

    cash_result = evaluate(risk_command(snapshot=low_cash), configured_policy())
    position_result = evaluate(
        risk_command(target=sell_target, snapshot=constrained_position_snapshot),
        configured_policy(),
    )
    reservation_result = evaluate(
        risk_command(snapshot=pending_snapshot), configured_policy()
    )

    assert isinstance(cash_result, Success)
    assert checks(cash_result)["cash_available"].passed is False
    assert isinstance(position_result, Success)
    assert checks(position_result)["position_available"].passed is False
    assert isinstance(reservation_result, Success)
    assert checks(reservation_result)["reservation_reconciliation"].passed is False


def test_unknown_asset_missing_mark_and_target_sequence_drift_reject() -> None:
    unsupported = evaluate(
        risk_command(instruments=(instrument_state(asset_class=AssetClass.CRYPTO),)),
        configured_policy(),
    )
    missing = evaluate(risk_command(instruments=()), configured_policy())
    original = target()
    drifted = PortfolioTarget.create(
        target_id=original.target_id,
        account_id=original.account_id,
        portfolio_snapshot_id=original.portfolio_snapshot_id,
        account_aggregate_sequence=original.account_aggregate_sequence,
        portfolio_sequence=99,
        as_of=original.as_of,
        allocations=original.allocations,
        input_signal_ids=original.input_signal_ids,
        policy_version=original.policy_version,
        policy_hash=original.policy_hash,
        expected_turnover=original.expected_turnover,
        expected_cost=original.expected_cost,
        cost_currency=original.cost_currency,
    )
    sequence = evaluate(risk_command(target=drifted), configured_policy())

    assert isinstance(unsupported, Success)
    assert checks(unsupported)["asset_class"].passed is False
    assert isinstance(missing, Success)
    assert checks(missing)["data_freshness"].passed is False
    assert isinstance(sequence, Success)
    assert checks(sequence)["target_binding"].passed is False


def test_policy_contract_loader_and_typed_strategy_boundary(tmp_path: Path) -> None:
    builder = HardRiskEvaluator(configured_policy())

    assert isinstance(builder, RiskPolicyPort)
    assert isinstance(builder.evaluate(risk_command()), Success)
    with pytest.raises(ValidationError, match="sorted"):
        payload = configured_policy().model_dump()
        HardRiskPolicy.model_validate(
            payload
            | {
                "allowed_asset_classes": tuple(
                    reversed(payload["allowed_asset_classes"])
                )
            }
        )
    with pytest.raises(ValueError, match="could not be loaded"):
        load_risk_policy(tmp_path / "missing.yaml")
