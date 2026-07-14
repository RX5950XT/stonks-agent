from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from integration.postgres.test_paper_execution import (
    ACCOUNT_ID,
    _broker,
    _ledger_policy,
    _seed_order,
    execution_request,
)
from integration.postgres.test_trading_persistence import decision
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.postgres.ledger_repository import PostgresLedgerRepository
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.execution.execute import execute_reference_paper
from stonks_agent.application.ledger.reconcile import reconcile_paper_account
from stonks_agent.application.monitoring.mark_to_market import mark_to_market
from stonks_agent.application.monitoring.outcomes import (
    build_outcome,
    save_outcome_evidence,
)
from stonks_agent.application.projections.queries import (
    read_nav_projection,
    read_portfolio_projection,
    read_risk_projection,
    record_nav_projection,
)
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import Success
from stonks_agent.domain.monitoring import (
    BuildOutcomeCommand,
    MarkToMarketCommand,
    OutcomeFillReference,
    PointInTimeMark,
    PortfolioValuation,
)
from stonks_contracts.common import stable_payload_hash
from stonks_contracts.market_data import DataQualityStatus
from stonks_contracts.report import (
    AnalysisReport,
    ClaimCertainty,
    ReportClaim,
    ReportReference,
)

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]
pytest_plugins = ["integration.postgres.conftest"]
VIEWER = LocalPrincipal(subject="viewer:p4-gate", roles=frozenset({Role.VIEWER}))
BENCHMARK = UUID("77000000-0000-4000-8000-000000000001")


