"""Local CLI for the paper-only Stonks Agent core."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

import typer

from stonks_agent.adapters.fakes.platform import build_fake_run_service
from stonks_agent.application.workflows.run_cycle import RunCycleRequest
from stonks_agent.entrypoints.cli_commands.data import app as data_app
from stonks_agent.entrypoints.cli_commands.report import app as report_app
from stonks_agent.entrypoints.cli_commands.research import app as research_app

app = typer.Typer(
    add_completion=False,
    help="Evidence-first、paper-only 的投資研究代理。",
    no_args_is_help=True,
)
app.add_typer(data_app, name="data", help="Canonical data ingestion commands.")
app.add_typer(research_app, name="research", help="Queued research commands.")
app.add_typer(report_app, name="report", help="Read-only report commands.")


@app.callback()
def main() -> None:
    """Stonks Agent command group."""


@app.command("fake-cycle")
def fake_cycle(
    symbol: Annotated[str, typer.Option(help="測試標的代號")] = "AAPL",
    as_of: Annotated[
        str,
        typer.Option(help="含時區的 RFC 3339 決策截止時間"),
    ] = "2026-01-02T21:00:00+00:00",
    idempotency_key: Annotated[
        str,
        typer.Option(help="呼叫端提供的冪等鍵"),
    ] = "local-fake-cycle",
) -> None:
    """執行 deterministic fixture 的完整 paper/replay 閉環。"""
    normalized_symbol = _validate_symbol(symbol)
    decision_time = _parse_timestamp(as_of)
    key = _validate_idempotency_key(idempotency_key)
    service = build_fake_run_service(clock=decision_time, seed="cli-v1")
    result = service.run(
        RunCycleRequest(
            idempotency_key=key,
            account_id="paper-local",
            instrument_id=f"instrument-{normalized_symbol.lower()}",
            symbol=normalized_symbol,
            as_of=decision_time,
            evidence_available_at=decision_time,
            signal_value=Decimal("0.80"),
            signal_confidence=Decimal("0.90"),
        )
    )
    fill = result.execution_receipt.fill if result.execution_receipt else None
    envelope = {
        "success": True,
        "status": 200,
        "data": {
            "run_id": result.run_id,
            "run_status": result.status,
            "symbol": normalized_symbol,
            "fill_price": format(fill.price, "f") if fill else None,
            "projection_hash": result.projection_hash,
            "report": result.report.conclusion,
        },
        "error": None,
        "metadata": {"execution_mode": "paper"},
    }
    typer.echo(json.dumps(envelope, ensure_ascii=False, sort_keys=True))


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("as-of 必須是 RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise typer.BadParameter("as-of 必須包含 timezone offset")
    return parsed


def _validate_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,15}", normalized):
        raise typer.BadParameter("symbol 格式無效")
    return normalized


def _validate_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise typer.BadParameter("idempotency key 長度必須為 1 到 128")
    return normalized


if __name__ == "__main__":
    app()
