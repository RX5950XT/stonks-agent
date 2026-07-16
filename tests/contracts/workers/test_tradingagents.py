from __future__ import annotations

import hashlib
import importlib
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stonks_service_auth import ServiceReceiver

ROOT = Path(__file__).resolve().parents[3]
WORKER = ROOT / "workers" / "tradingagents"
sys.path.insert(0, str(ROOT))

from fixtures.service_auth import (  # noqa: E402
    ExactServiceAuthenticator,
    authorization_headers,
)

from stonks_contracts.common import ModelUsage  # noqa: E402
from stonks_contracts.tradingagents import SignedEvidenceArtifact  # noqa: E402
from workers.tradingagents import runtime as runtime_module  # noqa: E402
from workers.tradingagents.adapter import (  # noqa: E402
    EvidenceCategory,
    ResolvedTradingAgentsRequest,
    RuntimeAnalysis,
    RuntimeTelemetry,
    ScopedEvidence,
    TradingAgentsRequest,
    TradingAgentsWorker,
    WorkerFailure,
    WorkerPolicy,
    WorkerProfile,
    WorkerSuccess,
    validate_worker_environment,
)
from workers.tradingagents.app import create_app  # noqa: E402
from workers.tradingagents.artifacts import FixedOriginArtifactResolver  # noqa: E402
from workers.tradingagents.runtime import (  # noqa: E402
    MODEL_PROXY_ORIGIN,
    PinnedTradingAgentsRuntime,
    _content_by_category,
    _extract_thesis,
    _install_canonical_facade,
    _normalize_recommendation,
    _runtime_config,
    _TelemetryCallback,
)

NOW = datetime(2026, 7, 12, 12, tzinfo=UTC)
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000301")
RUN_ID = UUID("00000000-0000-4000-8000-000000000302")
INSTRUMENT_ID = UUID("00000000-0000-4000-8000-000000000303")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000304")
JOB_ID = UUID("00000000-0000-4000-8000-000000000305")


