from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from stonks_agent.domain.operations import (
    KillSwitchScope,
    OperatorActionType,
    PaperKillSwitchState,
    PaperOperatorAction,
)

NOW = datetime(2026, 7, 13, 18, tzinfo=UTC)
HASH_A = "a" * 64


def state(**changes: object) -> PaperKillSwitchState:
    payload: dict[str, object] = {
        "switch_id": UUID("88000000-0000-4000-8000-000000000001"),
        "scope": KillSwitchScope.GLOBAL,
        "account_id": None,
        "active": True,
        "reason_code": "operator_requested",
        "actor": "operator:one",
        "version": 2,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return PaperKillSwitchState.model_validate(payload | changes)


def action(**changes: object) -> PaperOperatorAction:
    payload: dict[str, object] = {
        "action_id": UUID("88000000-0000-4000-8000-000000000002"),
        "sequence": 1,
        "action_type": OperatorActionType.ACTIVATED,
        "scope": KillSwitchScope.GLOBAL,
        "account_id": None,
        "actor": "operator:one",
        "reason_code": "operator_requested",
        "switch_version": 2,
        "cancelled_order_ids": (UUID("88000000-0000-4000-8000-000000000003"),),
        "reconciliation_hashes": (),
        "mismatch_reasons": (),
        "occurred_at": NOW,
        "previous_action_hash": None,
    }
    return PaperOperatorAction.create(**(payload | changes))  # type: ignore[arg-type]


def test_operator_action_is_hash_chained_and_stably_ordered() -> None:
    value = action()

    assert value.action_hash == value.expected_action_hash()
    with pytest.raises(ValidationError, match="hash"):
        PaperOperatorAction.model_validate(
            value.model_dump(mode="python") | {"action_hash": HASH_A}
        )


def test_scope_shape_and_action_chain_fail_closed() -> None:
    with pytest.raises(ValidationError, match="scope"):
        state(scope=KillSwitchScope.ACCOUNT)
    with pytest.raises(ValidationError, match="previous"):
        action(sequence=2)
    with pytest.raises(ValidationError, match="sorted"):
        action(
            cancelled_order_ids=(
                UUID("88000000-0000-4000-8000-000000000005"),
                UUID("88000000-0000-4000-8000-000000000004"),
            )
        )
