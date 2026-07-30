"""Loopback GUI research command/query surface without a runtime composition."""

from __future__ import annotations

import json
import re
import secrets
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from time import monotonic
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind, Role
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    StructuredError,
    Success,
)
from stonks_agent.domain.gui_research import (
    GuiResearchCommand,
    GuiResearchEvidenceView,
    GuiResearchHistoryView,
    GuiResearchRunRef,
    GuiResearchRunView,
)
from stonks_agent.domain.latest_market_data import BarInterval
from stonks_agent.domain.redaction import redact
from stonks_agent.domain.research_run import CanonicalRunEvent
from stonks_agent.entrypoints.api.envelope import (
    ErrorEnvelope,
    SuccessEnvelope,
    error_envelope,
    success_envelope,
)
from stonks_agent.entrypoints.api.gui_mutation import validate_gui_mutation
from stonks_agent.ports.gui_research import GuiResearchFacade

MAX_RESEARCH_EVENTS = 500
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")
_TERMINAL_EVENTS = frozenset(
    {
        "research.cancelled",
        "research.degraded",
        "research.failed",
        "research.succeeded",
    }
)
_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorEnvelope} for status in (400, 403, 404, 409, 429, 500, 503)
}
_SSE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "description": "Canonical research run events",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    },
    **_ERROR_RESPONSES,
}
_RESEARCH_PATHS = (
    "/api/v1/research/runs",
    "/api/v1/research/runs/{run_id}",
    "/api/v1/research/runs/{run_id}/evidence",
    "/api/v1/research/runs/{run_id}/events",
)
_HISTORY_LIMIT = 10
_MAX_HISTORY_LIMIT = 20
_START_LIMIT = 3
_START_WINDOW_SECONDS = 60.0


