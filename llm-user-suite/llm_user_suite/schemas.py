from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class BehaviorEventIn(BaseModel):
    eventId: str = ""
    kind: str
    tenantId: str = "default"
    userId: str = ""
    userHash: str = ""
    sessionId: str = ""
    conversationId: str = ""
    traceId: str = ""
    qaTraceId: str = ""
    method: str = ""
    path: str = ""
    statusCode: int | None = None
    durationMs: float | None = None
    occurredAt: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ScenarioStage(BaseModel):
    intent: str
    businessHint: str = ""
    apiHint: str = ""
    requestTemplate: dict[str, Any] = Field(default_factory=dict)
    completion: dict[str, Any] = Field(default_factory=dict)
    maxAttempts: int = Field(default=4, ge=1, le=4)
    delayMs: int = Field(default=0, ge=0, le=300_000)


class ScenarioSpec(BaseModel):
    name: str
    persona: dict[str, Any] = Field(default_factory=dict)
    goal: str
    preconditions: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    stages: list[ScenarioStage]
    hiddenOracle: dict[str, Any] = Field(default_factory=dict)
    cleanup: list[dict[str, Any]] = Field(default_factory=list)
    safety: dict[str, Any] = Field(default_factory=dict)

    @field_validator("stages")
    @classmethod
    def non_empty_stages(cls, value):
        if not value:
            raise ValueError("scenario requires at least one stage")
        return value


class ScenarioApproval(BaseModel):
    action: Literal["approve", "retire"]


class RunCreate(BaseModel):
    scenarioVersionId: str
    targetBaseUrl: str = ""
    environment: str = "test"
    credentialProfile: str = "default"
    baselineRunId: str = ""


class EvolutionIndexedEvent(BaseModel):
    eventId: str = Field(min_length=1, max_length=128)
    draftId: str = Field(min_length=1, max_length=64)
    runId: str = Field(min_length=1, max_length=64)
    scenarioVersionId: str = Field(min_length=1, max_length=64)
    tenantId: str = Field(default="default", min_length=1, max_length=64)
    status: Literal["indexed"] = "indexed"
    indexedAt: datetime | None = None


class RawArtifactCreate(BaseModel):
    tenantId: str = Field(default="default", max_length=64)
    sessionId: str = Field(default="", max_length=64)
    contentType: str = Field(default="application/octet-stream", max_length=128)
    contentBase64: str = Field(min_length=1, max_length=20_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationCallback(BaseModel):
    eventId: str
    runId: str
    scenarioVersionId: str
    tenantId: str = "default"
    outcome: str
    rootCause: Literal[
        "knowledge_gap", "citation_gap", "no_result", "retrieval",
        "generation", "stability", "test_data", "none",
    ]
    scores: dict[str, float | None] = Field(default_factory=dict)
    query: str = ""
    answer: str = ""
    reason: str = ""
    traceIds: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    occurredAt: datetime | None = None
