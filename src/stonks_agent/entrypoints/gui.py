"""One-command local GUI backed by the real isolated OpenBB sidecar."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from types import MappingProxyType
from typing import Annotated

import httpx
import typer
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretBytes

from stonks_agent.adapters.auth.service_credentials import (
    ReceiverAudience,
    RS256ServiceCredentialProvider,
    ServiceIssuerSettings,
)
from stonks_agent.adapters.market_data.openbb_latest import (
    OpenBBLatestMarketDataSource,
)
from stonks_agent.adapters.market_data.openbb_rest import OPENBB_ORIGIN
from stonks_agent.adapters.postgres.gui_research import (
    PostgresGuiResearchFacade,
)
from stonks_agent.composition.market_calendars import (
    verified_market_freshness_policy,
)
from stonks_agent.composition.model_settings import (
    SessionModelSettings,
    build_model_connection_tester,
)
from stonks_agent.composition.runtime import build_local_runtime
from stonks_agent.composition.worker import build_worker_composition
from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.entrypoints.api.envelope import (
    error_envelope,
    unexpected_error_envelope,
)
from stonks_agent.entrypoints.api.gui import (
    PaperCapability,
    ServiceStatus,
    create_gui_app,
)
from stonks_agent.entrypoints.api.gui_research import GuiResearchApiOptions
from stonks_agent.entrypoints.gui_paper import (
    PaperStartupError,
    bootstrap_account,
    migrate,
    open_engine,
    paper_reader,
    prepare_paper_runtime,
)
from stonks_agent.entrypoints.kronos_gui import KronosSidecarManager
from stonks_agent.ports.gui_model_settings import GuiModelSettingsPort
from stonks_agent.ports.service_credentials import (
    ServiceCredentialProvider,
    ServiceReceiver,
)

app = typer.Typer(add_completion=False, no_args_is_help=True)
_ISSUER = "https://identity.stonks-gui.invalid"
_SUBJECT = "service:stonks-gui-core"
_CLIENT_ID = "stonks-gui-core"
_KEY_ID = "stonks-gui-ephemeral"
_PROJECT_NAME = "stonks-gui-openbb"
_DB_PROJECT_NAME = "stonks-gui-postgres"
_SAFE_AMBIENT_ENV = frozenset(
    {
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMPOSE_PARALLEL_LIMIT",
        "COMPOSE_PROGRESS",
        "COMSPEC",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "XDG_RUNTIME_DIR",
    }
)
type Runner = Callable[..., subprocess.CompletedProcess[str]]


class GuiStartupError(RuntimeError):
    """Public-safe local GUI startup precondition failure."""


@dataclass(frozen=True, slots=True)
class EphemeralOpenBBRuntime:
    credentials: ServiceCredentialProvider
    environment: Mapping[str, str]
    jwks_file: Path


class OpenBBSidecarManager:
    """Own one exact local Compose project without inheriting secrets."""

    def __init__(
        self,
        *,
        root: Path,
        environment: Mapping[str, str],
        ambient: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
        vcs_ref: str | None = None,
    ) -> None:
        self._root = root.resolve()
        self._environment = dict(environment)
        self._ambient = dict(os.environ if ambient is None else ambient)
        self._runner = runner
        self._vcs_ref = vcs_ref or _git_revision(self._root)
        if re.fullmatch(r"[0-9a-f]{40}", self._vcs_ref) is None:
            raise RuntimeError("OpenBB build revision is invalid")

    def start(self) -> None:
        self._run(
            (
                *self._prefix(),
                "build",
                "--build-arg",
                f"VCS_REF={self._vcs_ref}",
                "openbb",
            ),
            timeout=1_800,
        )
        try:
            self._run(
                (
                    *self._prefix(),
                    "up",
                    "--detach",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "240",
                    "openbb",
                ),
                timeout=300,
            )
        except Exception:
            with suppress(Exception):
                self.stop()
            raise

    def stop(self) -> None:
        self._run(
            (*self._prefix(), "down", "--remove-orphans"),
            timeout=120,
        )

    def _prefix(self) -> tuple[str, ...]:
        return _compose_prefix(self._root, _PROJECT_NAME, "compose.openbb.yaml")

    def _run(self, command: Sequence[str], *, timeout: int) -> None:
        result = self._runner(
            tuple(command),
            cwd=self._root,
            env=self._safe_environment(),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("OpenBB sidecar lifecycle failed")

    def _safe_environment(self) -> dict[str, str]:
        safe = {
            name: value
            for name, value in self._ambient.items()
            if name.upper() in _SAFE_AMBIENT_ENV and value
        }
        safe.update(self._environment)
        return safe


class PaperDatabaseManager(OpenBBSidecarManager):
    """Own the optional local PostgreSQL project; data survives restarts."""

    def start(self) -> None:
        self._run(
            (
                *self._prefix(),
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "180",
                "postgres",
            ),
            timeout=300,
        )

    def stop(self) -> None:
        # No --volumes: stopping the console must never drop the paper ledger.
        self._run((*self._prefix(), "down"), timeout=120)

    def _prefix(self) -> tuple[str, ...]:
        return _compose_prefix(self._root, _DB_PROJECT_NAME, "compose.gui.yaml")


def _compose_prefix(root: Path, project: str, manifest: str) -> tuple[str, ...]:
    compose = root / "infra" / manifest
    if not compose.is_file() or compose.is_symlink():
        raise RuntimeError("Compose manifest is unavailable")
    return ("docker", "compose", "-p", project, "-f", str(compose))


@app.callback()
def main() -> None:
    """啟動使用真實 OpenBB/yfinance 日資料的 local Web GUI。"""


@app.command("serve")
def serve(
    port: Annotated[
        int,
        typer.Option(min=1_024, max=65_535, help="Loopback GUI port"),
    ] = 8_787,
    open_browser: Annotated[
        bool,
        typer.Option(help="GUI ready 後開啟預設瀏覽器"),
    ] = True,
    with_paper: Annotated[
        bool,
        typer.Option(help="另外啟動本機 PostgreSQL 並組合 paper 投資組合面板"),
    ] = False,
    with_research: Annotated[
        bool,
        typer.Option(
            help="啟動 live snapshot + LLM research worker; 同時啟用 paper 面板"
        ),
    ] = False,
    database_port: Annotated[
        int,
        typer.Option(min=1_024, max=65_535, help="Loopback paper 資料庫連接埠"),
    ] = 55_433,
    kronos_port: Annotated[
        int,
        typer.Option(min=1_024, max=65_535, help="Loopback Kronos CPU worker 連接埠"),
    ] = 17_200,
) -> None:
    """Build/start OpenBB, serve the GUI, and clean up on shutdown."""

    runtime_root: Path | None = None
    started = False
    manager: OpenBBSidecarManager | None = None
    paper: _PaperComposition | None = None
    research: _ResearchComposition | None = None
    kronos: KronosSidecarManager | None = None
    try:
        root = _project_root(Path.cwd())
        runtime_root = root / ".data" / "gui"
        runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="openbb-",
            dir=runtime_root,
        ) as temporary:
            runtime = prepare_ephemeral_openbb_runtime(Path(temporary))
            manager = OpenBBSidecarManager(
                root=root,
                environment=runtime.environment,
            )
            manager.start()
            started = True
            compose_paper = with_paper or with_research
            if with_research:
                try:
                    kronos = KronosSidecarManager(
                        root=root,
                        environment=runtime.environment,
                        model_root=root / ".data" / "models" / "kronos",
                        port=kronos_port,
                    )
                    kronos.start()
                except Exception:
                    raise GuiStartupError(
                        "Kronos CPU worker 啟動或模型完整性驗證失敗"
                    ) from None
            paper = (
                _compose_paper(root, runtime_root, port=database_port)
                if compose_paper
                else None
            )
            research = (
                _compose_research(
                    root,
                    paper,
                    credentials=runtime.credentials,
                    kronos_origin=f"http://127.0.0.1:{kronos_port}",
                )
                if with_research and paper is not None
                else None
            )
            with httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(15.0),
            ) as client:
                application = create_gui_app(
                    OpenBBLatestMarketDataSource(
                        client=client,
                        credentials=runtime.credentials,
                    ),
                    paper=paper.reader if paper is not None else None,
                    research=research.facade if research is not None else None,
                    model_settings=(
                        research.model_settings if research is not None else None
                    ),
                    research_api=(
                        GuiResearchApiOptions(account_id=paper.account_id)
                        if research is not None and paper is not None
                        else None
                    ),
                    services=_service_reader(
                        client,
                        paper=paper,
                        research=research,
                        model_settings=(
                            research.model_settings if research is not None else None
                        ),
                        kronos_port=kronos_port,
                        kronos_composed=kronos is not None,
                    ),
                    market_freshness=verified_market_freshness_policy(),
                )
                if open_browser:
                    Timer(
                        1.0,
                        _open_browser,
                        args=(f"http://127.0.0.1:{port}",),
                    ).start()
                uvicorn.run(
                    application,
                    host="127.0.0.1",
                    port=port,
                    workers=1,
                    proxy_headers=False,
                    forwarded_allow_ips="",
                    server_header=False,
                    date_header=False,
                    access_log=False,
                    backlog=64,
                    limit_concurrency=32,
                    timeout_keep_alive=5,
                    timeout_graceful_shutdown=15,
                )
    except (GuiStartupError, PaperStartupError) as error:
        envelope = error_envelope(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message=str(error),
            )
        )
        typer.echo(envelope.model_dump_json(), err=True)
        raise typer.Exit(code=1) from None
    except Exception as error:
        typer.echo(unexpected_error_envelope(error).model_dump_json(), err=True)
        raise typer.Exit(code=1) from None
    finally:
        if research is not None:
            with suppress(Exception):
                research.close()
        if paper is not None:
            with suppress(Exception):
                paper.close()
        if kronos is not None:
            with suppress(Exception):
                kronos.stop()
        if started and manager is not None:
            with suppress(Exception):
                manager.stop()
        if runtime_root is not None:
            _remove_empty_runtime_root(runtime_root)


@dataclass(frozen=True, slots=True)
class _PaperComposition:
    account_id: str
    database_url: str
    reader: Callable[[], PaperCapability]
    healthy: Callable[[], bool]
    close: Callable[[], None]


def _compose_paper(
    root: Path,
    runtime_root: Path,
    *,
    port: int,
) -> _PaperComposition:
    """Start PostgreSQL, migrate once, and expose read-only projections."""

    runtime = prepare_paper_runtime(runtime_root, port=port)
    database = PaperDatabaseManager(root=root, environment=runtime.environment)
    database.start()
    try:
        migrate(runtime.database_url, root=root)
        engine = open_engine(runtime.database_url)
        bootstrap_account(engine, account_id=runtime.account_id)
    except Exception:
        with suppress(Exception):
            database.stop()
        raise PaperStartupError(
            "本機 paper 資料庫初始化失敗。GUI 不會以假資料頂替"
        ) from None

    def close() -> None:
        with suppress(Exception):
            engine.dispose()
        database.stop()

    def healthy() -> bool:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            return False

    return _PaperComposition(
        account_id=runtime.account_id,
        database_url=runtime.database_url,
        reader=paper_reader(engine, account_id=runtime.account_id),
        healthy=healthy,
        close=close,
    )


@dataclass(frozen=True, slots=True)
class _ResearchComposition:
    facade: PostgresGuiResearchFacade
    model_settings: SessionModelSettings
    healthy: Callable[[], bool]
    close: Callable[[], None]


class _ResearchWorkerSupervisor:
    def __init__(self, facade: PostgresGuiResearchFacade) -> None:
        self._facade = facade
        self._stop = Event()
        self._thread = Thread(
            target=self._run,
            name="stonks-local-research-worker",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("Research worker did not stop")

    def healthy(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            result = self._facade.worker_once()
            if getattr(result, "value", False):
                continue


def _compose_research(
    root: Path,
    paper: _PaperComposition,
    *,
    credentials: ServiceCredentialProvider,
    kronos_origin: str,
) -> _ResearchComposition:
    local_runtime = build_local_runtime(
        database_url=paper.database_url,
        artifact_root=root / ".data" / "artifacts",
    )
    try:
        environment = dict(os.environ) | {"STONKS_ENVIRONMENT": "local"}
        model_settings = SessionModelSettings(
            environment,
            tester=build_model_connection_tester(),
        )
        if model_settings.view().source == "environment":
            model_settings.verify_environment()
        worker = build_worker_composition(
            local_runtime,
            environment=environment,
            root=root,
            credentials=credentials,
            kronos_origin=kronos_origin,
            model_environment=model_settings.environment_snapshot,
        )
        facade = PostgresGuiResearchFacade(
            runtime=local_runtime,
            queue=worker.queue,
            handlers=worker.handlers,
            worker_lock=Lock(),
        )
        supervisor = _ResearchWorkerSupervisor(facade)
        supervisor.start()
    except Exception:
        local_runtime.close()
        raise

    def close() -> None:
        supervisor.close()
        local_runtime.close()

    return _ResearchComposition(
        facade=facade,
        model_settings=model_settings,
        healthy=supervisor.healthy,
        close=close,
    )


def _service_reader(
    client: httpx.Client,
    *,
    paper: _PaperComposition | None,
    research: _ResearchComposition | None,
    model_settings: GuiModelSettingsPort | None,
    kronos_port: int,
    kronos_composed: bool,
) -> Callable[[], Sequence[ServiceStatus]]:
    """Probe current liveness without exposing origins or runtime identifiers."""

    def read() -> Sequence[ServiceStatus]:
        return (
            _service_status(
                "openbb",
                "yfinance sidecar",
                _http_healthy(client, f"{OPENBB_ORIGIN}/healthz"),
            ),
            (
                _service_status(
                    "postgres",
                    "canonical paper store",
                    paper.healthy(),
                )
                if paper is not None
                else ServiceStatus(
                    name="postgres",
                    detail="未組合",
                    state="absent",
                )
            ),
            _model_service_status(model_settings),
            (
                _service_status(
                    "kronos",
                    "actual CPU forecast worker",
                    _kronos_ready(client, kronos_port),
                )
                if kronos_composed
                else ServiceStatus(
                    name="kronos",
                    detail="未組合",
                    state="absent",
                )
            ),
            (
                _service_status(
                    "research",
                    "durable research worker",
                    research.healthy(),
                )
                if research is not None
                else ServiceStatus(
                    name="research",
                    detail="未組合",
                    state="absent",
                )
            ),
        )

    return read


def _model_service_status(
    settings: GuiModelSettingsPort | None,
) -> ServiceStatus:
    if settings is None:
        return ServiceStatus(name="llm", detail="未組合", state="absent")
    try:
        view = settings.view()
    except Exception:
        return ServiceStatus(name="llm", detail="狀態讀取失敗", state="failed")
    if view.state != "configured":
        return ServiceStatus(name="llm", detail="尚未設定", state="absent")
    if not view.verified:
        return ServiceStatus(
            name="llm",
            detail="已設定, 尚未於本次 session 驗證",
            state="failed",
        )
    model_id = view.config.model_id if view.config is not None else "model"
    return ServiceStatus(
        name="llm",
        detail=f"structured completion ready · {model_id}"[:128],
        state="ready",
    )


def _service_status(name: str, detail: str, healthy: bool) -> ServiceStatus:
    return ServiceStatus(
        name=name,
        detail=detail if healthy else f"{detail} 無回應",
        state="ready" if healthy else "failed",
    )


def _http_healthy(client: httpx.Client, url: str) -> bool:
    try:
        response = client.get(url, timeout=1.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _kronos_ready(client: httpx.Client, port: int) -> bool:
    try:
        response = client.get(
            f"http://127.0.0.1:{port}/readyz",
            timeout=1.0,
        )
        payload = response.json()
        return (
            response.status_code == 200
            and isinstance(payload, dict)
            and payload.get("success") is True
            and payload.get("data") == {"ready": True}
        )
    except (httpx.HTTPError, ValueError):
        return False


def prepare_ephemeral_openbb_runtime(directory: Path) -> EphemeralOpenBBRuntime:
    """Generate an in-memory signer and persist only its public JWKS."""

    target = directory.resolve()
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise RuntimeError("GUI authentication directory is invalid")
    target.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    if not isinstance(jwk, dict):
        raise RuntimeError("GUI public identity is invalid")
    public_jwk = {
        **jwk,
        "kid": _KEY_ID,
        "alg": "RS256",
        "use": "sig",
        "key_ops": ["verify"],
    }
    jwks_file = target / "openbb-service-jwks.json"
    jwks_file.write_text(
        json.dumps({"keys": [public_jwk]}, sort_keys=True),
        encoding="utf-8",
    )
    settings = _issuer_settings()
    credentials = RS256ServiceCredentialProvider(
        settings=settings,
        private_key_pem=SecretBytes(private_pem),
    )
    environment = MappingProxyType(
        {
            "STONKS_SERVICE_OIDC_ISSUER": _ISSUER,
            "STONKS_SERVICE_OIDC_AUDIENCE": settings.audience_for(
                ServiceReceiver.OPENBB
            ),
            "STONKS_SERVICE_OIDC_CORE_SUBJECT": _SUBJECT,
            "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": _CLIENT_ID,
            "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
            "STONKS_SERVICE_OIDC_JWKS_HOST_FILE": jwks_file.as_posix(),
        }
    )
    return EphemeralOpenBBRuntime(
        credentials=credentials,
        environment=environment,
        jwks_file=jwks_file,
    )


def _issuer_settings() -> ServiceIssuerSettings:
    return ServiceIssuerSettings(
        issuer=_ISSUER,
        subject=_SUBJECT,
        client_id=_CLIENT_ID,
        key_id=_KEY_ID,
        audiences=tuple(
            ReceiverAudience(
                receiver=receiver,
                audience=f"stonks-gui-{receiver.value.replace('_', '-')}",
            )
            for receiver in ServiceReceiver
        ),
        max_token_lifetime_seconds=120,
    )


def _project_root(candidate: Path) -> Path:
    root = candidate.resolve()
    if (
        not (root / "pyproject.toml").is_file()
        or not (root / "infra" / "compose.openbb.yaml").is_file()
    ):
        raise GuiStartupError("GUI requires a Stonks Agent source checkout")
    return root


def _git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        revision = result.stdout.strip().lower()
        if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError
        return revision
    except (OSError, subprocess.SubprocessError, ValueError):
        raise RuntimeError("Git revision is unavailable") from None


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:
        return


def _remove_empty_runtime_root(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            parent = path.parent
            if parent.name == ".data" and parent.is_dir() and not any(parent.iterdir()):
                shutil.rmtree(parent)
    except OSError:
        return


if __name__ == "__main__":
    app()
