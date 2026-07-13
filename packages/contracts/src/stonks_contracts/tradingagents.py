"""Lease-fenced wire contracts for the isolated TradingAgents worker."""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .common import ArtifactRef, ContractModel, NonEmptyString, Sha256, UTCDateTime
from .research import AgentOpinion, AnalysisBundle

type WorkerProfile = Literal["paper", "backtest", "production"]
type EvidenceCategory = Literal["market", "fundamentals", "news", "sentiment", "macro"]


class SignedEvidenceArtifact(ContractModel):
    """A short-lived capability for exactly one content-addressed evidence item."""

    evidence_id: UUID
    artifact_ref: ArtifactRef
    signed_url: str = Field(min_length=20, max_length=4_096, repr=False)
    expires_at: UTCDateTime
    available_at: UTCDateTime
    category: EvidenceCategory
    untrusted_content: Literal[True] = True

    @field_validator("signed_url")
    @classmethod
    def validate_signed_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or set(query) != {"expires", "signature"}
            or len(query["expires"]) != 1
            or not query["expires"][0].isascii()
            or not query["expires"][0].isdecimal()
            or len(query["signature"]) != 1
            or not 32 <= len(query["signature"][0]) <= 512
        ):
            raise ValueError("signed artifact URL is invalid")
        return value


class TradingAgentsWorkerRequest(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    profile: WorkerProfile
    instrument_id: UUID
    symbol: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9.-]*$",
    )
    as_of: UTCDateTime
    horizon: str = Field(min_length=1, max_length=128)
    allowed_evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=512)
    evidence: tuple[SignedEvidenceArtifact, ...] = Field(min_length=1, max_length=512)
    deadline: UTCDateTime

    @model_validator(mode="after")
    def validate_scope_and_time(self) -> Self:
        allowed = self.allowed_evidence_ids
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(allowed) != len(set(allowed)):
            raise ValueError("allowed evidence ids must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence items must be unique")
        if set(allowed) != set(evidence_ids):
            raise ValueError("evidence items must exactly match request scope")
        if any(item.available_at > self.as_of for item in self.evidence):
            raise ValueError("future evidence is not allowed")
        if any(item.expires_at <= self.as_of for item in self.evidence):
            raise ValueError("artifact capability expires before the research time")
        return self


class TradingAgentsWorkerResult(ContractModel):
    analysis_bundle: AnalysisBundle
    agent_opinion: AgentOpinion


class TradingAgentsWorkerResponse(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    result_artifact_hash: Sha256
    result: TradingAgentsWorkerResult

    @model_validator(mode="after")
    def validate_result_hash(self) -> Self:
        if self.result_artifact_hash != self.result.payload_hash():
            raise ValueError("worker result artifact hash is invalid")
        return self
