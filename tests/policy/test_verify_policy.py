from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]


def test_quality_gate_checks_format_before_lint() -> None:
    commands = cast(
        Callable[..., tuple[tuple[str, ...], ...]],
        run_path(str(ROOT / "scripts" / "verify.py"))["commands"],
    )
    configured = commands(with_postgres=False)
    format_check = (sys.executable, "-m", "ruff", "format", "--check", ".")
    lint_check = (sys.executable, "-m", "ruff", "check", ".")

    assert format_check in configured
    assert configured.index(format_check) < configured.index(lint_check)
