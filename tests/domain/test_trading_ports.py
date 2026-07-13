from __future__ import annotations

from stonks_agent.domain.errors import Result
from stonks_agent.domain.fills import ExecutionReceipt
from stonks_agent.domain.journal import JournalTransaction
from stonks_agent.domain.orders import ExecutionCommand
from stonks_agent.domain.portfolio import PortfolioTarget
from stonks_agent.domain.portfolio_construction import BuildTargetCommand
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.risk_evaluation import BuildRiskDecisionCommand
from stonks_agent.ports.execution import CanonicalExecutionPort
from stonks_agent.ports.ledger import LedgerHead, LedgerPort
from stonks_agent.ports.portfolio_policy import PortfolioPolicyPort
from stonks_agent.ports.risk_policy import RiskPolicyPort


class PortfolioPolicy:
    def build_target(
        self,
        command: BuildTargetCommand,
    ) -> Result[PortfolioTarget]:
        raise NotImplementedError


class RiskPolicy:
    def evaluate(
        self,
        command: BuildRiskDecisionCommand,
    ) -> Result[RiskDecision]:
        raise NotImplementedError


class Executor:
    def submit(self, command: ExecutionCommand) -> Result[ExecutionReceipt]:
        raise NotImplementedError

    def get_receipt(
        self, *, account_id: str, idempotency_key: str
    ) -> Result[ExecutionReceipt]:
        raise NotImplementedError


class Ledger:
    def get_head(self, account_id: str) -> Result[LedgerHead]:
        raise NotImplementedError

    def append(
        self,
        transaction: JournalTransaction,
        *,
        expected_sequence: int,
        expected_hash: str | None,
    ) -> Result[JournalTransaction]:
        raise NotImplementedError

    def list_transactions(
        self, account_id: str, *, after_sequence: int = 0
    ) -> Result[tuple[JournalTransaction, ...]]:
        raise NotImplementedError


def test_p4_strategy_and_adapter_ports_are_runtime_checkable() -> None:
    assert isinstance(PortfolioPolicy(), PortfolioPolicyPort)
    assert isinstance(RiskPolicy(), RiskPolicyPort)
    assert isinstance(Executor(), CanonicalExecutionPort)
    assert isinstance(Ledger(), LedgerPort)
