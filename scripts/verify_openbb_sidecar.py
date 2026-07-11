#!/usr/bin/env python3
"""Fail-closed verification for the optional OpenBB AGPL sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from openbb_sidecar_container_policy import container_violations
from openbb_sidecar_policy import (
    EXPECTED_CHECKS,
    PolicyInputError,
    Violation,
    contract_violations,
    load_inputs,
)


def check_repository(root: Path) -> list[Violation]:
    """Return all violations; malformed required inputs raise."""

    inputs = load_inputs(root)
    return [*contract_violations(inputs), *container_violations(inputs)]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the script's parent repository)",
    )
    return parser


def _emit(violations: Sequence[Violation]) -> None:
    success = not violations
    payload = {
        "success": success,
        "status": "passed" if success else "failed",
        "data": {
            "check_count": len(EXPECTED_CHECKS),
            "violation_count": 0,
        }
        if success
        else None,
        "error": None
        if success
        else {
            "code": "OPENBB_SIDECAR_POLICY_VIOLATION",
            "message": "OpenBB sidecar policy checks failed",
            "details": [asdict(violation) for violation in violations],
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    args = _parser().parse_args()
    try:
        violations = check_repository(args.root)
    except PolicyInputError as error:
        violations = [Violation("POLICY_INPUT_ERROR", str(error))]
    _emit(violations)
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
