"""Clean-room HTTP adapter for AI-Trader community/control endpoints only."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock
from time import monotonic
from typing import Final, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import BaseModel, ValidationError

from stonks_agent.adapters.market_data._http_response import (
    ResponseBodyError,
    read_bounded_raw,
    response_deadline,
)
from stonks_agent.adapters.platform._ai_trader_contracts import (
    AiTraderEvent,
    AiTraderEventPage,
    AiTraderEventPollRequest,
    AiTraderReplyRequest,
    AiTraderWriteReceipt,
    _ChallengeJoinResponse,
    _ChallengeSubmissionResponse,
    _ChallengeVoteResponse,
    _ExperimentAssignResponse,
    _HeartbeatRecord,
    _HeartbeatResponse,
    _PublicationResponse,
    _RepliesResponse,
    _ReplyRecord,
    _WriteResponse,
)
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.secrets import SecretAccessRequest, SecretRef
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_agent.ports.platform import PlatformEventInboxPort
from stonks_agent.ports.secret_provider import SecretProvider
from stonks_contracts.evidence import Sensitivity
from stonks_contracts.platform import (
    ChallengeAction,
    ChallengeRequest,
    ChallengeResult,
    ExperimentAction,
    ExperimentRequest,
    ExperimentResult,
    ExternalActivityStatus,
    ExternalEvidence,
    ExternalEvidenceKind,
    FeedbackPage,
    FeedbackPollRequest,
    PublishedThesis,
    PublishThesisRequest,
)

AI_TRADER_ORIGIN: Final = "https://api.ai4trade.ai"
AI_TRADER_UPSTREAM_COMMIT: Final = "d03ff6c056b32ced735adf7c19ed8175adb1c8df"
AI_TRADER_ENDPOINT_TEMPLATES: Final = frozenset(
    {
        "/api/signals/strategy",
        "/api/signals/discussion",
        "/api/signals/reply",
        "/api/signals/{signal_id}/replies",
        "/api/claw/agents/heartbeat",
        "/api/challenges/{challenge_key}/join",
        "/api/challenges/{challenge_key}/submit",
        "/api/challenges/{challenge_key}/submissions/{submission_id}/vote",
        "/api/experiments/{experiment_key}/assign",
    }
)
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]{0,18}$")
_REPLY_CURSOR = re.compile(r"^reply:([1-9][0-9]{0,18})$")
_SECRET_PURPOSE: Final = "ai_trader_access_token"


class AiTraderHttpAdapter:
    """Use a fixed public community API surface with no execution endpoints."""

    __slots__ = (
        "_artifacts",
        "_client",
        "_clock",
        "_disabled_reason",
        "_event_inbox",
        "_lock",
        "_max_request_bytes",
        "_max_response_bytes",
        "_monotonic_clock",
        "_secret_provider",
        "_secret_ref",
        "_timeout",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        artifacts: ArtifactStore,
        event_inbox: PlatformEventInboxPort,
        secret_provider: SecretProvider,
        secret_ref: SecretRef,
        origin: str = AI_TRADER_ORIGIN,
        timeout_seconds: float = 10.0,
        max_request_bytes: int = 65_536,
        max_response_bytes: int = 1_048_576,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if origin != AI_TRADER_ORIGIN:
            raise ValueError("AI-Trader origin is not allowlisted")
        if timeout_seconds <= 0 or max_request_bytes <= 0 or max_response_bytes <= 0:
            raise ValueError("AI-Trader HTTP limits must be positive")
        self._client = client
        self._artifacts = artifacts
        self._event_inbox = event_inbox
        self._secret_provider = secret_provider
        self._secret_ref = secret_ref
        self._timeout_seconds = float(timeout_seconds)
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._clock = clock or utc_now
        self._monotonic_clock = monotonic_clock or monotonic
        self._disabled_reason: str | None = None
        self._lock = RLock()

    @property
    def is_disabled(self) -> bool:
        with self._lock:
            return self._disabled_reason is not None

    def publish_thesis(self, request: PublishThesisRequest) -> Result[PublishedThesis]:
        return self._publish(request, endpoint="strategy")

    def publish_discussion(
        self, request: PublishThesisRequest
    ) -> Result[PublishedThesis]:
        return self._publish(request, endpoint="discussion")

    def reply(self, request: AiTraderReplyRequest) -> Result[AiTraderWriteReceipt]:
        signal_id = _positive_id(request.publication_id)
        if signal_id is None:
            return _failure(ErrorCode.INVALID_INPUT, "Publication identity is invalid")
        response = self._post(
            "/api/signals/reply",
            "/api/signals/reply",
            {"signal_id": signal_id, "content": request.public_content},
            request.request_id,
            request.idempotency_key,
            request.deadline,
        )
        if isinstance(response, Failure):
            return response
        parsed = self._parse(response.value, _WriteResponse)
        if isinstance(parsed, Failure):
            return parsed
        archived = self._archive(response.value)
        if isinstance(archived, Failure):
            return archived
        return Success(
            AiTraderWriteReceipt(
                request_id=request.request_id,
                run_id=request.run_id,
                external_ref=f"signal:{signal_id}:reply",
                occurred_at=self._now(),
                response_artifact_ref=f"sha256:{archived.value}",
                response_content_hash=archived.value,
            )
        )

    def poll_feedback(self, request: FeedbackPollRequest) -> Result[FeedbackPage]:
        signal_id = _positive_id(request.publication_id)
        cursor = _reply_cursor(request.cursor)
        if signal_id is None or isinstance(cursor, Failure):
            return _failure(
                ErrorCode.INVALID_INPUT, "Feedback cursor or identity is invalid"
            )
        path = f"/api/signals/{signal_id}/replies"
        response = self._get(
            "/api/signals/{signal_id}/replies",
            path,
            request.request_id,
            request.deadline,
        )
        if isinstance(response, Failure):
            return response
        parsed = self._parse(response.value, _RepliesResponse)
        if isinstance(parsed, Failure):
            return parsed
        if any(record.signal_id != signal_id for record in parsed.value.replies):
            return self._disable(
                ErrorCode.MODEL_OUTPUT_INVALID, "Feedback scope is invalid"
            )
        archived_page = self._archive(response.value)
        if isinstance(archived_page, Failure):
            return archived_page
        records = sorted(
            parsed.value.replies, key=lambda item: (item.created_at, item.id)
        )
        selected = [item for item in records if item.id > cursor.value][: request.limit]
        evidence: list[ExternalEvidence] = []
        for record in selected:
            converted = self._feedback_evidence(record, request)
            if isinstance(converted, Failure):
                return converted
            evidence.append(converted.value)
        next_id = selected[-1].id if selected else cursor.value
        return Success(
            FeedbackPage(
                request_id=request.request_id,
                run_id=request.run_id,
                platform="ai-trader",
                publication_id=request.publication_id,
                request_cursor=request.cursor,
                next_cursor=f"reply:{next_id}" if next_id else None,
                evidence=tuple(evidence),
                observed_at=self._now(),
                response_artifact_ref=f"sha256:{archived_page.value}",
                response_content_hash=archived_page.value,
            )
        )

    def poll_events(
        self, request: AiTraderEventPollRequest
    ) -> Result[AiTraderEventPage]:
        response = self._post(
            "/api/claw/agents/heartbeat",
            "/api/claw/agents/heartbeat",
            {},
            request.request_id,
            None,
            request.deadline,
        )
        if isinstance(response, Failure):
            return response
        parsed = self._parse(response.value, _HeartbeatResponse)
        if isinstance(parsed, Failure):
            return parsed
        archived_page = self._archive(response.value)
        if isinstance(archived_page, Failure):
            return archived_page
        events: list[AiTraderEvent] = []
        for kind, records in (
            ("message", parsed.value.messages),
            ("task", parsed.value.tasks),
        ):
            for record in records:
                converted = self._platform_event(
                    cast(Literal["message", "task"], kind), record
                )
                if isinstance(converted, Failure):
                    return converted
                accepted = self._event_inbox.accept(
                    converted.value.event_id, converted.value.content_hash
                )
                if isinstance(accepted, Failure):
                    return self._disable(ErrorCode.CONFLICT, "Platform inbox conflict")
                if accepted.value:
                    events.append(converted.value)
        cursor_hash = hashlib.sha256(
            f"{parsed.value.server_time.isoformat()}:{archived_page.value}".encode()
        ).hexdigest()
        return Success(
            AiTraderEventPage(
                request_id=request.request_id,
                run_id=request.run_id,
                request_cursor=request.cursor,
                next_cursor=f"heartbeat:{cursor_hash}",
                events=tuple(events),
                observed_at=self._now(),
                response_artifact_ref=f"sha256:{archived_page.value}",
                response_content_hash=archived_page.value,
            )
        )

    def submit_challenge(self, request: ChallengeRequest) -> Result[ChallengeResult]:
        key = _safe_segment(request.activity_ref)
        if key is None:
            return _failure(ErrorCode.INVALID_INPUT, "Challenge identity is invalid")
        route = self._challenge_route(request, key)
        if isinstance(route, Failure):
            return route
        template, path, body, model = route
        response = self._post(
            template,
            path,
            body,
            request.request_id,
            request.idempotency_key,
            request.deadline,
        )
        if isinstance(response, Failure):
            return response
        parsed = self._parse(response.value, model)
        if isinstance(parsed, Failure):
            return parsed
        archived = self._archive(response.value)
        if isinstance(archived, Failure):
            return archived
        return Success(
            ChallengeResult(
                request_id=request.request_id,
                run_id=request.run_id,
                platform="ai-trader",
                activity_ref=request.activity_ref,
                action=request.action,
                status=ExternalActivityStatus.ACCEPTED,
                external_ref=_challenge_external_ref(key, parsed.value),
                occurred_at=self._now(),
                response_artifact_ref=f"sha256:{archived.value}",
                response_content_hash=archived.value,
            )
        )

    def submit_experiment(self, request: ExperimentRequest) -> Result[ExperimentResult]:
        key = _safe_segment(request.activity_ref)
        if key is None:
            return _failure(ErrorCode.INVALID_INPUT, "Experiment identity is invalid")
        route = self._experiment_route(request, key)
        if isinstance(route, Failure):
            return route
        template, path, body, model = route
        response = self._post(
            template,
            path,
            body,
            request.request_id,
            request.idempotency_key,
            request.deadline,
        )
        if isinstance(response, Failure):
            return response
        parsed = self._parse(response.value, model)
        if isinstance(parsed, Failure):
            return parsed
        archived = self._archive(response.value)
        if isinstance(archived, Failure):
            return archived
        if isinstance(parsed.value, _ExperimentAssignResponse):
            external_ref = f"experiment:{key}:variant:{parsed.value.variant_key}"
        elif isinstance(parsed.value, _PublicationResponse):
            external_ref = f"experiment:{key}:signal:{parsed.value.signal_id}"
        else:
            return self._disable(
                ErrorCode.MODEL_OUTPUT_INVALID, "Experiment response schema is invalid"
            )
        return Success(
            ExperimentResult(
                request_id=request.request_id,
                run_id=request.run_id,
                platform="ai-trader",
                activity_ref=request.activity_ref,
                action=request.action,
                status=ExternalActivityStatus.ACCEPTED,
                external_ref=external_ref,
                occurred_at=self._now(),
                response_artifact_ref=f"sha256:{archived.value}",
                response_content_hash=archived.value,
            )
        )

    def _publish(
        self,
        request: PublishThesisRequest,
        *,
        endpoint: Literal["strategy", "discussion"],
    ) -> Result[PublishedThesis]:
        if request.platform != "ai-trader":
            return _failure(ErrorCode.INVALID_INPUT, "Platform identity is invalid")
        path = f"/api/signals/{endpoint}"
        body = _publication_body(request, endpoint)
        response = self._post(
            path,
            path,
            body,
            request.request_id,
            request.idempotency_key,
            request.observation_deadline,
        )
        if isinstance(response, Failure):
            return response
        parsed = self._parse(response.value, _PublicationResponse)
        if isinstance(parsed, Failure):
            return parsed
        archived = self._archive(response.value)
        if isinstance(archived, Failure):
            return archived
        now = self._now()
        return Success(
            PublishedThesis(
                request_id=request.request_id,
                run_id=request.run_id,
                platform=request.platform,
                publication_id=str(parsed.value.signal_id),
                external_ref=f"signal:{parsed.value.signal_id}",
                thesis_content_hash=request.thesis_content_hash,
                published_at=now,
                observation_deadline=request.observation_deadline,
                response_artifact_ref=f"sha256:{archived.value}",
                response_content_hash=archived.value,
            )
        )

    def _challenge_route(
        self, request: ChallengeRequest, key: str
    ) -> tuple[str, str, dict[str, object], type[BaseModel]] | Failure:
        if request.action is ChallengeAction.JOIN:
            template = "/api/challenges/{challenge_key}/join"
            return template, f"/api/challenges/{key}/join", {}, _ChallengeJoinResponse
        if request.action is ChallengeAction.SUBMIT_RESEARCH:
            template = "/api/challenges/{challenge_key}/submit"
            body: dict[str, object] = {
                "submission_type": "research_artifact",
                "content": request.public_content,
                "prediction_json": {
                    "artifact_ref": request.payload_artifact_ref,
                    "content_hash": request.payload_content_hash,
                    "redaction_manifest_hash": request.redaction_manifest_hash,
                },
            }
            return (
                template,
                f"/api/challenges/{key}/submit",
                body,
                _ChallengeSubmissionResponse,
            )
        submission = _positive_id(request.submission_ref)
        if submission is None or request.vote is None:
            return _failure(
                ErrorCode.INVALID_INPUT, "Challenge vote identity is invalid"
            )
        template = "/api/challenges/{challenge_key}/submissions/{submission_id}/vote"
        vote_body: dict[str, object] = {
            "vote": request.vote.value,
            "content": request.public_content,
        }
        return (
            template,
            f"/api/challenges/{key}/submissions/{submission}/vote",
            vote_body,
            _ChallengeVoteResponse,
        )

    def _experiment_route(
        self, request: ExperimentRequest, key: str
    ) -> tuple[str, str, dict[str, object], type[BaseModel]] | Failure:
        if request.action is ExperimentAction.ENROLL:
            template = "/api/experiments/{experiment_key}/assign"
            return (
                template,
                f"/api/experiments/{key}/assign",
                {},
                _ExperimentAssignResponse,
            )
        endpoint = (
            "discussion"
            if request.action is ExperimentAction.SUBMIT_OBSERVATION
            else "strategy"
        )
        path = f"/api/signals/{endpoint}"
        body: dict[str, object] = {
            "market": request.market,
            "title": request.public_title,
            "content": request.public_content,
            "tags": f"stonks-agent,experiment:{key}",
        }
        if endpoint == "discussion":
            body["symbol"] = request.subject
        else:
            body["symbols"] = request.subject
        return path, path, body, _PublicationResponse

    def _feedback_evidence(
        self, record: _ReplyRecord, request: FeedbackPollRequest
    ) -> Result[ExternalEvidence]:
        payload = record.model_dump(mode="json")
        raw = _canonical_bytes(payload)
        archived = self._archive(raw)
        if isinstance(archived, Failure):
            return archived
        return Success(
            ExternalEvidence(
                evidence_id=uuid5(NAMESPACE_URL, f"ai-trader:reply:{record.id}"),
                platform="ai-trader",
                external_event_id=f"reply:{record.id}",
                subject=f"publication:{request.publication_id}",
                kind=ExternalEvidenceKind.COMMUNITY_FEEDBACK,
                payload={
                    "content": record.content,
                    "accepted": record.accepted,
                    "agent_name": record.agent_name,
                    "agent_is_verified": record.agent_is_verified,
                },
                event_time=record.created_at,
                published_at=record.created_at,
                available_at=record.created_at,
                observed_at=self._now(),
                as_of=request.as_of,
                content_hash=archived.value,
                raw_artifact_ref=f"sha256:{archived.value}",
                author_ref=str(record.agent_id),
                license_tag="external-platform-terms",
                redistribution_tag="internal-only",
                ingestion_version=f"ai-trader/{AI_TRADER_UPSTREAM_COMMIT[:8]}",
            )
        )

    def _platform_event(
        self, kind: Literal["message", "task"], record: _HeartbeatRecord
    ) -> Result[AiTraderEvent]:
        payload = record.model_dump(mode="json")
        archived = self._archive(_canonical_bytes(payload))
        if isinstance(archived, Failure):
            return archived
        return Success(
            AiTraderEvent(
                event_id=f"{kind}:{record.id}",
                kind=kind,
                payload=payload,
                occurred_at=record.created_at,
                content_hash=archived.value,
                raw_artifact_ref=f"sha256:{archived.value}",
            )
        )

    def _get(
        self, template: str, path: str, request_id: UUID, deadline: datetime
    ) -> Result[bytes]:
        return self._request("GET", template, path, None, request_id, None, deadline)

    def _post(
        self,
        template: str,
        path: str,
        body: dict[str, object],
        request_id: UUID,
        idempotency_key: str | None,
        deadline: datetime,
    ) -> Result[bytes]:
        return self._request(
            "POST", template, path, body, request_id, idempotency_key, deadline
        )

    def _request(
        self,
        method: Literal["GET", "POST"],
        template: str,
        path: str,
        body: dict[str, object] | None,
        request_id: UUID,
        idempotency_key: str | None,
        deadline: datetime,
    ) -> Result[bytes]:
        disabled = self._disabled()
        if disabled is not None:
            return disabled
        if template not in AI_TRADER_ENDPOINT_TEMPLATES or not path.startswith("/api/"):
            return self._disable(ErrorCode.EGRESS_DENIED, "Platform endpoint is denied")
        now = self._now()
        if now >= deadline:
            return _failure(
                ErrorCode.DEADLINE_EXCEEDED, "Platform request deadline exceeded"
            )
        encoded = None if body is None else _canonical_bytes(body)
        if encoded is not None and len(encoded) > self._max_request_bytes:
            return _failure(
                ErrorCode.PAYLOAD_TOO_LARGE, "Platform request is too large"
            )
        credential = self._resolve_access_token()
        if isinstance(credential, Failure):
            return credential
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {credential.value}",
            "X-Stonks-Request-ID": str(request_id),
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        deadline_at = response_deadline(self._monotonic_clock, self._timeout_seconds)
        try:
            with self._client.stream(
                method,
                f"{AI_TRADER_ORIGIN}{path}",
                content=encoded,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                status_failure = self._status_failure(response.status_code)
                if status_failure is not None:
                    return status_failure
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", maxsplit=1)[0].strip() != "application/json":
                    return self._disable(
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        "Platform response media type is invalid",
                    )
                raw = read_bounded_raw(
                    response,
                    max_bytes=self._max_response_bytes,
                    deadline=deadline_at,
                    clock=self._monotonic_clock,
                )
                if raw is ResponseBodyError.DEADLINE_EXCEEDED:
                    return _failure(
                        ErrorCode.DEADLINE_EXCEEDED, "Platform response timed out"
                    )
                if isinstance(raw, ResponseBodyError):
                    return self._disable(
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        "Platform response body is invalid",
                    )
                return Success(raw)
        except httpx.TimeoutException:
            return _failure(ErrorCode.DEADLINE_EXCEEDED, "Platform request timed out")
        except httpx.HTTPError:
            return _failure(ErrorCode.DATA_UNAVAILABLE, "Platform is unavailable")

    def _resolve_access_token(self) -> Result[str]:
        try:
            resolved = self._secret_provider.resolve(
                SecretAccessRequest(
                    reference=self._secret_ref,
                    purpose=_SECRET_PURPOSE,
                )
            )
            if isinstance(resolved, Failure):
                return _credential_failure(resolved.error.code)
            token = resolved.value.reveal()
            if not _is_valid_access_token(token):
                return _credential_failure(ErrorCode.CONFIGURATION_INVALID)
            return Success(token)
        except Exception:
            return _credential_failure(ErrorCode.INTERNAL_ERROR)

    def _status_failure(self, status: int) -> Failure | None:
        if status == 401:
            return self._disable(
                ErrorCode.UNAUTHORIZED, "Platform authorization failed"
            )
        if status == 403:
            return self._disable(ErrorCode.FORBIDDEN, "Platform authorization failed")
        if 300 <= status < 400:
            return self._disable(ErrorCode.EGRESS_DENIED, "Platform redirect is denied")
        mapping = {
            400: ErrorCode.INVALID_INPUT,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
            413: ErrorCode.PAYLOAD_TOO_LARGE,
            429: ErrorCode.RATE_LIMITED,
        }
        if status in mapping:
            return _failure(mapping[status], "Platform request was rejected")
        if status >= 500:
            return _failure(ErrorCode.DATA_UNAVAILABLE, "Platform is unavailable")
        if status != 200:
            return self._disable(
                ErrorCode.MODEL_OUTPUT_INVALID, "Platform status is invalid"
            )
        return None

    def _parse[T: BaseModel](self, body: bytes, model: type[T]) -> Result[T]:
        try:
            value = json.loads(body, parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise ValueError("response must be an object")
            return Success(model.model_validate(value))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
            return self._disable(
                ErrorCode.MODEL_OUTPUT_INVALID, "Platform response schema is invalid"
            )

    def _archive(self, body: bytes) -> Result[str]:
        manifest = self._artifacts.finalize(
            body,
            metadata=ArtifactMetadata(
                media_type="application/json",
                license_tag="external-platform-terms",
                sensitivity=Sensitivity.PUBLIC,
                source="stonks-agent-ai-trader-http",
            ),
            finalized_at=self._now(),
        )
        if isinstance(manifest, Failure):
            return manifest
        return Success(manifest.value.content_hash)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("adapter clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _disabled(self) -> Failure | None:
        with self._lock:
            if self._disabled_reason is None:
                return None
        return _failure(ErrorCode.CAPABILITY_DENIED, "Platform adapter is disabled")

    def _disable(self, code: ErrorCode, message: str) -> Failure:
        with self._lock:
            if self._disabled_reason is None:
                self._disabled_reason = code.value
        return _failure(code, message)


def _publication_body(
    request: PublishThesisRequest, endpoint: Literal["strategy", "discussion"]
) -> dict[str, object]:
    body: dict[str, object] = {
        "market": request.market,
        "title": request.public_title,
        "content": request.public_summary,
        "tags": "stonks-agent,evidence-backed",
    }
    body["symbols" if endpoint == "strategy" else "symbol"] = request.subject
    contexts = {
        "challenge_key": request.challenge_ref,
        "mission_key": request.mission_ref,
        "team_key": request.team_ref,
    }
    body.update({key: value for key, value in contexts.items() if value is not None})
    return body


def _challenge_external_ref(key: str, response: BaseModel) -> str:
    if isinstance(response, _ChallengeJoinResponse):
        return f"challenge:{key}:participant:{response.participant.id}"
    if isinstance(response, _ChallengeSubmissionResponse):
        return f"challenge:{key}:submission:{response.id}"
    if isinstance(response, _ChallengeVoteResponse):
        return f"challenge:{key}:vote:{response.vote.id}"
    raise TypeError("unsupported challenge response")


def _reply_cursor(value: str | None) -> Success[int] | Failure:
    if value is None:
        return Success(0)
    match = _REPLY_CURSOR.fullmatch(value)
    if match is None:
        return _failure(ErrorCode.INVALID_INPUT, "Feedback cursor is invalid")
    return Success(int(match.group(1)))


def _positive_id(value: str | None) -> int | None:
    if value is None or _POSITIVE_INTEGER.fullmatch(value) is None:
        return None
    return int(value)


def _safe_segment(value: str) -> str | None:
    return value if _SEGMENT.fullmatch(value) is not None else None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))


def _credential_failure(source_code: ErrorCode) -> Failure:
    safe_code = (
        source_code
        if source_code in {ErrorCode.CONFIGURATION_INVALID, ErrorCode.DATA_UNAVAILABLE}
        else ErrorCode.INTERNAL_ERROR
    )
    return _failure(
        safe_code,
        "Platform credential is unavailable",
    )


def _is_valid_access_token(value: str) -> bool:
    return (
        1 <= len(value) <= 4096
        and value.strip() == value
        and all(33 <= ord(character) <= 126 for character in value)
    )