def evidence(**overrides: object) -> SignedEvidenceArtifact:
    values: dict[str, object] = {
        "evidence_id": EVIDENCE_ID,
        "artifact_ref": f"sha256:{'a' * 64}",
        "available_at": NOW - timedelta(minutes=1),
        "category": EvidenceCategory.MARKET,
        "signed_url": (
            f"http://artifact-service:8080/v1/artifacts/{'a' * 64}"
            "?expires=1783857900&signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ),
        "expires_at": NOW + timedelta(minutes=5),
        "untrusted_content": True,
    }
    values.update(overrides)
    return SignedEvidenceArtifact.model_validate(values)


def analysis_request(**overrides: object) -> TradingAgentsRequest:
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "run_id": RUN_ID,
        "job_id": JOB_ID,
        "attempt_generation": 2,
        "attempt_nonce": "nonce-a",
        "profile": WorkerProfile.PAPER,
        "instrument_id": INSTRUMENT_ID,
        "symbol": "AAPL",
        "as_of": NOW,
        "horizon": "20 trading days",
        "allowed_evidence_ids": (EVIDENCE_ID,),
        "evidence": (evidence(),),
        "deadline": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return TradingAgentsRequest.model_validate(values)


def resolved_request(**overrides: object) -> ResolvedTradingAgentsRequest:
    values = analysis_request().model_dump(mode="python")
    for key in ("schema_version", "job_id", "attempt_generation", "attempt_nonce"):
        values.pop(key)
    values["evidence"] = (
        ScopedEvidence(
            evidence_id=EVIDENCE_ID,
            artifact_ref=f"sha256:{'a' * 64}",
            available_at=NOW - timedelta(minutes=1),
            category=EvidenceCategory.MARKET,
            content='{"close":"100.00"}',
            untrusted_content=True,
        ),
    )
    values.update(overrides)
    return ResolvedTradingAgentsRequest.model_validate(values)


class FakeArtifacts:
    def __init__(self, content: str = '{"close":"100.00"}') -> None:
        self.content = content

    def resolve(self, artifact: SignedEvidenceArtifact) -> str:
        assert artifact.evidence_id == EVIDENCE_ID
        return self.content


def policy(**overrides: object) -> WorkerPolicy:
    values: dict[str, object] = {
        "profile": WorkerProfile.PAPER,
        "selected_analysts": ("market", "fundamentals"),
        "max_evidence_bytes": 65_536,
        "network_egress": "deny",
        "worker_version": "tradingagents-worker/0.1.0",
        "upstream_commit": "01477f9afb7a47b849ed4c9259d3a9a4738d9fda",
    }
    values.update(overrides)
    return WorkerPolicy.model_validate(values)


class RecordingRuntime:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.requests: list[ResolvedTradingAgentsRequest] = []
        self.error = error

    def run(self, request: ResolvedTradingAgentsRequest) -> RuntimeAnalysis:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return RuntimeAnalysis(
            recommendation="Overweight",
            thesis="Canonical evidence supports a cautious positive research view.",
            telemetry=RuntimeTelemetry(
                model_usage=(
                    ModelUsage(
                        input_tokens=120,
                        output_tokens=30,
                        latency_ms=25,
                    ),
                ),
                tool_latency_ms=(25,),
                warnings=("upstream_risk_text_treated_as_opinion",),
                source_refs=(EVIDENCE_ID,),
            ),
        )


def test_success_returns_only_analysis_bundle_and_agent_opinion() -> None:
    runtime = RecordingRuntime()
    worker = TradingAgentsWorker(
        policy=policy(), runtime=runtime, artifacts=FakeArtifacts(), clock=lambda: NOW
    )

    result = worker.analyze(analysis_request())

    assert isinstance(result, WorkerSuccess)
    assert result.value.result.analysis_bundle.run_id == RUN_ID
    assert result.value.result.analysis_bundle.source_refs == (EVIDENCE_ID,)
    assert (
        result.value.result.analysis_bundle.worker_version
        == "tradingagents-worker/0.1.0"
    )
    assert result.value.result.agent_opinion.recommendation == "Overweight"
    assert result.value.result.agent_opinion.confidence == Decimal("0")
    assert result.value.result.agent_opinion.evidence_refs == (EVIDENCE_ID,)
    assert runtime.requests == [resolved_request()]
    payload = result.value.result.model_dump(mode="json")
    assert set(payload) == {"schema_version", "analysis_bundle", "agent_opinion"}
    assert not _forbidden_authority_keys(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_evidence_ids": (UUID(int=9),)},
        {"allowed_evidence_ids": (EVIDENCE_ID, EVIDENCE_ID)},
        {"evidence": (evidence(available_at=NOW + timedelta(seconds=1)),)},
        {"evidence": (evidence(), evidence())},
        {"symbol": "AAPL;curl attacker"},
        {"order": {"quantity": 100}},
    ],
)
def test_request_rejects_scope_future_duplicates_injection_and_order_fields(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        analysis_request(**overrides)


def test_profile_deadline_and_size_fail_before_runtime() -> None:
    runtime = RecordingRuntime()
    worker = TradingAgentsWorker(
        policy=policy(), runtime=runtime, artifacts=FakeArtifacts(), clock=lambda: NOW
    )

    mismatched = worker.analyze(analysis_request(profile=WorkerProfile.BACKTEST))
    expired = worker.analyze(analysis_request(deadline=NOW - timedelta(seconds=1)))
    small_policy = policy(max_evidence_bytes=1)
    oversized = TradingAgentsWorker(
        policy=small_policy,
        runtime=runtime,
        artifacts=FakeArtifacts(),
        clock=lambda: NOW,
    ).analyze(analysis_request())

    assert isinstance(mismatched, WorkerFailure)
    assert mismatched.error.code == "profile_mismatch"
    assert isinstance(expired, WorkerFailure)
    assert expired.error.code == "deadline_exceeded"
    assert isinstance(oversized, WorkerFailure)
    assert oversized.error.code == "evidence_too_large"
    assert runtime.requests == []


def test_runtime_exception_is_generic_and_never_leaks_secret() -> None:
    runtime = RecordingRuntime(error=RuntimeError("token=must-not-leak"))
    worker = TradingAgentsWorker(
        policy=policy(), runtime=runtime, artifacts=FakeArtifacts(), clock=lambda: NOW
    )

    result = worker.analyze(analysis_request())

    assert isinstance(result, WorkerFailure)
    assert result.error.code == "runtime_failed"
    assert "must-not-leak" not in result.model_dump_json()


def test_runtime_source_refs_must_remain_inside_request_scope() -> None:
    class BadRuntime:
        def run(self, request: ResolvedTradingAgentsRequest) -> RuntimeAnalysis:
            return RuntimeAnalysis(
                recommendation="Hold",
                thesis="Out of scope",
                telemetry=RuntimeTelemetry(source_refs=(UUID(int=99),)),
            )

    result = TradingAgentsWorker(
        policy=policy(),
        runtime=BadRuntime(),
        artifacts=FakeArtifacts(),
        clock=lambda: NOW,
    ).analyze(analysis_request())

    assert isinstance(result, WorkerFailure)
    assert result.error.code == "source_scope_exceeded"


def test_api_uses_standard_envelope_and_bounded_body() -> None:
    runtime = RecordingRuntime()
    request = analysis_request()
    app = create_app(
        worker=TradingAgentsWorker(
            policy=policy(),
            runtime=runtime,
            artifacts=FakeArtifacts(),
            clock=lambda: NOW,
        ),
        authenticator=ExactServiceAuthenticator.for_request(
            request,
            receiver=ServiceReceiver.TRADINGAGENTS,
        ),
        max_request_bytes=8_192,
    )

    with TestClient(app) as client:
        health = client.get("/healthz")
        success = client.post(
            "/v1/analyze",
            content=request.model_dump_json(),
            headers={**authorization_headers(), "content-type": "application/json"},
        )
        invalid = client.post(
            "/v1/analyze",
            content=b"not-json",
            headers={**authorization_headers(), "content-type": "application/json"},
        )
        oversized = client.post(
            "/v1/analyze",
            content=b"{" + b"x" * 8_192,
            headers={**authorization_headers(), "content-type": "application/json"},
        )
        hostile_lengths = tuple(
            client.post(
                "/v1/analyze",
                content=b"{}",
                headers={
                    **authorization_headers(),
                    "content-length": declared,
                    "content-type": "application/json",
                },
            )
            for declared in ("9" * 5_000, "not-a-number")
        )
        hidden = client.get("/docs")

    assert health.json()["data"]["upstream_commit"].startswith("01477f9a")
    auth_source_hash = health.json()["data"]["service_auth_source_hash"]
    assert len(auth_source_hash) == 64
    assert all(character in "0123456789abcdef" for character in auth_source_hash)
    assert success.status_code == 200
    assert success.json()["success"] is True
    assert set(success.json()) == {"success", "status", "data", "error", "metadata"}
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert oversized.status_code == 413
    assert all(response.status_code == 413 for response in hostile_lengths)
    assert all(
        response.json()["error"]["code"] == "request_too_large"
        for response in hostile_lengths
    )
    assert hidden.status_code == 404


def test_api_maps_runtime_and_media_failures_without_leaking() -> None:
    failing = TradingAgentsWorker(
        policy=policy(),
        runtime=RecordingRuntime(error=RuntimeError("secret=hidden")),
        artifacts=FakeArtifacts(),
        clock=lambda: NOW,
    )
    request = analysis_request()
    authenticator = ExactServiceAuthenticator.for_request(
        request,
        receiver=ServiceReceiver.TRADINGAGENTS,
    )
    app = create_app(worker=failing, authenticator=authenticator)
    with pytest.raises(ValueError, match="max_request_bytes"):
        create_app(worker=failing, authenticator=authenticator, max_request_bytes=0)
    with TestClient(app) as client:
        unsupported = client.post(
            "/v1/analyze", content=b"{}", headers=authorization_headers()
        )
        encoded = client.post(
            "/v1/analyze",
            content=b"{}",
            headers={
                **authorization_headers(),
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )
        failed = client.post(
            "/v1/analyze",
            content=request.model_dump_json(),
            headers={**authorization_headers(), "content-type": "application/json"},
        )
        denied = client.post(
            "/v1/analyze",
            content=request.model_dump_json(),
            headers={
                "authorization": "Bearer wrong-but-long-service-token",
                "content-type": "application/json",
            },
        )
    wrong_target = TestClient(
        create_app(
            worker=failing,
            authenticator=ExactServiceAuthenticator.for_request(
                request,
                receiver=ServiceReceiver.TRADINGAGENTS,
                target_identifier=UUID(int=999),
            ),
        )
    ).post(
        "/v1/analyze",
        content=request.model_dump_json(),
        headers={**authorization_headers(), "content-type": "application/json"},
    )

    assert unsupported.status_code == 415
    assert encoded.status_code == 415
    assert encoded.json()["error"]["code"] == "unsupported_content_encoding"
    assert failed.status_code == 503
    assert denied.status_code == 401
    assert wrong_target.status_code == 403
    assert "hidden" not in failed.text


def test_environment_is_strictly_allowlisted_and_denies_db_broker_queue_secrets() -> (
    None
):
    accepted = validate_worker_environment(
        {
            "STONKS_WORKER_PROFILE": "paper",
            "STONKS_MODEL_PROXY_TOKEN": "secret-ref-at-runtime",
        }
    )
    assert accepted.profile is WorkerProfile.PAPER

    for forbidden in (
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "BROKER_API_KEY",
        "REDIS_URL",
        "QUEUE_TOKEN",
        "OPENAI_API_KEY",
    ):
        with pytest.raises(ValueError, match="forbidden worker environment"):
            validate_worker_environment(
                {
                    "STONKS_WORKER_PROFILE": "paper",
                    forbidden: "must-not-enter-worker",
                }
            )


def test_packaging_pin_license_lock_and_container_hardening() -> None:
    project = tomllib.loads((WORKER / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(project["project"]["dependencies"])
    lock = (WORKER / "uv.lock").read_text(encoding="utf-8")
    dockerfile = (WORKER / "Dockerfile").read_text(encoding="utf-8")
    notice = (WORKER / "NOTICE.md").read_text(encoding="utf-8")
    core_lock = (ROOT / "uv.lock").read_text(encoding="utf-8").lower()

    assert "01477f9afb7a47b849ed4c9259d3a9a4738d9fda" in dependencies
    assert "01477f9afb7a47b849ed4c9259d3a9a4738d9fda" in lock
    assert "apache-2.0" in notice.lower()
    assert "TauricResearch/TradingAgents" in notice
    assert "@sha256:" in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "PYTHONPATH=/workspace" in dockerfile
    assert "WORKDIR /workspace" in dockerfile
    assert "ADD http" not in dockerfile
    assert "COPY .env" not in dockerfile
    assert 'name = "tradingagents"' not in core_lock
    assert 'name = "torch"' not in core_lock


def test_runtime_config_and_evidence_facade_are_fixed_and_fail_closed() -> None:
    config = _runtime_config(
        {
            "data_vendors": {"core_stock_apis": "yfinance"},
            "tool_vendors": {"get_stock_data": "alpha_vantage"},
        }
    )
    grouped = _content_by_category(resolved_request())

    assert config["backend_url"] == MODEL_PROXY_ORIGIN
    assert config["llm_provider"] == "openai_compatible"
    assert config["llm_max_retries"] == 0
    assert config["checkpoint_enabled"] is False
    assert set(config["data_vendors"].values()) == {"canonical_facade"}
    assert config["tool_vendors"] == {}
    assert str(EVIDENCE_ID) in grouped[EvidenceCategory.MARKET]
    assert "UNTRUSTED EVIDENCE" in grouped[EvidenceCategory.MARKET]
    assert grouped[EvidenceCategory.NEWS] == "No scoped evidence available."


def test_runtime_normalization_and_telemetry_are_bounded() -> None:
    assert _normalize_recommendation("BUY") == "Buy"
    with pytest.raises(ValueError, match="recommendation"):
        _normalize_recommendation("execute 100 shares")
    thesis, warnings = _extract_thesis({"final_trade_decision": "x" * 20_000})
    assert len(thesis) == 16_384
    assert warnings == ("upstream_thesis_truncated",)

    moments = iter((1.0, 1.025, 2.0, 2.010))
    callback = _TelemetryCallback(clock=lambda: next(moments))
    callback.on_llm_start()
    callback.on_llm_end(
        type(
            "Response",
            (),
            {
                "llm_output": {
                    "token_usage": {"prompt_tokens": 9, "completion_tokens": 4}
                }
            },
        )()
    )
    callback.on_tool_start()
    callback.on_tool_end("ok")
    assert callback.model_usage[0].input_tokens == 9
    assert callback.model_usage[0].output_tokens == 4
    assert callback.model_usage[0].latency_ms == 24
    assert callback.tool_latency_ms == (9,)
    with pytest.raises(ValueError, match="state"):
        _extract_thesis([])
    with pytest.raises(ValueError, match="thesis"):
        _extract_thesis({"final_trade_decision": ""})


def test_pinned_runtime_maps_fake_upstream_without_execution_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Graph:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("init", kwargs))

        def propagate(
            self, *args: object, **kwargs: object
        ) -> tuple[dict[str, str], str]:
            calls.append(("propagate", args, kwargs))
            return {"final_trade_decision": "Evidence-scoped thesis"}, "BUY"

    monkeypatch.setattr(
        runtime_module,
        "_load_upstream",
        lambda: (SimpleNamespace(), Graph, {}),
    )
    monkeypatch.setattr(
        runtime_module,
        "_install_canonical_facade",
        lambda module, request: calls.append(("facade", module, request)),
    )

    result = PinnedTradingAgentsRuntime(
        selected_analysts=("market", "fundamentals")
    ).run(resolved_request())

    assert result.recommendation == "Buy"
    assert result.thesis == "Evidence-scoped thesis"
    assert result.telemetry.source_refs == (EVIDENCE_ID,)
    assert [item[0] for item in calls] == ["facade", "init", "propagate"]


def test_canonical_facade_replaces_every_upstream_data_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    langchain = ModuleType("langchain_core")
    tools = ModuleType("langchain_core.tools")
    tools.tool = lambda function: function  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_core", langchain)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools)
    upstream = SimpleNamespace()

    _install_canonical_facade(upstream, resolved_request())

    assert "UNTRUSTED EVIDENCE" in upstream.get_stock_data("AAPL", "x", "y")
    assert "UNTRUSTED EVIDENCE" in upstream.get_indicators("AAPL", "rsi", "x")
    assert "UNTRUSTED EVIDENCE" in upstream.get_verified_market_snapshot("AAPL", "x")
    assert upstream.get_fundamentals("AAPL") == "No scoped evidence available."
    assert upstream.get_news("AAPL") == "No scoped evidence available."
    assert upstream.get_global_news() == "No scoped evidence available."
    assert upstream.get_insider_transactions("AAPL") == "No scoped evidence available."
    assert upstream.resolve_instrument_identity("AAPL") == {
        "symbol": "AAPL",
        "name": "AAPL",
    }
    with pytest.raises(ValueError, match="symbol"):
        upstream.get_stock_data("MSFT", "x", "y")


def test_runtime_entrypoint_builds_one_profile_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stonks_service_auth

    monkeypatch.setenv("STONKS_WORKER_PROFILE", "backtest")
    monkeypatch.delenv("STONKS_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("STONKS_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        stonks_service_auth,
        "load_static_oidc_service_authenticator",
        lambda _environment: ExactServiceAuthenticator.for_request(
            analysis_request(),
            receiver=ServiceReceiver.TRADINGAGENTS,
        ),
    )
    sys.modules.pop("workers.tradingagents.runtime_app", None)

    loaded = importlib.import_module("workers.tradingagents.runtime_app")

    assert loaded.policy.profile is WorkerProfile.BACKTEST
    assert loaded.runtime._selected_analysts == (
        "market",
        "fundamentals",
        "news",
        "social",
    )


def test_profiles_are_separate_fail_closed_processes_with_internal_network() -> None:
    compose = yaml.safe_load(
        (ROOT / "infra" / "compose.tradingagents.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]

    assert set(services) == {
        "tradingagents-paper",
        "tradingagents-backtest",
        "tradingagents-production",
    }
    service_trust = {
        "STONKS_SERVICE_OIDC_ISSUER": "${STONKS_SERVICE_OIDC_ISSUER:?required}",
        "STONKS_SERVICE_OIDC_AUDIENCE": (
            "${STONKS_TRADINGAGENTS_SERVICE_OIDC_AUDIENCE:?required}"
        ),
        "STONKS_SERVICE_OIDC_CORE_SUBJECT": (
            "${STONKS_SERVICE_OIDC_CORE_SUBJECT:?required}"
        ),
        "STONKS_SERVICE_OIDC_CORE_CLIENT_ID": (
            "${STONKS_SERVICE_OIDC_CORE_CLIENT_ID:?required}"
        ),
        "STONKS_SERVICE_OIDC_ALGORITHMS": "RS256",
        "STONKS_SERVICE_OIDC_RECEIVER": "tradingagents",
        "STONKS_SERVICE_OIDC_JWKS_FILE": "/run/secrets/stonks-service-jwks.json",
    }
    for profile in ("paper", "backtest", "production"):
        service = services[f"tradingagents-{profile}"]
        assert service["environment"] == service_trust | {
            "STONKS_WORKER_PROFILE": profile
        }
        assert service["volumes"] == [
            "${STONKS_SERVICE_OIDC_JWKS_HOST_FILE:?required}:"
            "/run/secrets/stonks-service-jwks.json:ro"
        ]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["networks"] == ["tradingagents-internal"]
        assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m"]
    assert compose["networks"]["tradingagents-internal"]["internal"] is True


def test_artifact_resolver_enforces_origin_hash_redirect_and_size() -> None:
    content = b'{"close":"100.00"}'
    digest = hashlib.sha256(content).hexdigest()
    capability = evidence(
        artifact_ref=f"sha256:{digest}",
        signed_url=(
            f"http://artifact-service:8080/v1/artifacts/{digest}"
            "?expires=1783857900&signature=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ),
    )

    def ok(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, content=content, request=incoming)

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        resolver = FixedOriginArtifactResolver(
            client=client,
            origin="http://artifact-service:8080",
            max_bytes=1024,
            timeout_seconds=1,
            clock=lambda: NOW,
        )
        assert resolver.resolve(capability) == content.decode()
        with pytest.raises(ValueError, match="scope"):
            resolver.resolve(
                capability.model_copy(
                    update={
                        "signed_url": capability.signed_url.replace(
                            "artifact-service:8080", "attacker:8080"
                        )
                    }
                )
            )
        small = FixedOriginArtifactResolver(
            client=client,
            origin="http://artifact-service:8080",
            max_bytes=1,
            timeout_seconds=1,
            clock=lambda: NOW,
        )
        with pytest.raises(ValueError, match="exceeds"):
            small.resolve(capability)

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(
                302, headers={"location": "http://attacker"}, request=incoming
            )
        )
    ) as client:
        resolver = FixedOriginArtifactResolver(
            client=client,
            origin="http://artifact-service:8080",
            max_bytes=1,
            timeout_seconds=1,
            clock=lambda: NOW,
        )
        with pytest.raises(ValueError, match="rejected"):
            resolver.resolve(capability)


def _forbidden_authority_keys(value: object) -> set[str]:
    forbidden = {
        "order",
        "orders",
        "quantity",
        "qty",
        "execution",
        "trade_intent",
        "portfolio_target",
        "risk_decision",
    }
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in forbidden:
                found.add(key.lower())
            found.update(_forbidden_authority_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_forbidden_authority_keys(nested))
    return found
