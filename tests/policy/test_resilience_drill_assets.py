from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = {
    "provider-outage.md": (
        "DATA_UNAVAILABLE",
        "不得建立 target/order",
        "fallback",
    ),
    "worker-crash.md": ("generation", "nonce", "lease", "quarantine"),
    "db-restore.md": ("pg_dump", "pg_restore", "Alembic", "RTO", "RPO"),
    "ledger-mismatch.md": ("rollback", "global kill switch", "reconcile"),
    "kill-switch.md": ("paper activate", "paper resume", "fill/journal"),
    "dead-letter.md": ("dead_letter", "non-retry", "attempt", "追單"),
}


def test_resilience_runbooks_define_fail_closed_recovery_and_evidence() -> None:
    for filename, tokens in RUNBOOKS.items():
        path = ROOT / "docs" / "runbooks" / filename
        content = path.read_text(encoding="utf-8")
        for common in ("paper-only", "停止條件", "復原 gate", "稽核證據"):
            assert common in content, f"{filename}: missing {common}"
        for token in tokens:
            assert token in content, f"{filename}: missing {token}"


def test_ci_runs_resilience_matrix_and_actual_restore_with_read_only_authority() -> (
    None
):
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    content = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(content)
    job = workflow["jobs"]["resilience"]

    assert job["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] <= 25
    assert "tests/resilience" in content
    assert "scripts/drill_postgres_restore.py" in content
    assert "resilience-report.json" in content
    assert "--output" in content
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in content
    assert "id-token: write" not in str(job)
    assert "packages: write" not in str(job)
