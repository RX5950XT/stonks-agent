"""Read-only, point-in-time evidence tools scoped to one research request."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from time import monotonic
from typing import Protocol
from uuid import UUID

from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.tool_policy import (
    AuthorizedToolCall,
    ToolArgumentKind,
    ToolArgumentSpec,
    ToolMutationClass,
    ToolPolicy,
    ToolResult,
    ToolRule,
)
from stonks_agent.ports.artifact_store import ArtifactStore
from stonks_contracts.common import canonical_json
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity

_TOOL_VERSION = "evidence-tools/1.0.0"


class EvidenceReader(Protocol):
    def get(self, evidence_id: UUID) -> Result[EvidenceItem]: ...


@dataclass(frozen=True, slots=True)
class _EvidenceOutput:
    payload: dict[str, object]
    materialized_evidence_ids: frozenset[UUID]


class EvidenceTool:
    __slots__ = ("_artifacts", "_as_of", "_clock", "_repository")

    def __init__(
        self,
        *,
        repository: EvidenceReader,
        artifacts: ArtifactStore,
        as_of: datetime,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("evidence tool as_of must be timezone-aware")
        self._repository = repository
        self._artifacts = artifacts
        self._as_of = as_of.astimezone(UTC)
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, call: AuthorizedToolCall) -> Result[ToolResult]:
        started = monotonic()
        if call.policy_id != "research-evidence-v1" or not call.audit_required:
            return _failure(ErrorCode.CAPABILITY_DENIED, "Evidence tool call denied")
        if call.tool_name == "list_evidence":
            result = self._list(call)
        elif call.tool_name == "read_evidence":
            result = self._read(call)
        elif call.tool_name == "price_window":
            result = self._price_window(call)
        elif call.tool_name == "fundamental_snapshot":
            result = self._kind_snapshot(call, EvidenceKind.FUNDAMENTAL)
        elif call.tool_name == "filing_history":
            result = self._kind_snapshot(call, EvidenceKind.FILING)
        else:
            return _failure(ErrorCode.CAPABILITY_DENIED, "Evidence tool is unavailable")
        if isinstance(result, Failure):
            return result
        return self._store(call, result.value, started)

    def _list(self, call: AuthorizedToolCall) -> Result[_EvidenceOutput]:
        items = self._scoped_items(call)
        if isinstance(items, Failure):
            return items
        return Success(
            _EvidenceOutput(
                payload={
                    "count": len(items.value),
                    "items": [
                        {
                            "evidence_id": str(item.evidence_id),
                            "subject": item.subject,
                            "kind": item.kind.value,
                            "available_at": item.available_at.isoformat(),
                            "source": item.source,
                            "provider": item.provider,
                        }
                        for item in items.value
                    ],
                },
                materialized_evidence_ids=frozenset(),
            )
        )

    def _read(self, call: AuthorizedToolCall) -> Result[_EvidenceOutput]:
        evidence_id = _uuid_argument(call, "evidence_id")
        if evidence_id is None or evidence_id not in call.evidence_ids:
            return _failure(ErrorCode.CAPABILITY_DENIED, "Evidence scope was exceeded")
        item = self._get_scoped(call, evidence_id)
        if isinstance(item, Failure):
            return item
        return Success(
            _EvidenceOutput(
                payload=item.value.model_dump(mode="json"),
                materialized_evidence_ids=frozenset({item.value.evidence_id}),
            )
        )

    def _price_window(
        self,
        call: AuthorizedToolCall,
    ) -> Result[_EvidenceOutput]:
        window = _window(call, self._as_of)
        if isinstance(window, Failure):
            return window
        items = self._scoped_items(call)
        if isinstance(items, Failure):
            return items
        start, end = window.value
        bars: list[tuple[EvidenceItem, dict[str, str]]] = []
        for item in items.value:
            if (
                item.kind is EvidenceKind.MARKET_DATA
                and start <= item.event_time <= end
            ):
                parsed = _bar(item)
                if isinstance(parsed, Failure):
                    return parsed
                bars.append((item, parsed.value))
        bars.sort(key=lambda value: value[0].event_time)
        if not bars:
            return _failure(ErrorCode.NOT_FOUND, "No price evidence in window")
        return Success(
            _EvidenceOutput(
                payload={
                    "count": len(bars),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bars": [
                        {
                            "evidence_id": str(item.evidence_id),
                            "event_time": item.event_time.isoformat(),
                            **bar,
                        }
                        for item, bar in bars
                    ],
                    "statistics": _statistics(tuple(bar for _, bar in bars)),
                },
                materialized_evidence_ids=frozenset(
                    item.evidence_id for item, _ in bars
                ),
            )
        )

    def _kind_snapshot(
        self,
        call: AuthorizedToolCall,
        kind: EvidenceKind,
    ) -> Result[_EvidenceOutput]:
        items = self._scoped_items(call)
        if isinstance(items, Failure):
            return items
        selected = tuple(item for item in items.value if item.kind is kind)[:64]
        if not selected:
            return _failure(
                ErrorCode.NOT_FOUND, "Requested evidence kind is unavailable"
            )
        return Success(
            _EvidenceOutput(
                payload={
                    "kind": kind.value,
                    "count": len(selected),
                    "items": [item.model_dump(mode="json") for item in selected],
                },
                materialized_evidence_ids=frozenset(
                    item.evidence_id for item in selected
                ),
            )
        )

    def _scoped_items(
        self,
        call: AuthorizedToolCall,
    ) -> Result[tuple[EvidenceItem, ...]]:
        values: list[EvidenceItem] = []
        for evidence_id in sorted(call.evidence_ids, key=str):
            item = self._get_scoped(call, evidence_id)
            if isinstance(item, Failure):
                return item
            values.append(item.value)
        return Success(tuple(values))

    def _get_scoped(
        self,
        call: AuthorizedToolCall,
        evidence_id: UUID,
    ) -> Result[EvidenceItem]:
        loaded = self._repository.get(evidence_id)
        if isinstance(loaded, Failure):
            return loaded
        item = loaded.value
        if (
            item.evidence_id not in call.evidence_ids
            or item.subject not in call.instrument_ids
            or item.available_at > self._as_of
            or item.as_of > self._as_of
        ):
            return _failure(ErrorCode.CAPABILITY_DENIED, "Evidence scope was exceeded")
        return Success(item)

    def _store(
        self,
        call: AuthorizedToolCall,
        output: _EvidenceOutput,
        started: float,
    ) -> Result[ToolResult]:
        content = canonical_json(output.payload).encode("utf-8")
        if len(content) > call.output_limit_bytes:
            return _failure(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "Evidence tool output exceeded authorized limit",
            )
        observed_at = self._clock()
        stored = self._artifacts.finalize(
            content,
            metadata=ArtifactMetadata(
                media_type="application/json",
                license_tag="Apache-2.0",
                sensitivity=Sensitivity.INTERNAL,
                source="stonks-agent-evidence-tool",
                attributes=(
                    ("tool", call.tool_name),
                    ("tool_version", _TOOL_VERSION),
                ),
            ),
            finalized_at=observed_at,
        )
        if isinstance(stored, Failure):
            return stored
        return Success(
            ToolResult(
                call_id=call.call_id,
                artifact_ref=f"sha256:{stored.value.content_hash}",
                content_hash=stored.value.content_hash,
                content_type="application/json",
                byte_count=len(content),
                tool_version=_TOOL_VERSION,
                materialized_evidence_ids=output.materialized_evidence_ids,
                latency_ms=min(int((monotonic() - started) * 1_000), 120_000),
                observed_at=observed_at,
            )
        )


def build_evidence_tool_policy(
    *,
    instrument_ids: frozenset[str],
    evidence_ids: frozenset[UUID],
) -> ToolPolicy:
    return ToolPolicy(
        policy_id="research-evidence-v1",
        principal_profile="research-worker",
        tools=(
            _rule("list_evidence"),
            _rule(
                "read_evidence",
                ToolArgumentSpec(
                    name="evidence_id",
                    kind=ToolArgumentKind.STRING,
                    max_length=36,
                ),
            ),
            _rule(
                "price_window",
                ToolArgumentSpec(
                    name="start",
                    kind=ToolArgumentKind.STRING,
                    max_length=40,
                ),
                ToolArgumentSpec(
                    name="end",
                    kind=ToolArgumentKind.STRING,
                    max_length=40,
                ),
            ),
            _rule("fundamental_snapshot"),
            _rule("filing_history"),
        ),
        allowed_instrument_ids=instrument_ids,
        allowed_evidence_ids=evidence_ids,
    )


def _rule(name: str, *arguments: ToolArgumentSpec) -> ToolRule:
    return ToolRule(
        name=name,
        mutation_class=ToolMutationClass.READ_ONLY,
        arguments=arguments,
        max_timeout_ms=2_000,
        max_output_bytes=64 * 1024,
        audit_enabled=True,
        requires_instrument_scope=True,
        requires_evidence_scope=True,
    )


def _uuid_argument(call: AuthorizedToolCall, name: str) -> UUID | None:
    value = call.arguments.get(name)
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _window(
    call: AuthorizedToolCall,
    as_of: datetime,
) -> Result[tuple[datetime, datetime]]:
    try:
        start = datetime.fromisoformat(
            str(call.arguments["start"]).replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(str(call.arguments["end"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return _failure(ErrorCode.INVALID_INPUT, "Price window is invalid")
    if start.tzinfo is None or end.tzinfo is None or start >= end or end > as_of:
        return _failure(ErrorCode.INVALID_INPUT, "Price window is invalid")
    return Success((start.astimezone(UTC), end.astimezone(UTC)))


def _bar(item: EvidenceItem) -> Result[dict[str, str]]:
    keys = ("open", "high", "low", "close", "volume")
    try:
        values = {key: Decimal(str(item.payload[key])) for key in keys}
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return _failure(ErrorCode.INVALID_INPUT, "Price evidence is invalid")
    if any(not value.is_finite() for value in values.values()):
        return _failure(ErrorCode.INVALID_INPUT, "Price evidence is invalid")
    if (
        values["low"] > values["high"]
        or not values["low"] <= values["open"] <= values["high"]
        or not values["low"] <= values["close"] <= values["high"]
        or values["volume"] < 0
    ):
        return _failure(ErrorCode.INVALID_INPUT, "Price evidence is invalid")
    return Success({key: str(value) for key, value in values.items()})


def _statistics(bars: tuple[dict[str, str], ...]) -> dict[str, str]:
    closes = tuple(Decimal(item["close"]) for item in bars)
    returns = tuple(
        float(current / previous - 1)
        for previous, current in pairwise(closes)
        if previous != 0
    )
    volatility = (
        math.sqrt(
            sum((value - sum(returns) / len(returns)) ** 2 for value in returns)
            / len(returns)
        )
        if returns
        else 0.0
    )
    total_return = closes[-1] / closes[0] - 1 if closes[0] != 0 else Decimal(0)
    return {
        "return": _decimal_text(total_return),
        "volatility": _decimal_text(Decimal(str(volatility))),
        "high": _decimal_text(max(Decimal(item["high"]) for item in bars)),
        "low": _decimal_text(min(Decimal(item["low"]) for item in bars)),
    }


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
