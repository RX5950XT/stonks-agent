"""CLI command for capability-scoped local report artifact reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.adapters.reporting.artifact_reader import ArtifactReportReader
from stonks_agent.application.research.request_run import read_report
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import Failure
from stonks_agent.entrypoints.api.envelope import error_envelope, success_envelope

app = typer.Typer(add_completion=False, no_args_is_help=True)
_PRINCIPAL = LocalPrincipal(subject="local-cli", roles=frozenset({Role.VIEWER}))


@app.command("show")
def show_command(
    content_hash: Annotated[str, typer.Option()],
    artifact_root: Annotated[Path, typer.Option(envvar="STONKS_ARTIFACT_ROOT")] = Path(
        ".data/artifacts"
    ),
) -> None:
    result = read_report(
        _PRINCIPAL,
        content_hash,
        ArtifactReportReader(LocalArtifactStore(artifact_root)),
    )
    if isinstance(result, Failure):
        _emit(error_envelope(result.error))
        raise typer.Exit(code=2)
    _emit(success_envelope(result.value))


def _emit(value: BaseModel) -> None:
    typer.echo(
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    )
