from __future__ import annotations

import ast
import inspect

import stonks_agent.ports.backtest_engine as backtest_port
from stonks_agent.ports.backtest_engine import BacktestEnginePort
from stonks_contracts.backtest import BacktestJob, BacktestOrder


def test_backtest_port_has_no_paper_execution_or_engine_runtime_dependency() -> None:
    tree = ast.parse(inspect.getsource(backtest_port))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "nautilus_trader",
        "QuantConnect",
        "stonks_agent.domain.orders",
        "stonks_agent.ports.execution",
        "stonks_agent.ports.ledger",
        "stonks_agent.ports.trading_repository",
    }

    assert imported.isdisjoint(forbidden)
    public_methods = {
        name
        for name, value in BacktestEnginePort.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {"run"}


def test_backtest_wire_surface_is_simulation_only() -> None:
    job_fields = set(BacktestJob.model_fields)
    order_fields = set(BacktestOrder.model_fields)
    forbidden = {
        "account_reservation",
        "broker_credentials",
        "execution_receipt",
        "ledger",
        "risk_decision",
    }

    assert job_fields.isdisjoint(forbidden)
    assert order_fields.isdisjoint(forbidden)
    assert BacktestJob.model_fields["execution_mode"].default == "backtest"
    assert BacktestOrder.model_fields["simulation_only"].default is True
