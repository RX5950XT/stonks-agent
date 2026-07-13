from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from stonks_agent.application.portfolio.build_target import (
    DeterministicPortfolioBuilder,
    PortfolioPolicy,
    build_target,
    load_portfolio_policy,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.strategy import PromotionState
from stonks_agent.ports.portfolio_policy import PortfolioPolicyPort

from .helpers import (
    INSTRUMENT_A,
    INSTRUMENT_B,
    NOW,
    ROOT,
    SIGNAL_A,
    SIGNAL_C,
    candidate,
    candidates,
    command,
    configured_policy,
    mark,
    snapshot,
)

GOLDEN = ROOT / "tests" / "golden" / "portfolio" / "portfolio_v1.json"


def test_configured_policy_builds_golden_deterministic_target() -> None:
    result = build_target(command(), configured_policy())

    assert isinstance(result, Success)
    actual = result.value.model_dump(mode="json")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected
    allocation = result.value.allocations[0]
    assert allocation.target_quantity == Decimal("19")
    assert allocation.delta_quantity == Decimal("9")
    assert result.value.expected_turnover == Decimal("0.081818181818")
    assert result.value.expected_cost == Decimal("0.72")


def test_missing_signal_weight_is_not_renormalized() -> None:
    full = build_target(command(), configured_policy())
    missing = build_target(
        command(signal_candidates=(candidates()[0],)), configured_policy()
    )

    assert isinstance(full, Success)
    assert isinstance(missing, Success)
    assert full.value.allocations[0].target_quantity == Decimal("19")
    assert missing.value.allocations[0].target_quantity == Decimal("15")
    assert "ensemble_missing_weight:0.650000000000" in (
        missing.value.allocations[0].constraint_diagnostics
    )


@given(reverse_marks=st.booleans(), reverse_signals=st.booleans())
def test_input_permutations_have_stable_order_and_calculation_hash(
    reverse_marks: bool,
    reverse_signals: bool,
) -> None:
    original_marks = (
        mark(),
        mark(
            instrument_id=INSTRUMENT_B,
            price=Decimal("25"),
        ),
    )
    instrument_b_candidate = candidate(
        strategy_id="baseline-linear",
        signal_id=SIGNAL_C,
        value=Decimal("0.5"),
        confidence=Decimal("0.8"),
        instrument_id=INSTRUMENT_B,
        ordinal=3,
    )
    original_candidates = (*candidates(), instrument_b_candidate)
    first = build_target(
        command(
            marks=tuple(reversed(original_marks)) if reverse_marks else original_marks,
            signal_candidates=(
                tuple(reversed(original_candidates))
                if reverse_signals
                else original_candidates
            ),
        ),
        configured_policy(),
    )
    second = build_target(
        command(marks=original_marks, signal_candidates=original_candidates),
        configured_policy(),
    )

    assert isinstance(first, Success)
    assert isinstance(second, Success)
    assert first.value.calculation_hash == second.value.calculation_hash
    assert first.value == second.value
    assert tuple(item.instrument_id for item in first.value.allocations) == (
        INSTRUMENT_A,
        INSTRUMENT_B,
    )


@pytest.mark.parametrize(
    ("policy_changes", "expected_quantity", "diagnostic"),
    [
        ({"deadband": Decimal("0.30")}, Decimal("2"), "deadband:applied"),
        (
            {"max_position_weight": Decimal("0.10")},
            Decimal("11"),
            "position_bound:applied",
        ),
        ({"shrinkage": Decimal("0.50")}, Decimal("13"), "shrinkage:0.50"),
        ({"turnover_penalty": Decimal("1")}, Decimal("10"), "turnover_penalty:1"),
    ],
)
def test_policy_constraints_are_exact_and_diagnostic(
    policy_changes: dict[str, Decimal],
    expected_quantity: Decimal,
    diagnostic: str,
) -> None:
    policy = configured_policy().model_copy(update=policy_changes)

    result = build_target(command(), policy)

    assert isinstance(result, Success)
    allocation = result.value.allocations[0]
    assert allocation.target_quantity == expected_quantity
    assert diagnostic in allocation.constraint_diagnostics


def test_ineligible_or_future_signal_fails_closed() -> None:
    suspended = candidate(
        strategy_id="baseline-last-value",
        signal_id=SIGNAL_A,
        value=Decimal("0.8"),
        confidence=Decimal("0.75"),
        ordinal=1,
        state=PromotionState.SUSPENDED,
    )
    future = candidate(
        strategy_id="baseline-last-value",
        signal_id=SIGNAL_A,
        value=Decimal("0.8"),
        confidence=Decimal("0.75"),
        ordinal=1,
        generated_at=NOW + timedelta(seconds=1),
    )

    ineligible_result = build_target(
        command(signal_candidates=(suspended,)), configured_policy()
    )
    future_result = build_target(
        command(signal_candidates=(future,)), configured_policy()
    )

    assert isinstance(ineligible_result, Failure)
    assert ineligible_result.error.code is ErrorCode.CONFLICT
    assert ineligible_result.error.details["reason"] == "strategy_not_paper_eligible"
    assert isinstance(future_result, Failure)
    assert future_result.error.code is ErrorCode.INVALID_INPUT


def test_missing_mark_and_duplicate_strategy_instrument_fail_closed() -> None:
    missing_mark = build_target(command(marks=()), configured_policy())
    duplicate = build_target(
        command(signal_candidates=(candidates()[0], candidates()[0].model_copy())),
        configured_policy(),
    )

    assert isinstance(missing_mark, Failure)
    assert missing_mark.error.code is ErrorCode.DATA_UNAVAILABLE
    assert isinstance(duplicate, Failure)
    assert duplicate.error.code is ErrorCode.CONFLICT


def test_policy_contract_rejects_unsafe_or_ambiguous_configuration() -> None:
    payload = configured_policy().model_dump()

    with pytest.raises(ValidationError, match="sorted"):
        PortfolioPolicy.model_validate(
            payload | {"strategy_weights": tuple(reversed(payload["strategy_weights"]))}
        )
    with pytest.raises(ValidationError, match="sum"):
        overweight = tuple(
            item.model_copy(update={"weight": Decimal("0.30")})
            for item in configured_policy().strategy_weights
        )
        PortfolioPolicy.model_validate(payload | {"strategy_weights": overweight})


def test_builder_satisfies_typed_policy_port() -> None:
    builder = DeterministicPortfolioBuilder(configured_policy())

    result = builder.build_target(command())

    assert isinstance(builder, PortfolioPolicyPort)
    assert isinstance(result, Success)


def test_loader_masks_io_and_yaml_failures(tmp_path: Path) -> None:
    malformed = tmp_path / "portfolio.yaml"
    malformed.write_text("weights: [", encoding="utf-8")

    with pytest.raises(ValueError, match="could not be loaded"):
        load_portfolio_policy(malformed)
    with pytest.raises(ValueError, match="could not be loaded"):
        load_portfolio_policy(tmp_path / "missing.yaml")


def test_duplicate_future_and_quantum_inconsistent_marks_fail_closed() -> None:
    duplicate = build_target(command(marks=(mark(), mark())), configured_policy())
    future = build_target(
        command(marks=(mark(as_of=NOW + timedelta(seconds=1)),)),
        configured_policy(),
    )
    mismatched_quantum = build_target(
        command(marks=(mark(quantity_quantum=Decimal("0.5")),)),
        configured_policy(),
    )

    assert isinstance(duplicate, Failure)
    assert duplicate.error.code is ErrorCode.CONFLICT
    assert isinstance(future, Failure)
    assert future.error.code is ErrorCode.INVALID_INPUT
    assert isinstance(mismatched_quantum, Failure)
    assert mismatched_quantum.error.code is ErrorCode.CONFLICT


def test_empty_portfolio_and_unconfigured_strategy_fail_closed() -> None:
    empty = build_target(
        command(
            account_snapshot=snapshot(cash_amount=Decimal("1.00"), positions=()),
            signal_candidates=(),
        ),
        configured_policy(),
    )
    unconfigured = candidate(
        strategy_id="not-configured",
        signal_id=SIGNAL_A,
        value=Decimal("0.1"),
        confidence=Decimal("0.5"),
        ordinal=9,
    )
    unconfigured_result = build_target(
        command(signal_candidates=(unconfigured,)), configured_policy()
    )

    assert isinstance(empty, Failure)
    assert empty.error.code is ErrorCode.DATA_UNAVAILABLE
    assert isinstance(unconfigured_result, Failure)
    assert unconfigured_result.error.code is ErrorCode.CONFIGURATION_INVALID


def test_future_registry_or_evaluation_binding_fails_closed() -> None:
    value = candidates()[0]
    future_registry = value.model_copy(
        update={
            "registry": value.registry.model_copy(
                update={"updated_at": NOW + timedelta(seconds=1)}
            )
        }
    )
    future_evaluation = value.model_copy(
        update={
            "evaluation": value.evaluation.model_copy(
                update={"created_at": NOW + timedelta(seconds=1)}
            )
        }
    )

    for bound in (future_registry, future_evaluation):
        result = build_target(command(signal_candidates=(bound,)), configured_policy())
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.INVALID_INPUT


def test_zero_nav_and_foreign_currency_fail_closed() -> None:
    zero_nav = build_target(
        command(account_snapshot=snapshot(cash_amount=Decimal("0.00"), positions=())),
        configured_policy(),
    )
    foreign = command().model_copy(update={"base_currency": "TWD"})

    foreign_result = build_target(foreign, configured_policy())

    assert isinstance(zero_nav, Failure)
    assert zero_nav.error.code is ErrorCode.DATA_UNAVAILABLE
    assert isinstance(foreign_result, Failure)
    assert foreign_result.error.code is ErrorCode.CONFLICT
