"""Exercise the local GUI facade against live OpenBB and durable PostgreSQL."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from stonks_agent.adapters.postgres.gui_research import (
    PostgresGuiResearchFacade,
)
from stonks_agent.composition.runtime import build_local_runtime
from stonks_agent.composition.worker import build_worker_composition
from stonks_agent.domain.errors import Failure
from stonks_agent.domain.gui_research import GuiResearchCommand
from stonks_agent.domain.latest_market_data import BarInterval
from stonks_agent.entrypoints.api.gui_research import GuiResearchApiOptions
from stonks_agent.entrypoints.gui import (
    OpenBBSidecarManager,
    prepare_ephemeral_openbb_runtime,
)


def main() -> None:
    arguments = _arguments()
    root = arguments.root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="stonks-gui-research-") as raw:
        ephemeral = prepare_ephemeral_openbb_runtime(Path(raw) / "auth")
        manager = OpenBBSidecarManager(
            root=root,
            environment=ephemeral.environment,
        )
        manager.start()
        runtime = build_local_runtime(
            database_url=arguments.database_url,
            artifact_root=root / ".data" / "artifacts",
        )
        try:
            environment = {
                "STONKS_ENVIRONMENT": "local",
            }
            worker = build_worker_composition(
                runtime,
                environment=environment,
                root=root,
                credentials=ephemeral.credentials,
            )
            facade = PostgresGuiResearchFacade(
                runtime=runtime,
                queue=worker.queue,
                handlers=worker.handlers,
                worker_lock=Lock(),
            )
            options = GuiResearchApiOptions()
            submitted = facade.submit(
                options.principal,
                GuiResearchCommand(
                    symbol=arguments.symbol,
                    interval=BarInterval.DAY,
                    profile=options.default_profile,
                    account_id=options.account_id,
                    requested_at=datetime.now(UTC),
                ),
            )
            if isinstance(submitted, Failure):
                raise RuntimeError(
                    f"GUI research submit failed: {submitted.error.code.value}"
                )
            deadline = time.monotonic() + arguments.timeout
            view = facade.read(options.principal, submitted.value.run_id)
            while (
                not isinstance(view, Failure)
                and view.value.status in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                facade.worker_once()
                view = facade.read(options.principal, submitted.value.run_id)
            events = facade.events(
                options.principal,
                submitted.value.run_id,
                after_sequence=0,
                limit=100,
            )
            print(
                json.dumps(
                    {
                        "success": not isinstance(view, Failure),
                        "status": (
                            view.error.code.value
                            if isinstance(view, Failure)
                            else view.value.status
                        ),
                        "run_id": str(submitted.value.run_id),
                        "error_code": (
                            None if isinstance(view, Failure) else view.value.error_code
                        ),
                        "events": (
                            []
                            if isinstance(events, Failure)
                            else [item.event_type for item in events.value]
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        finally:
            runtime.close()
            manager.stop()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--timeout", type=float, default=30)
    values = parser.parse_args()
    values.symbol = values.symbol.strip().upper()
    if (
        not values.symbol
        or len(values.symbol) > 16
        or any(
            not (character.isalnum() or character in ".-")
            for character in values.symbol
        )
    ):
        parser.error("symbol is invalid")
    if not 1 <= values.timeout <= 300:
        parser.error("timeout is invalid")
    return values


if __name__ == "__main__":
    main()
