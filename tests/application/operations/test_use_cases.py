from __future__ import annotations

from typing import Self
from uuid import UUID

from application.ledger.helpers import ACCOUNT_ID, NOW, opening
from stonks_agent.application.ledger.replay import replay_journal
from stonks_agent.application.operations.activate_kill_switch import (
    activate_kill_switch,
    read_kill_switch,
    read_operator_actions,
)
from stonks_agent.application.operations.reconcile import reconcile_paper_state
from stonks_agent.application.operations.resume import resume_paper
from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_agent.domain.operations import (
    ActivateKillSwitchCommand,
    KillSwitchScope,
    OperatorActionType,
    PaperKillSwitchState,
    PaperOperationRecord,
    PaperOperatorAction,
    PaperReconciliationResult,
    ReconcilePaperCommand,
    ResumePaperCommand,
    ResumePreparation,
)
from stonks_agent.ports.trading_unit_of_work import TradingCommitError

HASH_A = "a" * 64
ACTION_ID = UUID("89000000-0000-4000-8000-000000000001")
OPERATOR = LocalPrincipal(
    subject="operator:one",
    roles=frozenset({Role.PAPER_OPERATOR}),
    targets=frozenset(
        {
            AccessTarget(kind=ResourceKind.ACCOUNT, identifier=ACCOUNT_ID),
            AccessTarget(kind=ResourceKind.PAPER_GLOBAL, identifier="global"),
        }
    ),
)
ADMIN = LocalPrincipal(subject="admin:one", roles=frozenset({Role.ADMIN}))
RESEARCHER = LocalPrincipal(
    subject="researcher:one", roles=frozenset({Role.RESEARCHER})
)


def switch(*, active: bool = True, version: int = 2) -> PaperKillSwitchState:
    return PaperKillSwitchState(
        switch_id=UUID("89000000-0000-4000-8000-000000000002"),
        scope=KillSwitchScope.GLOBAL,
        account_id=None,
        active=active,
        reason_code="operator_requested",
        actor=OPERATOR.subject,
        version=version,
        created_at=NOW,
        updated_at=NOW,
    )


def action(
    action_type: OperatorActionType,
    *,
    sequence: int = 1,
) -> PaperOperatorAction:
    return PaperOperatorAction.create(
        action_id=ACTION_ID,
        sequence=sequence,
        action_type=action_type,
        scope=KillSwitchScope.GLOBAL,
        account_id=None,
        actor=OPERATOR.subject,
        reason_code=action_type.value,
        switch_version=2,
        cancelled_order_ids=(),
        reconciliation_hashes=(),
        mismatch_reasons=(),
        occurred_at=NOW,
        previous_action_hash=None,
    )


def record(action_type: OperatorActionType) -> PaperOperationRecord:
    return PaperOperationRecord(state=switch(), action=action(action_type))


class FakeLedger:
    def __init__(self, *, drift: bool = False) -> None:
        self._opening = opening()
        replayed = replay_journal(self._opening, ())
        assert isinstance(replayed, Success)
        self._projection = replayed.value
        if drift:
            self._projection = self._projection.model_copy(
                update={"projection_hash": HASH_A}
            )

    def get_opening_snapshot(self, account_id: str):  # type: ignore[no-untyped-def]
        assert account_id == ACCOUNT_ID
        return Success(self._opening)

    def list_transactions(self, account_id: str):  # type: ignore[no-untyped-def]
        assert account_id == ACCOUNT_ID
        return Success(())

    def get_projection(self, account_id: str):  # type: ignore[no-untyped-def]
        assert account_id == ACCOUNT_ID
        return Success(self._projection)

    def validate_account_graph(self, account_id: str):  # type: ignore[no-untyped-def]
        assert account_id == ACCOUNT_ID
        return Success(True)


class FakeOperations:
    def __init__(self) -> None:
        self.state = switch()
        self.actions = (action(OperatorActionType.ACTIVATED),)
        self.preparation = ResumePreparation(
            state=self.state,
            account_ids=(ACCOUNT_ID,),
        )
        self.last_call: str | None = None

    def get_kill_switch(self, scope, account_id):  # type: ignore[no-untyped-def]
        del scope, account_id
        return Success(self.state)

    def list_actions(self, *, after_sequence=0):  # type: ignore[no-untyped-def]
        return Success(
            tuple(item for item in self.actions if item.sequence > after_sequence)
        )

    def activate(self, command, *, actor):  # type: ignore[no-untyped-def]
        assert actor == OPERATOR.subject
        self.last_call = "activate"
        return Success(record(OperatorActionType.ACTIVATED))

    def record_reconciliation(self, command, report, *, actor):  # type: ignore[no-untyped-def]
        del command, report
        assert actor == OPERATOR.subject
        self.last_call = "reconciled"
        return Success(
            PaperReconciliationResult(
                report=_matched_report(),
                state=self.state,
                action=action(OperatorActionType.RECONCILED),
            )
        )

    def fail_reconciliation(self, command, *, actor, mismatch_reasons):  # type: ignore[no-untyped-def]
        del command, mismatch_reasons
        assert actor == OPERATOR.subject
        self.last_call = "reconciliation_failed"
        return Success(record(OperatorActionType.RECONCILIATION_FAILED))

    def prepare_resume(self, command):  # type: ignore[no-untyped-def]
        del command
        self.last_call = "prepare_resume"
        return Success(self.preparation)

    def complete_resume(self, command, preparation, reports, *, actor):  # type: ignore[no-untyped-def]
        del command, preparation, reports
        assert actor == OPERATOR.subject
        self.last_call = "resumed"
        return Success(record(OperatorActionType.RESUMED))

    def reject_resume(self, command, preparation, *, actor, mismatch_reasons):  # type: ignore[no-untyped-def]
        del command, preparation, mismatch_reasons
        assert actor == OPERATOR.subject
        self.last_call = "resume_rejected"
        return Success(action(OperatorActionType.RESUME_REJECTED))


