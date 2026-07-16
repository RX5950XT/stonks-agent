from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from application.execution.helpers import ACCOUNT_ID, INSTRUMENT_ID, NOW, command
from stonks_agent.application.execution.submit import submit_paper_execution
from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    PrincipalKind,
    ResourceKind,
    Role,
    ServiceIdentity,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.fills import ExecutionReceipt
from stonks_agent.domain.orders import (
    ExecutionCommand,
    OrderStatus,
    append_order_event,
)
from stonks_contracts.execution import (
    ExecutionCommand as WireExecutionCommand,
)
from stonks_contracts.execution import (
    OrderIntent as WireOrderIntent,
)
from stonks_contracts.execution import (
    OrderSide as WireOrderSide,
)
from stonks_contracts.execution import (
    OrderType as WireOrderType,
)
from stonks_contracts.execution import (
    TimeInForce as WireTimeInForce,
)


class RecordingCanonicalExecutionPort:
    def __init__(self) -> None:
        self.commands: list[ExecutionCommand] = []

    def submit(self, value: ExecutionCommand) -> Result[ExecutionReceipt]:
        self.commands.append(value)
        accepted = append_order_event(
            value.intent,
            previous=None,
            target_status=OrderStatus.ACCEPTED,
            cumulative_filled_quantity=Decimal("0"),
            occurred_at=value.issued_at,
        )
        assert isinstance(accepted, Success)
        return Success(
            ExecutionReceipt.create(
                receipt_id=UUID("46000000-0000-4000-8000-000000000001"),
                command_id=value.command_id,
                intent=value.intent,
                event=accepted.value,
                fills=(),
                occurred_at=value.issued_at,
            )
        )


def _account_targets(account_id: str) -> frozenset[AccessTarget]:
    return frozenset(
        {
            AccessTarget(
                kind=ResourceKind.ACCOUNT,
                identifier=account_id,
            )
        }
    )


def test_human_paper_operator_cannot_reach_execution_port() -> None:
    port = RecordingCanonicalExecutionPort()
    operator = LocalPrincipal(
        subject="paper-operator",
        roles=frozenset({Role.PAPER_OPERATOR}),
    )

    result = submit_paper_execution(
        principal=operator,
        candidate=command(),
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert port.commands == []


@pytest.mark.parametrize(
    "identity",
    [ServiceIdentity.CORE_RUNNER, ServiceIdentity.RESEARCH_WORKER],
)
def test_non_executor_services_cannot_reach_execution_port(
    identity: ServiceIdentity,
) -> None:
    port = RecordingCanonicalExecutionPort()

    result = submit_paper_execution(
        principal=_service(identity, _account_targets(ACCOUNT_ID)),
        candidate=command(),
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert port.commands == []


@pytest.mark.parametrize(
    "targets",
    [
        frozenset(),
        _account_targets("another-account"),
        frozenset(
            {
                AccessTarget(
                    kind=ResourceKind.JOB,
                    identifier=ACCOUNT_ID,
                )
            }
        ),
    ],
)
def test_paper_executor_requires_exact_account_target(
    targets: frozenset[AccessTarget],
) -> None:
    port = RecordingCanonicalExecutionPort()

    result = submit_paper_execution(
        principal=_service(ServiceIdentity.PAPER_EXECUTOR, targets),
        candidate=command(),
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.FORBIDDEN
    assert port.commands == []


def test_legacy_wire_execution_command_cannot_reach_execution_port() -> None:
    port = RecordingCanonicalExecutionPort()

    result = submit_paper_execution(
        principal=_paper_executor(),
        candidate=_legacy_wire_command(),
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert port.commands == []


def test_hash_tampered_canonical_command_cannot_reach_execution_port() -> None:
    port = RecordingCanonicalExecutionPort()
    tampered = command().model_copy(update={"command_hash": "f" * 64})

    result = submit_paper_execution(
        principal=_paper_executor(),
        candidate=tampered,
        port=port,
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INVALID_INPUT
    assert port.commands == []


def test_assigned_paper_executor_submits_validated_canonical_command_once() -> None:
    port = RecordingCanonicalExecutionPort()
    execution_command = command()

    result = submit_paper_execution(
        principal=_paper_executor(),
        candidate=execution_command,
        port=port,
    )

    assert isinstance(result, Success)
    assert port.commands == [execution_command]


def _paper_executor() -> LocalPrincipal:
    return _service(ServiceIdentity.PAPER_EXECUTOR, _account_targets(ACCOUNT_ID))


def _service(
    identity: ServiceIdentity,
    targets: frozenset[AccessTarget],
) -> LocalPrincipal:
    return LocalPrincipal(
        subject=f"service:{identity.value}",
        principal_kind=PrincipalKind.SERVICE,
        service_identity=identity,
        targets=targets,
    )


def _legacy_wire_command() -> WireExecutionCommand:
    intent = WireOrderIntent(
        intent_id=UUID("46000000-0000-4000-8000-000000000010"),
        run_id=UUID("46000000-0000-4000-8000-000000000011"),
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        side=WireOrderSide.BUY,
        order_type=WireOrderType.MARKET,
        quantity=Decimal("5"),
        time_in_force=WireTimeInForce.DAY,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        risk_decision_id=UUID("46000000-0000-4000-8000-000000000012"),
        reservation_id=UUID("46000000-0000-4000-8000-000000000013"),
        portfolio_snapshot_id=UUID("46000000-0000-4000-8000-000000000014"),
        aggregate_sequence=1,
        idempotency_key="legacy-wire-execution",
        execution_model_version="paper-v1",
        created_at=NOW,
    )
    return WireExecutionCommand(
        command_id=UUID("46000000-0000-4000-8000-000000000015"),
        intent=intent,
        attempt_generation=1,
        attempt_nonce="legacy-wire-attempt",
        issued_at=NOW,
    )
