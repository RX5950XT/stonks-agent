from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from integration.postgres.test_paper_execution import (
    _broker,
    _ledger_policy,
    execution_request,
)
from integration.postgres.test_trading_persistence import (
    ACCOUNT_ID,
    INTENT_ID,
    reservation_order,
    seed,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from stonks_agent.adapters.postgres.trading_repository import (
    PostgresTradingRepository,
)
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.execution.execute import execute_reference_paper
from stonks_agent.application.operations.activate_kill_switch import (
    activate_kill_switch,
    read_operator_actions,
)
from stonks_agent.application.operations.reconcile import reconcile_paper_state
from stonks_agent.application.operations.resume import resume_paper
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    OperatorActionType,
    ReconcilePaperCommand,
    ResumePaperCommand,
)
from stonks_agent.entrypoints.cli import app as cli_app

pytestmark = pytest.mark.postgres

OPERATOR = LocalPrincipal(
    subject="operator:postgres", roles=frozenset({Role.PAPER_OPERATOR})
)
RUNNER = CliRunner()


def _seed_pending_order(engine: Engine) -> None:
    mutation, intent = reservation_order()
    with Session(engine) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        assert isinstance(
            repository.create_reservation_order(mutation, intent), Success
        )
        session.commit()


def _activate_command(
    action_id: UUID,
    *,
    expected_version: int = 1,
) -> ActivateKillSwitchCommand:
    return ActivateKillSwitchCommand(
        action_id=action_id,
        scope=KillSwitchScope.GLOBAL,
        account_id=None,
        expected_version=expected_version,
        reason_code="operator_requested",
        requested_at=datetime.now(UTC),
    )