class FakeUnitOfWork:
    def __init__(
        self,
        operations: FakeOperations,
        *,
        drift: bool = False,
        fail_commit: bool = False,
    ) -> None:
        self.operations = operations
        self.ledger = FakeLedger(drift=drift)
        self.fail_commit = fail_commit
        self.commits = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def commit(self) -> None:
        if self.fail_commit:
            raise TradingCommitError("failed")
        self.commits += 1

    def rollback(self) -> None:
        return None


class Factory:
    def __init__(self, *, drift: bool = False, fail_commit: bool = False) -> None:
        self.operations = FakeOperations()
        self.uow = FakeUnitOfWork(
            self.operations,
            drift=drift,
            fail_commit=fail_commit,
        )
        self.calls = 0

    def __call__(self) -> FakeUnitOfWork:
        self.calls += 1
        return self.uow


def activate_command() -> ActivateKillSwitchCommand:
    return ActivateKillSwitchCommand(
        action_id=ACTION_ID,
        scope=KillSwitchScope.GLOBAL,
        account_id=None,
        expected_version=1,
        reason_code="operator_requested",
        requested_at=NOW,
    )


def reconcile_command() -> ReconcilePaperCommand:
    return ReconcilePaperCommand(
        action_id=ACTION_ID,
        account_id=ACCOUNT_ID,
        requested_at=NOW,
    )


def resume_command() -> ResumePaperCommand:
    return ResumePaperCommand(
        action_id=ACTION_ID,
        scope=KillSwitchScope.GLOBAL,
        account_id=None,
        expected_version=2,
        reason_code="reconciliation_passed",
        requested_at=NOW,
    )


def test_activate_is_authorized_audited_and_committed() -> None:
    factory = Factory()

    result = activate_kill_switch(OPERATOR, activate_command(), factory)

    assert isinstance(result, Success)
    assert result.value.action.action_type is OperatorActionType.ACTIVATED
    assert factory.operations.last_call == "activate"
    assert factory.uow.commits == 1


def test_non_operator_cannot_read_or_mutate_operator_state() -> None:
    factory = Factory()

    denied = activate_kill_switch(RESEARCHER, activate_command(), factory)
    read = read_kill_switch(RESEARCHER, KillSwitchScope.GLOBAL, None, factory)

    assert isinstance(denied, Failure)
    assert denied.error.code is ErrorCode.FORBIDDEN
    assert isinstance(read, Failure)
    assert factory.uow.commits == 0


def test_reads_return_verified_state_and_action_chain() -> None:
    factory = Factory()

    state_result = read_kill_switch(OPERATOR, KillSwitchScope.GLOBAL, None, factory)
    actions_result = read_operator_actions(
        ADMIN, after_sequence=0, unit_of_work=factory
    )

    assert state_result == Success(factory.operations.state)
    assert actions_result == Success(factory.operations.actions)


def test_reconcile_records_match_and_commits() -> None:
    factory = Factory()

    result = reconcile_paper_state(OPERATOR, reconcile_command(), factory)

    assert isinstance(result, Success)
    assert result.value.report.matched
    assert factory.operations.last_call == "reconciled"
    assert factory.uow.commits == 1


def test_reconcile_drift_activates_switch_audits_and_returns_conflict() -> None:
    factory = Factory(drift=True)

    result = reconcile_paper_state(OPERATOR, reconcile_command(), factory)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert factory.operations.last_call == "reconciliation_failed"
    assert factory.uow.commits == 1


def test_resume_only_completes_after_all_locked_accounts_reconcile() -> None:
    factory = Factory()

    result = resume_paper(OPERATOR, resume_command(), factory)

    assert isinstance(result, Success)
    assert factory.operations.last_call == "resumed"
    assert factory.uow.commits == 1


def test_resume_reconciliation_drift_keeps_switch_active_and_audits_rejection() -> None:
    factory = Factory(drift=True)

    result = resume_paper(OPERATOR, resume_command(), factory)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    assert factory.operations.last_call == "resume_rejected"
    assert factory.uow.commits == 1


def test_commit_failure_never_reports_success() -> None:
    factory = Factory(fail_commit=True)

    result = activate_kill_switch(OPERATOR, activate_command(), factory)

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.INTERNAL_ERROR


def test_cross_account_global_and_audit_access_are_denied_before_uow() -> None:
    factory = Factory()
    other_account = LocalPrincipal(
        subject="operator:other",
        roles=frozenset({Role.PAPER_OPERATOR}),
        targets=frozenset(
            {
                AccessTarget(
                    kind=ResourceKind.ACCOUNT,
                    identifier="paper-other",
                )
            }
        ),
    )

    results = (
        activate_kill_switch(other_account, activate_command(), factory),
        reconcile_paper_state(other_account, reconcile_command(), factory),
        read_operator_actions(OPERATOR, after_sequence=0, unit_of_work=factory),
    )

    assert all(
        isinstance(result, Failure) and result.error.code is ErrorCode.FORBIDDEN
        for result in results
    )
    assert factory.calls == 0


def _matched_report() -> LedgerReconciliationReport:
    return LedgerReconciliationReport(
        account_id=ACCOUNT_ID,
        as_of=NOW,
        ledger_sequence=0,
        replay_projection_hash=opening().snapshot_hash,
        database_projection_hash=opening().snapshot_hash,
        matched=True,
        mismatch_reasons=(),
    )
