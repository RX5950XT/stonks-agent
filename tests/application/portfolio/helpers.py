from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from stonks_agent.application.portfolio.build_target import (
    BuildTargetCommand,
    PortfolioMark,
    PortfolioPolicy,
    PortfolioSignalCandidate,
    load_portfolio_policy,
)
from stonks_agent.domain.evaluation import (
    MANDATORY_EVALUATION_CHECKS,
    EvaluationCheck,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PositionBalance,
)
from stonks_agent.domain.signal import AlphaSignal, SignalDirection, SignalSource
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyKind,
    StrategyManifest,
    StrategyRegistryEntry,
)
from stonks_contracts.common import ConfidenceCalibration

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 7, 13, 10, tzinfo=UTC)
ACCOUNT_ID = "portfolio-paper"
SNAPSHOT_ID = UUID("43000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("43000000-0000-4000-8000-000000000002")
INSTRUMENT_A = UUID("43000000-0000-4000-8000-000000000010")
INSTRUMENT_B = UUID("43000000-0000-4000-8000-000000000020")
SIGNAL_A = UUID("43000000-0000-4000-8000-000000000101")
SIGNAL_B = UUID("43000000-0000-4000-8000-000000000102")
SIGNAL_C = UUID("43000000-0000-4000-8000-000000000103")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def configured_policy() -> PortfolioPolicy:
    return load_portfolio_policy(ROOT / "config" / "policies" / "portfolio_v1.yaml")


def snapshot(
    *,
    cash_amount: Decimal = Decimal("10000.00"),
    positions: tuple[PositionBalance, ...] | None = None,
) -> AccountPortfolioSnapshot:
    if positions is None:
        positions = (
            PositionBalance(
                instrument_id=INSTRUMENT_A,
                quantity=Decimal("10"),
                sellable_quantity=Decimal("10"),
                reserved_quantity=Decimal("0"),
                quantum=Decimal("1"),
            ),
        )
    return AccountPortfolioSnapshot(
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        account_aggregate_sequence=7,
        portfolio_sequence=3,
        ledger_sequence=11,
        ledger_hash=HASH_A,
        cash=(
            CashBalance(
                currency="USD",
                settled_amount=cash_amount,
                reserved_amount=Decimal("0.00"),
                quantum=Decimal("0.01"),
            ),
        ),
        positions=positions,
        pending_order_ids=(),
    )


def mark(
    instrument_id: UUID = INSTRUMENT_A,
    *,
    price: Decimal = Decimal("100"),
    as_of: datetime = NOW,
    quantity_quantum: Decimal = Decimal("1"),
) -> PortfolioMark:
    return PortfolioMark(
        instrument_id=instrument_id,
        as_of=as_of,
        currency="USD",
        price=price,
        quantity_quantum=quantity_quantum,
    )


def _manifest(
    strategy_id: str, strategy_version: str, ordinal: int
) -> StrategyManifest:
    return StrategyManifest(
        manifest_id=UUID(int=0x43000000000040008000000000000200 + ordinal),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        kind=StrategyKind.DETERMINISTIC,
        source_artifact_ref=f"sha256:{HASH_A}",
        runtime_hash=HASH_B,
        feature_spec_hash=HASH_C,
        label_spec_hash=HASH_D,
        universe_spec_hash=HASH_E,
        cost_model_hash=HASH_F,
        split_policy_hash=HASH_A,
        parameters_hash=HASH_B,
        owner="quant-research",
        deterministic=True,
        created_at=NOW - timedelta(days=30),
    )


def _evaluation(manifest: StrategyManifest, ordinal: int) -> EvaluationReport:
    return EvaluationReport(
        report_id=UUID(int=0x43000000000040008000000000000300 + ordinal),
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.strategy_version,
        strategy_manifest_hash=manifest.manifest_hash,
        dataset_snapshot_id=UUID(int=0x43000000000040008000000000000400 + ordinal),
        data_hash=HASH_C,
        runtime_hash=manifest.runtime_hash,
        evaluation_policy_hash=HASH_D,
        as_of=NOW - timedelta(days=2),
        window_start=NOW - timedelta(days=367),
        window_end=NOW - timedelta(days=3),
        checks=tuple(
            EvaluationCheck(kind=kind, status=EvaluationCheckStatus.PASSED)
            for kind in MANDATORY_EVALUATION_CHECKS
        ),
        metrics=(EvaluationMetric(name="net_alpha", value="0.01", unit="return"),),
        calibration=ConfidenceCalibration.CALIBRATED,
        baseline_ids=("baseline-last-value/1.0.0",),
        report_artifact_ref=f"sha256:{HASH_E}",
        valid_until=NOW + timedelta(days=30),
        created_at=NOW - timedelta(days=1),
        passed=True,
    )


def candidate(
    *,
    strategy_id: str,
    signal_id: UUID,
    value: Decimal,
    confidence: Decimal,
    instrument_id: UUID = INSTRUMENT_A,
    ordinal: int,
    state: PromotionState = PromotionState.PAPER_ELIGIBLE,
    generated_at: datetime | None = None,
) -> PortfolioSignalCandidate:
    strategy_version = "1.0.0"
    manifest = _manifest(strategy_id, strategy_version, ordinal)
    evaluation = _evaluation(manifest, ordinal)
    registry = StrategyRegistryEntry(
        manifest=manifest,
        state=state,
        evaluation_report_id=evaluation.report_id,
        evaluation_hash=evaluation.evaluation_hash,
        version=4,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
    )
    direction = (
        SignalDirection.LONG
        if value > 0
        else SignalDirection.SHORT
        if value < 0
        else SignalDirection.NEUTRAL
    )
    generated = generated_at or NOW - timedelta(minutes=1)
    signal = AlphaSignal(
        signal_id=signal_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument_id=instrument_id,
        as_of=NOW - timedelta(minutes=5),
        generated_at=generated,
        stale_at=generated + timedelta(hours=1),
        expires_at=generated + timedelta(hours=2),
        horizon="1 session",
        value=value,
        confidence=confidence,
        calibration=ConfidenceCalibration.CALIBRATED,
        direction=direction,
        source=SignalSource.DETERMINISTIC,
        strategy_manifest_hash=manifest.manifest_hash,
        dataset_snapshot_id=UUID(int=0x43000000000040008000000000000500 + ordinal),
        data_hash=HASH_C,
        runtime_hash=manifest.runtime_hash,
        evaluation_policy_hash=evaluation.evaluation_policy_hash,
        raw_output_artifact_ref=f"sha256:{HASH_F}",
        evaluation_report_id=evaluation.report_id,
        evaluation_hash=evaluation.evaluation_hash,
    )
    return PortfolioSignalCandidate(
        signal=signal,
        registry=registry,
        evaluation=evaluation,
    )


def candidates() -> tuple[PortfolioSignalCandidate, ...]:
    return (
        candidate(
            strategy_id="baseline-last-value",
            signal_id=SIGNAL_A,
            value=Decimal("0.8"),
            confidence=Decimal("0.75"),
            ordinal=1,
        ),
        candidate(
            strategy_id="baseline-moving-average",
            signal_id=SIGNAL_B,
            value=Decimal("0.4"),
            confidence=Decimal("0.5"),
            ordinal=2,
        ),
    )


def command(
    *,
    marks: tuple[PortfolioMark, ...] | None = None,
    signal_candidates: tuple[PortfolioSignalCandidate, ...] | None = None,
    account_snapshot: AccountPortfolioSnapshot | None = None,
) -> BuildTargetCommand:
    return BuildTargetCommand(
        target_id=TARGET_ID,
        snapshot=account_snapshot or snapshot(),
        base_currency="USD",
        marks=(mark(),) if marks is None else marks,
        signal_candidates=candidates()
        if signal_candidates is None
        else signal_candidates,
    )