def test_activation_atomically_terminates_pending_order_and_releases_reservation(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    action_id = UUID("90000000-0000-4000-8000-000000000001")

    result = activate_kill_switch(
        OPERATOR,
        _activate_command(action_id),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Success)
    assert result.value.state.active
    assert result.value.action.cancelled_order_ids == (INTENT_ID,)
    with clean_database.connect() as connection:
        values = (
            connection.execute(
                text(
                    """
                select
                  (select to_status from order_event order by sequence desc limit 1) status,
                  (select state from account_reservation limit 1) reservation_state,
                  (select reserved_amount from paper_cash_projection
                    where account_id=:account_id and currency='USD') reserved,
                  (select count(*) from paper_fill) fills,
                  (select count(*) from journal_transaction) journals,
                  (select count(*) from paper_operator_action) actions
                """
                ),
                {"account_id": ACCOUNT_ID},
            )
            .mappings()
            .one()
        )
    assert values["status"] in {"cancelled", "expired"}
    assert values["reservation_state"] in {"released", "expired"}
    assert values["reserved"] == 0
    assert values["fills"] == values["journals"] == 0
    assert values["actions"] == 1


def test_operator_activation_blocks_new_execution_commands(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    activated = activate_kill_switch(
        OPERATOR,
        _activate_command(UUID("90000000-0000-4000-8000-000000000025")),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(activated, Success)

    blocked = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(blocked, Failure)
    assert blocked.error.code is ErrorCode.CONFLICT
    assert "kill switch is active" in blocked.error.message.lower()
    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from paper_fill")) == 0
        assert connection.scalar(text("select count(*) from journal_transaction")) == 0


def test_resume_reconciles_locked_account_before_disabling_global_switch(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    activated = activate_kill_switch(
        OPERATOR,
        _activate_command(UUID("90000000-0000-4000-8000-000000000002")),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(activated, Success)

    resumed = resume_paper(
        OPERATOR,
        ResumePaperCommand(
            action_id=UUID("90000000-0000-4000-8000-000000000003"),
            scope=KillSwitchScope.GLOBAL,
            account_id=None,
            expected_version=activated.value.state.version,
            reason_code="reconciliation_passed",
            requested_at=datetime.now(UTC) + timedelta(seconds=1),
        ),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(resumed, Success)
    assert not resumed.value.state.active
    actions = read_operator_actions(
        OPERATOR,
        after_sequence=0,
        unit_of_work=lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(actions, Success)
    assert tuple(item.action_type for item in actions.value) == (
        OperatorActionType.ACTIVATED,
        OperatorActionType.RESUMED,
    )


def test_successful_reconciliation_is_audited_without_changing_switch(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)

    result = reconcile_paper_state(
        OPERATOR,
        ReconcilePaperCommand(
            action_id=UUID("90000000-0000-4000-8000-000000000020"),
            account_id=ACCOUNT_ID,
            requested_at=datetime.now(UTC),
        ),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Success)
    assert result.value.report.matched
    assert result.value.action.action_type is OperatorActionType.RECONCILED
    assert not result.value.state.active


def test_account_scoped_activation_and_resume_only_mutate_exact_switch(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    activated = activate_kill_switch(
        OPERATOR,
        ActivateKillSwitchCommand(
            action_id=UUID("90000000-0000-4000-8000-000000000021"),
            scope=KillSwitchScope.ACCOUNT,
            account_id=ACCOUNT_ID,
            expected_version=0,
            reason_code="account_operator_requested",
            requested_at=datetime.now(UTC),
        ),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(activated, Success)

    resumed = resume_paper(
        OPERATOR,
        ResumePaperCommand(
            action_id=UUID("90000000-0000-4000-8000-000000000022"),
            scope=KillSwitchScope.ACCOUNT,
            account_id=ACCOUNT_ID,
            expected_version=activated.value.state.version,
            reason_code="reconciliation_passed",
            requested_at=datetime.now(UTC) + timedelta(seconds=1),
        ),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(resumed, Success)
    assert resumed.value.state.account_id == ACCOUNT_ID
    assert not resumed.value.state.active
    with clean_database.connect() as connection:
        global_active = connection.scalar(
            text("select active from paper_kill_switch where scope='global'")
        )
    assert global_active is False


def test_resume_drift_records_rejection_and_keeps_switch_active(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    activated = activate_kill_switch(
        OPERATOR,
        _activate_command(UUID("90000000-0000-4000-8000-000000000023")),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(activated, Success)
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "alter table paper_ledger_account_projection disable trigger "
                "trg_paper_ledger_projection_mutation"
            )
        )
        connection.execute(
            text(
                "update paper_ledger_account_projection set debit_total=debit_total-1 "
                "where account_id=:account_id and ledger_account='asset:cash:USD'"
            ),
            {"account_id": ACCOUNT_ID},
        )
        connection.execute(
            text(
                "alter table paper_ledger_account_projection enable trigger "
                "trg_paper_ledger_projection_mutation"
            )
        )

    resumed = resume_paper(
        OPERATOR,
        ResumePaperCommand(
            action_id=UUID("90000000-0000-4000-8000-000000000024"),
            scope=KillSwitchScope.GLOBAL,
            account_id=None,
            expected_version=activated.value.state.version,
            reason_code="reconciliation_passed",
            requested_at=datetime.now(UTC) + timedelta(seconds=1),
        ),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(resumed, Failure)
    with clean_database.connect() as connection:
        state = connection.scalar(
            text("select active from paper_kill_switch where scope='global'")
        )
        action_types = tuple(
            connection.scalars(
                text("select action_type from paper_operator_action order by sequence")
            ).all()
        )
    assert state is True
    assert action_types == ("activated", "resume_rejected")


def test_reconciliation_drift_activates_global_switch_and_commits_audit(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "alter table paper_ledger_account_projection disable trigger "
                "trg_paper_ledger_projection_mutation"
            )
        )
        connection.execute(
            text(
                """
                update paper_ledger_account_projection
                   set debit_total = debit_total - 1
                 where account_id=:account_id and ledger_account='asset:cash:USD'
                """
            ),
            {"account_id": ACCOUNT_ID},
        )
        connection.execute(
            text(
                "alter table paper_ledger_account_projection enable trigger "
                "trg_paper_ledger_projection_mutation"
            )
        )

    result = reconcile_paper_state(
        OPERATOR,
        ReconcilePaperCommand(
            action_id=UUID("90000000-0000-4000-8000-000000000004"),
            account_id=ACCOUNT_ID,
            requested_at=datetime.now(UTC),
        ),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        switch = connection.execute(
            text(
                "select active, reason_code from paper_kill_switch where scope='global'"
            )
        ).one()
        action_type = connection.scalar(
            text("select action_type from paper_operator_action")
        )
    assert switch.active
    assert switch.reason_code == "ledger_reconciliation_failed"
    assert action_type == OperatorActionType.RECONCILIATION_FAILED.value


def test_concurrent_activation_expected_version_has_one_winner(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)

    def activate(index: int):  # type: ignore[no-untyped-def]
        return activate_kill_switch(
            OPERATOR,
            _activate_command(UUID(int=1000 + index)),
            lambda: PostgresUnitOfWork(clean_database),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(activate, (1, 2)))

    assert sum(isinstance(item, Success) for item in results) == 1
    assert sum(isinstance(item, Failure) for item in results) == 1
    with clean_database.connect() as connection:
        assert (
            connection.scalar(text("select count(*) from paper_operator_action")) == 1
        )


def test_operator_action_is_database_append_only(clean_database: Engine) -> None:
    _seed_pending_order(clean_database)
    result = activate_kill_switch(
        OPERATOR,
        _activate_command(UUID("90000000-0000-4000-8000-000000000005")),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(result, Success)

    with (
        pytest.raises(DBAPIError, match="append-only"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text("update paper_operator_action set reason_code='tampered'")
        )


def test_paper_cli_uses_database_authority_for_activate_resume_and_audit(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    database = str(clean_database.url)
    activated = RUNNER.invoke(
        cli_app,
        [
            "paper",
            "activate",
            "--action-id",
            "90000000-0000-4000-8000-000000000011",
            "--scope",
            "global",
            "--expected-version",
            "1",
            "--reason-code",
            "operator_requested",
            "--database-url",
            database,
        ],
    )
    reconciled = RUNNER.invoke(
        cli_app,
        [
            "paper",
            "reconcile",
            "--action-id",
            "90000000-0000-4000-8000-000000000013",
            "--account-id",
            ACCOUNT_ID,
            "--database-url",
            database,
        ],
    )
    resumed = RUNNER.invoke(
        cli_app,
        [
            "paper",
            "resume",
            "--action-id",
            "90000000-0000-4000-8000-000000000012",
            "--scope",
            "global",
            "--expected-version",
            "2",
            "--reason-code",
            "reconciliation_passed",
            "--database-url",
            database,
        ],
    )
    status = RUNNER.invoke(
        cli_app,
        [
            "paper",
            "status",
            "--scope",
            "global",
            "--database-url",
            database,
        ],
    )
    actions = RUNNER.invoke(
        cli_app,
        ["paper", "actions", "--database-url", database],
    )

    assert (
        activated.exit_code
        == reconciled.exit_code
        == resumed.exit_code
        == status.exit_code
        == 0
    )
    assert '"active": false' in status.stdout
    assert actions.exit_code == 0
    assert '"action_type": "activated"' in actions.stdout
    assert '"action_type": "reconciled"' in actions.stdout
    assert '"action_type": "resumed"' in actions.stdout
