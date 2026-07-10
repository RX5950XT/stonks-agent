from __future__ import annotations

import json

from sqlalchemy import Engine
from typer.testing import CliRunner

from stonks_agent.entrypoints.worker import app


def test_worker_claim_once_handles_empty_queue_without_leaking_database_url(
    clean_database: Engine,
    postgres_url: str,
) -> None:
    del clean_database
    result = CliRunner().invoke(
        app,
        ["claim-once", "--worker-id", "worker-cli"],
        env={"STONKS_DATABASE_URL": postgres_url},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["data"] == {"claimed": False, "lease": None}
    assert postgres_url not in result.stdout