class CreateGuiResearchRunBody(BaseModel):
    """The browser cannot choose owner, account, model, mode, target, or order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,15}$")
    interval: BarInterval
    profile: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class GuiResearchCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["ready", "unavailable"]
    detail: str = Field(min_length=1, max_length=256)
    allowed_profiles: tuple[str, ...] = Field(min_length=1, max_length=16)
    default_profile: str = Field(min_length=1, max_length=128)
    intent_token: str = Field(min_length=32, max_length=256)


@dataclass(frozen=True, slots=True)
class GuiResearchApiOptions:
    """Server-owned local authority and one process-memory browser intent."""

    account_id: str = "paper-local"
    allowed_profiles: tuple[str, ...] = ("balanced/1",)
    default_profile: str = "balanced/1"
    intent_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def __post_init__(self) -> None:
        if (
            not self.account_id
            or len(self.account_id) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}", self.account_id)
            is None
        ):
            raise ValueError("GUI research account is invalid")
        if (
            not 1 <= len(self.allowed_profiles) <= 16
            or len(set(self.allowed_profiles)) != len(self.allowed_profiles)
            or any(
                _PROFILE.fullmatch(profile) is None for profile in self.allowed_profiles
            )
            or self.default_profile not in self.allowed_profiles
        ):
            raise ValueError("GUI research profiles are invalid")
        if _TOKEN.fullmatch(self.intent_token) is None:
            raise ValueError("GUI research intent token is invalid")

    @property
    def principal(self) -> LocalPrincipal:
        return LocalPrincipal(
            subject="local-console-research",
            roles=frozenset({Role.RESEARCHER}),
            targets=frozenset(
                {
                    AccessTarget(
                        kind=ResourceKind.ACCOUNT,
                        identifier=self.account_id,
                    )
                }
            ),
        )


def install_gui_research_routes(
    app: FastAPI,
    facade: GuiResearchFacade | None,
    *,
    options: GuiResearchApiOptions,
    clock: Callable[[], datetime] | None = None,
    model_ready: Callable[[], bool] | None = None,
) -> None:
    """Install a stable surface; an absent facade returns typed 503."""

    if facade is not None and not isinstance(facade, GuiResearchFacade):
        raise TypeError("research must implement GuiResearchFacade")
    selected_clock = clock or utc_now
    app.add_api_route(
        "/api/v1/research/runs",
        _CreateEndpoint(facade, options, selected_clock, model_ready),
        methods=["POST"],
        status_code=202,
        response_model=SuccessEnvelope[GuiResearchRunRef],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/v1/research/runs",
        _ListEndpoint(facade, options),
        methods=["GET"],
        response_model=SuccessEnvelope[GuiResearchHistoryView],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/v1/research/runs/{run_id}",
        _DetailEndpoint(facade, options),
        methods=["GET"],
        response_model=SuccessEnvelope[GuiResearchRunView],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/v1/research/runs/{run_id}/evidence",
        _EvidenceEndpoint(facade, options),
        methods=["GET"],
        response_model=SuccessEnvelope[GuiResearchEvidenceView],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        "/api/v1/research/runs/{run_id}/events",
        _EventsEndpoint(facade, options),
        methods=["GET"],
        response_model=None,
        responses=_SSE_RESPONSES,
    )
    _install_exact_openapi(app)


def research_capability(
    facade: GuiResearchFacade | None,
    options: GuiResearchApiOptions,
) -> GuiResearchCapability:
    ready = facade is not None
    return GuiResearchCapability(
        state="ready" if ready else "unavailable",
        detail=(
            "Research API contract is composed."
            if ready
            else "Research workflow runtime is not composed in this process."
        ),
        allowed_profiles=options.allowed_profiles,
        default_profile=options.default_profile,
        intent_token=options.intent_token,
    )


def _install_exact_openapi(app: FastAPI) -> None:
    """Match central runtime validation, which maps invalid input to 400."""

    original = app.openapi

    def exact_openapi() -> dict[str, Any]:
        document = original()
        paths = document.get("paths", {})
        if not isinstance(paths, dict):
            return document
        for path in _RESEARCH_PATHS:
            operations = paths.get(path, {})
            if not isinstance(operations, dict):
                continue
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses", {})
                if isinstance(responses, dict):
                    responses.pop("422", None)
        return document

    object.__setattr__(app, "openapi", exact_openapi)


class _CreateEndpoint:
    def __init__(
        self,
        facade: GuiResearchFacade | None,
        options: GuiResearchApiOptions,
        clock: Callable[[], datetime],
        model_ready: Callable[[], bool] | None,
    ) -> None:
        self._facade = facade
        self._options = options
        self._clock = clock
        self._model_ready = model_ready
        self._gate = _ResearchStartGate()

    def __call__(
        self,
        request: Request,
        body: CreateGuiResearchRunBody,
    ) -> JSONResponse:
        rejected = _validate_mutation_request(request, self._options.intent_token)
        if rejected is not None:
            return _error_response(rejected)
        if body.profile not in self._options.allowed_profiles:
            return _error_response(_invalid("Research profile is not allowed"))
        if self._facade is None:
            return _error_response(_unavailable())
        if self._model_ready is not None:
            try:
                ready = self._model_ready()
            except Exception:
                ready = False
            if not ready:
                return _error_response(
                    Failure(
                        StructuredError(
                            code=ErrorCode.DATA_UNAVAILABLE,
                            message="A verified model connection is required",
                        )
                    )
                )
        gate_error = self._gate.begin(self._active_run)
        if gate_error is not None:
            response = _error_response(
                Failure(
                    StructuredError(
                        code=gate_error,
                        message=(
                            "A research run is already active"
                            if gate_error is ErrorCode.CONFLICT
                            else "Research start rate limit exceeded"
                        ),
                    )
                )
            )
            if gate_error is ErrorCode.RATE_LIMITED:
                response.headers["Retry-After"] = "60"
            return response
        try:
            command = GuiResearchCommand(
                symbol=body.symbol,
                interval=body.interval,
                profile=body.profile,
                account_id=self._options.account_id,
                requested_at=self._clock(),
            )
            result: object = self._facade.submit(self._options.principal, command)
        except (TypeError, ValueError, ValidationError):
            self._gate.cancel_start()
            return _error_response(_invalid("Research request is invalid"))
        except Exception:
            self._gate.cancel_start()
            return _error_response(_internal())
        if isinstance(result, Failure):
            self._gate.cancel_start()
            return _error_response(result)
        if not isinstance(result, Success) or not isinstance(
            result.value, GuiResearchRunRef
        ):
            self._gate.cancel_start()
            return _error_response(_internal())
        self._gate.accept(result.value.run_id)
        return _success(result.value, status=202)

    def _active_run(self, run_id: UUID) -> bool:
        if self._facade is None:
            return True
        try:
            result = self._facade.read(self._options.principal, run_id)
        except Exception:
            return True
        if not isinstance(result, Success) or not isinstance(
            result.value,
            GuiResearchRunView,
        ):
            return True
        return result.value.status in {"queued", "running"}


class _ResearchStartGate:
    """One active expensive start and three accepted starts per local minute."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._starts: deque[float] = deque(maxlen=_START_LIMIT)
        self._active_run_id: UUID | None = None
        self._starting = False

    def begin(
        self,
        is_active: Callable[[UUID], bool],
    ) -> ErrorCode | None:
        now = monotonic()
        with self._lock:
            while self._starts and now - self._starts[0] >= _START_WINDOW_SECONDS:
                self._starts.popleft()
            if self._starting:
                return ErrorCode.CONFLICT
            if self._active_run_id is not None:
                if is_active(self._active_run_id):
                    return ErrorCode.CONFLICT
                self._active_run_id = None
            if len(self._starts) >= _START_LIMIT:
                return ErrorCode.RATE_LIMITED
            self._starting = True
            self._starts.append(now)
            return None

    def accept(self, run_id: UUID) -> None:
        with self._lock:
            self._active_run_id = run_id
            self._starting = False

    def cancel_start(self) -> None:
        with self._lock:
            self._starting = False


