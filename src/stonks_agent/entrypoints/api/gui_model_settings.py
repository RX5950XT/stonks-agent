"""Secret-safe loopback model settings API with no durable authority."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    StructuredError,
    Success,
)
from stonks_agent.domain.gui_model_settings import (
    ConfigureGuiModelSettings,
    GuiModelSettingsView,
)
from stonks_agent.entrypoints.api.envelope import (
    ErrorEnvelope,
    SuccessEnvelope,
    error_envelope,
    success_envelope,
)
from stonks_agent.entrypoints.api.gui_mutation import validate_gui_mutation
from stonks_agent.ports.gui_model_settings import GuiModelSettingsPort

_PATH = "/api/v1/settings/llm"
_UPDATE_LIMIT = 3
_UPDATE_WINDOW_SECONDS = 60.0
_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorEnvelope} for status in (400, 403, 409, 429, 500, 503)
}


class ClearGuiModelSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class GuiModelSettingsApiOptions:
    intent_token: str

    def __post_init__(self) -> None:
        if not 32 <= len(self.intent_token) <= 256:
            raise ValueError("GUI model settings intent is invalid")


def install_gui_model_settings_routes(
    app: FastAPI,
    settings: GuiModelSettingsPort | None,
    *,
    options: GuiModelSettingsApiOptions,
) -> None:
    if settings is not None and not isinstance(settings, GuiModelSettingsPort):
        raise TypeError("model_settings must implement GuiModelSettingsPort")
    gate = _SettingsUpdateGate()
    app.add_api_route(
        _PATH,
        _ReadEndpoint(settings),
        methods=["GET"],
        response_model=SuccessEnvelope[GuiModelSettingsView],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        _PATH,
        _ConfigureEndpoint(settings, options, gate),
        methods=["PUT"],
        response_model=SuccessEnvelope[GuiModelSettingsView],
        responses=_ERROR_RESPONSES,
    )
    app.add_api_route(
        _PATH,
        _ClearEndpoint(settings, options),
        methods=["DELETE"],
        response_model=SuccessEnvelope[GuiModelSettingsView],
        responses=_ERROR_RESPONSES,
    )
    _install_exact_openapi(app)


def model_settings_capability(
    settings: GuiModelSettingsPort | None,
) -> GuiModelSettingsView:
    if settings is None:
        return _unavailable_view()
    try:
        view = settings.view()
        if not isinstance(view, GuiModelSettingsView):
            raise TypeError
        return view
    except Exception:
        return _unavailable_view()


class _ReadEndpoint:
    def __init__(self, settings: GuiModelSettingsPort | None) -> None:
        self._settings = settings

    def __call__(self, request: Request) -> JSONResponse:
        if tuple(request.query_params):
            return _error_response(_invalid("Model settings query is invalid"))
        if self._settings is None:
            return _error_response(_unavailable())
        try:
            view: object = self._settings.view()
        except Exception:
            return _error_response(_internal())
        if not isinstance(view, GuiModelSettingsView):
            return _error_response(_conflict())
        return _success(view)


class _ConfigureEndpoint:
    def __init__(
        self,
        settings: GuiModelSettingsPort | None,
        options: GuiModelSettingsApiOptions,
        gate: _SettingsUpdateGate,
    ) -> None:
        self._settings = settings
        self._options = options
        self._gate = gate

    def __call__(
        self,
        request: Request,
        body: ConfigureGuiModelSettings,
    ) -> JSONResponse:
        invalid = validate_gui_mutation(
            request,
            self._options.intent_token,
            query_error="Model settings query is invalid",
        )
        if invalid is not None:
            return _error_response(invalid)
        if self._settings is None:
            return _error_response(_unavailable())
        admitted = self._gate.start()
        if admitted is not None:
            return _error_response(admitted, retry_after=60)
        try:
            result: object = self._settings.configure(body)
        except Exception:
            return _error_response(_internal())
        finally:
            self._gate.finish()
        if isinstance(result, Failure):
            return _error_response(result)
        if not isinstance(result, Success) or not isinstance(
            result.value,
            GuiModelSettingsView,
        ):
            return _error_response(_conflict())
        return _success(result.value)


class _ClearEndpoint:
    def __init__(
        self,
        settings: GuiModelSettingsPort | None,
        options: GuiModelSettingsApiOptions,
    ) -> None:
        self._settings = settings
        self._options = options

    def __call__(
        self,
        request: Request,
        body: ClearGuiModelSettingsBody,
    ) -> JSONResponse:
        del body
        invalid = validate_gui_mutation(
            request,
            self._options.intent_token,
            query_error="Model settings query is invalid",
        )
        if invalid is not None:
            return _error_response(invalid)
        if self._settings is None:
            return _error_response(_unavailable())
        try:
            result: object = self._settings.clear()
        except Exception:
            return _error_response(_internal())
        if isinstance(result, Failure):
            return _error_response(result)
        if not isinstance(result, Success) or not isinstance(
            result.value,
            GuiModelSettingsView,
        ):
            return _error_response(_conflict())
        return _success(result.value)


class _SettingsUpdateGate:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._clock = clock
        self._lock = Lock()
        self._active = False
        self._starts: deque[float] = deque()

    def start(self) -> Failure | None:
        now = self._clock()
        with self._lock:
            cutoff = now - _UPDATE_WINDOW_SECONDS
            while self._starts and self._starts[0] <= cutoff:
                self._starts.popleft()
            if self._active:
                return _failure(
                    ErrorCode.CONFLICT,
                    "Model connection test is already running",
                )
            if len(self._starts) >= _UPDATE_LIMIT:
                return _failure(
                    ErrorCode.RATE_LIMITED,
                    "Model connection test rate limit exceeded",
                )
            self._active = True
            self._starts.append(now)
        return None

    def finish(self) -> None:
        with self._lock:
            self._active = False


def _install_exact_openapi(app: FastAPI) -> None:
    original = app.openapi

    def exact_openapi() -> dict[str, Any]:
        document = original()
        paths = document.get("paths", {})
        operations = paths.get(_PATH, {}) if isinstance(paths, dict) else {}
        if isinstance(operations, dict):
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses", {})
                if isinstance(responses, dict):
                    responses.pop("422", None)
        return document

    object.__setattr__(app, "openapi", exact_openapi)


def _success(value: GuiModelSettingsView) -> JSONResponse:
    envelope = success_envelope(value)
    return JSONResponse(
        status_code=200,
        content=envelope.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def _error_response(
    result: Failure,
    *,
    retry_after: int | None = None,
) -> JSONResponse:
    envelope = error_envelope(result.error)
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=envelope.status,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _unavailable_view() -> GuiModelSettingsView:
    return GuiModelSettingsView(
        state="unavailable",
        source="none",
        detail="Model settings runtime is not composed.",
        api_key_configured=False,
        verified=False,
        generation=0,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def _invalid(message: str) -> Failure:
    return _failure(ErrorCode.INVALID_INPUT, message)


def _unavailable() -> Failure:
    return _failure(
        ErrorCode.DATA_UNAVAILABLE,
        "Model settings runtime is not composed",
    )


def _conflict() -> Failure:
    return _failure(ErrorCode.CONFLICT, "Model settings state is inconsistent")


def _internal() -> Failure:
    return _failure(ErrorCode.INTERNAL_ERROR, "Model settings request failed")