def test_small_portfolio_closes_report_replay_reconciliation_and_projection_loop(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    request = execution_request()
    start = _valuation(clean_database, UUID(int=101), request.command.issued_at, ())
    assert isinstance(
        record_nav_projection(start, lambda: PostgresUnitOfWork(clean_database)),
        Success,
    )

    executed = execute_reference_paper(
        request,
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(executed, Success)
    fill = executed.value.outcome.receipt.fills[0]
    end_mark = _mark(
        fill.instrument_id,
        request.bars[0].close,
        request.bars[0].closes_at,
        request.bars[0].available_at,
        UUID(int=102),
    )
    end = _valuation(clean_database, UUID(int=103), request.as_of, (end_mark,))
    assert isinstance(
        record_nav_projection(end, lambda: PostgresUnitOfWork(clean_database)),
        Success,
    )

    outcome = _outcome(start, end, executed.value.outcome.receipt)
    evidence = save_outcome_evidence(outcome, MemoryArtifactStore())
    assert isinstance(evidence, Success)
    report = _report(outcome, evidence.value.evidence_id)
    replay = AnalysisReport.model_validate(report.model_dump(mode="json"))
    reconciled = reconcile_paper_account(
        ACCOUNT_ID,
        as_of=request.as_of + timedelta(seconds=2),
        unit_of_work=lambda: PostgresUnitOfWork(clean_database),
    )

    assert replay == report
    assert stable_payload_hash(replay) == stable_payload_hash(report)
    assert isinstance(reconciled, Success)
    assert reconciled.value.matched
    assert report.portfolio_target_refs[0].ref_id == decision().portfolio_target_id
    assert report.risk_decision_refs[0].ref_id == decision().decision_id
    assert report.order_intent_refs[0].ref_id == request.command.intent.intent_id
    assert report.fill_refs[0].ref_id == fill.fill_id
    assert report.outcome_refs[0].ref_id == outcome.outcome_id
    _assert_current_read_models(clean_database, end)


def _valuation(
    engine: Engine,
    valuation_id: UUID,
    as_of,  # type: ignore[no-untyped-def]
    marks: tuple[PointInTimeMark, ...],
) -> PortfolioValuation:
    with Session(engine) as session:
        ledger = PostgresLedgerRepository(session).get_projection(ACCOUNT_ID)
    assert isinstance(ledger, Success)
    result = mark_to_market(
        MarkToMarketCommand(
            valuation_id=valuation_id,
            account_id=ACCOUNT_ID,
            base_currency="USD",
            as_of=as_of,
            ledger=ledger.value,
            marks=marks,
            currency_quantum=Decimal("0.01"),
        )
    )
    assert isinstance(result, Success)
    return result.value


def _mark(
    instrument_id: UUID,
    price: Decimal,
    event_time,  # type: ignore[no-untyped-def]
    available_at,  # type: ignore[no-untyped-def]
    evidence_id: UUID,
) -> PointInTimeMark:
    return PointInTimeMark(
        instrument_id=instrument_id,
        currency="USD",
        price=price,
        event_time=event_time,
        available_at=available_at,
        evidence_id=evidence_id,
        source_artifact_ref=f"sha256:{'c' * 64}",
    )


def _outcome(start, end, receipt):  # type: ignore[no-untyped-def]
    risk = decision()
    fill = receipt.fills[0]
    benchmark_start = _mark(
        BENCHMARK,
        Decimal("100"),
        start.as_of - timedelta(seconds=1),
        start.as_of,
        UUID(int=104),
    )
    benchmark_end = _mark(
        BENCHMARK,
        Decimal("101"),
        end.as_of - timedelta(seconds=1),
        end.as_of,
        UUID(int=105),
    )
    built = build_outcome(
        BuildOutcomeCommand(
            outcome_id=UUID(int=106),
            decision=risk,
            valuations=(start, end),
            benchmark_start=benchmark_start,
            benchmark_end=benchmark_end,
            fill_refs=(
                OutcomeFillReference(
                    risk_decision_id=risk.decision_id,
                    risk_decision_hash=risk.decision_hash,
                    account_id=ACCOUNT_ID,
                    receipt_id=receipt.receipt_id,
                    receipt_hash=receipt.receipt_hash,
                    order_intent_id=receipt.order_intent_id,
                    intent_hash=receipt.intent_hash,
                    fill_id=fill.fill_id,
                    instrument_id=fill.instrument_id,
                    side=fill.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    fee_currency=fill.fee_currency,
                    fees=fill.fees,
                    occurred_at=fill.occurred_at,
                ),
            ),
            calculated_at=end.as_of + timedelta(seconds=1),
        )
    )
    assert isinstance(built, Success)
    return built.value


def _report(outcome, evidence_id):  # type: ignore[no-untyped-def]
    risk = decision()
    fill = outcome.fill_refs[0]
    return AnalysisReport(
        report_id=UUID(int=107),
        subject=ACCOUNT_ID,
        as_of=outcome.calculated_at,
        language="zh-TW",
        report_type="paper_outcome",
        conclusion="neutral_outlook",
        score=Decimal("0.5"),
        confidence=Decimal("1"),
        action_guardrails=("paper_only", "research_not_execution"),
        claims=(
            ReportClaim(
                claim_id=UUID(int=108),
                assertion="Paper outcome is fully reconciled.",
                certainty=ClaimCertainty.OBSERVED,
                data_quality=DataQualityStatus.AVAILABLE,
                evidence_refs=(evidence_id,),
            ),
        ),
        evidence_refs=(evidence_id,),
        signal_ids=risk.normalized_target.input_signal_ids,  # type: ignore[union-attr]
        portfolio_target_refs=(
            ReportReference(
                ref_id=risk.portfolio_target_id,
                content_hash=risk.input_target_hash,
            ),
        ),
        risk_decision_refs=(
            ReportReference(ref_id=risk.decision_id, content_hash=risk.decision_hash),
        ),
        order_intent_refs=(
            ReportReference(ref_id=fill.order_intent_id, content_hash=fill.intent_hash),
        ),
        fill_refs=(
            ReportReference(
                ref_id=fill.fill_id,
                content_hash=stable_payload_hash(
                    {
                        "instrument_id": str(fill.instrument_id),
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "fees": str(fill.fees),
                    }
                ),
            ),
        ),
        outcome_refs=(
            ReportReference(
                ref_id=outcome.outcome_id,
                content_hash=outcome.outcome_hash,
            ),
        ),
        generator_version="p4-phase-gate/1.0.0",
        policy_version="paper-outcome-report/1.0.0",
    )


def _assert_current_read_models(
    engine: Engine, expected_nav: PortfolioValuation
) -> None:
    factory = lambda: PostgresUnitOfWork(engine)  # noqa: E731
    portfolio = read_portfolio_projection(VIEWER, ACCOUNT_ID, factory)
    nav = read_nav_projection(VIEWER, ACCOUNT_ID, factory)
    risk = read_risk_projection(
        VIEWER,
        ACCOUNT_ID,
        as_of=expected_nav.as_of,
        unit_of_work=factory,
    )
    assert isinstance(portfolio, Success)
    assert isinstance(nav, Success) and nav.value == expected_nav
    assert isinstance(risk, Success)