class _DetailEndpoint:
    def __init__(
        self,
        facade: GuiResearchFacade | None,
        options: GuiResearchApiOptions,
    ) -> None:
        self._facade = facade
        self._options = options

    def __call__(self, request: Request, run_id: UUID) -> JSONResponse:
        if tuple(request.query_params):
            return _error_response(_invalid("Research query is invalid"))
        if self._facade is None:
            return _error_response(_unavailable())
        try:
            result: object = self._facade.read(self._options.principal, run_id)
        except Exception:
            return _error_response(_internal())
        if isinstance(result, Failure):
            return _error_response(result)
        if (
            not isinstance(result, Success)
            or not isinstance(result.value, GuiResearchRunView)
            or result.value.run_id != run_id
        ):
            return _error_response(_conflict())
        return _success(result.value)


class _ListEndpoint:
    def __init__(
        self,
        facade: GuiResearchFacade | None,
        options: GuiResearchApiOptions,
    ) -> None:
        self._facade = facade
        self._options = options

    def __call__(self, request: Request) -> JSONResponse:
        parsed = _history_query(request)
        if isinstance(parsed, Failure):
            return _error_response(parsed)
        if self._facade is None:
            return _error_response(_unavailable())
        try:
            result: object = self._facade.recent(
                self._options.principal,
                limit=parsed.value,
            )
        except Exception:
            return _error_response(_internal())
        if not isinstance(result, Success) or not isinstance(
            result.value, GuiResearchHistoryView
        ):
            return _error_response(
                result if isinstance(result, Failure) else _internal()
            )
        return _success(result.value)


class _EvidenceEndpoint:
    def __init__(
        self,
        facade: GuiResearchFacade | None,
        options: GuiResearchApiOptions,
    ) -> None:
        self._facade = facade
        self._options = options

    def __call__(self, request: Request, run_id: UUID) -> JSONResponse:
        if tuple(request.query_params):
            return _error_response(_invalid("Research evidence query is invalid"))
        if self._facade is None:
            return _error_response(_unavailable())
        try:
            result: object = self._facade.evidence(
                self._options.principal,
                run_id,
            )
        except Exception:
            return _error_response(_internal())
        if isinstance(result, Failure):
            return _error_response(result)
        if (
            not isinstance(result, Success)
            or not isinstance(result.value, GuiResearchEvidenceView)
            or result.value.run_id != run_id
        ):
            return _error_response(_conflict())
        return _success(result.value)


class _EventsEndpoint:
    def __init__(
        self,
        facade: GuiResearchFacade | None,
        options: GuiResearchApiOptions,
    ) -> None:
        self._facade = facade
        self._options = options

    def __call__(
        self,
        request: Request,
        run_id: UUID,
    ) -> JSONResponse | StreamingResponse:
        parsed = _event_query(request)
        if isinstance(parsed, Failure):
            return _error_response(parsed)
        if self._facade is None:
            return _error_response(_unavailable())
        after_sequence, limit = parsed.value
        try:
            result: object = self._facade.events(
                self._options.principal,
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except Exception:
            return _error_response(_internal())
        if isinstance(result, Failure):
            return _error_response(result)
        if not isinstance(result, Success) or not _valid_events(
            result.value,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        ):
            return _error_response(_conflict())
        return StreamingResponse(
            _sse(result.value),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )


def _validate_mutation_request(
    request: Request,
    intent_token: str,
) -> Failure | None:
    return validate_gui_mutation(
        request,
        intent_token,
        query_error="Research request query is invalid",
    )


def _event_query(request: Request) -> Success[tuple[int, int]] | Failure:
    parameters = request.query_params
    if any(name != "limit" for name in parameters):
        return _invalid("Research event query is invalid")
    limits = parameters.getlist("limit")
    event_ids = request.headers.getlist("last-event-id")
    if len(limits) > 1 or len(event_ids) > 1:
        return _invalid("Research event query is invalid")
    try:
        limit = int(limits[0]) if limits else 100
        after_sequence = int(event_ids[0]) if event_ids else 0
    except ValueError:
        return _invalid("Research event query is invalid")
    if not 1 <= limit <= MAX_RESEARCH_EVENTS or after_sequence < 0:
        return _invalid("Research event query is invalid")
    return Success((after_sequence, limit))


def _history_query(request: Request) -> Success[int] | Failure:
    parameters = request.query_params
    if any(name != "limit" for name in parameters):
        return _invalid("Research history query is invalid")
    values = parameters.getlist("limit")
    if len(values) > 1:
        return _invalid("Research history query is invalid")
    if not values:
        return Success(_HISTORY_LIMIT)
    try:
        limit = int(values[0])
    except ValueError:
        return _invalid("Research history limit is invalid")
    if not 1 <= limit <= _MAX_HISTORY_LIMIT:
        return _invalid("Research history limit is invalid")
    return Success(limit)


def _valid_events(
    events: object,
    *,
    run_id: UUID,
    after_sequence: int,
    limit: int,
) -> bool:
    if not isinstance(events, tuple) or len(events) > limit:
        return False
    previous = after_sequence
    for event in events:
        if (
            not isinstance(event, CanonicalRunEvent)
            or event.run_id != run_id
            or event.sequence <= previous
        ):
            return False
        previous = event.sequence
    return True


def _sse(events: tuple[CanonicalRunEvent, ...]) -> Iterator[str]:
    for event in events:
        projection = event.model_dump(mode="json")
        projection["payload"] = cast(dict[str, object], redact(event.payload))
        envelope = success_envelope(projection)
        data = json.dumps(
            envelope.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
        if event.event_type in _TERMINAL_EVENTS:
            return


def _success(value: BaseModel, *, status: int = 200) -> JSONResponse:
    envelope = success_envelope(value, status=status)
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def _error_response(result: Failure) -> JSONResponse:
    envelope = error_envelope(result.error)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def _invalid(message: str) -> Failure:
    return _failure(ErrorCode.INVALID_INPUT, message)


def _forbidden(message: str) -> Failure:
    return _failure(ErrorCode.FORBIDDEN, message)


def _unavailable() -> Failure:
    return _failure(
        ErrorCode.DATA_UNAVAILABLE,
        "Research workflow runtime is not composed",
    )


def _conflict() -> Failure:
    return _failure(ErrorCode.CONFLICT, "Research projection is inconsistent")


def _internal() -> Failure:
    return _failure(ErrorCode.INTERNAL_ERROR, "Research request failed")
