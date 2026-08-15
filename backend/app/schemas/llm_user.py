from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LlmUserEvaluationEvent(BaseModel):
    eventId: str = Field(min_length=1, max_length=128)
    runId: str = Field(min_length=1, max_length=128)
    scenarioVersionId: str = Field(min_length=1, max_length=128)
    tenantId: str = Field(default="default", min_length=1, max_length=64)
    outcome: str = Field(max_length=32)
    rootCause: Literal[
        "knowledge_gap", "citation_gap", "no_result", "retrieval",
        "generation", "stability", "test_data", "none",
    ]
    scores: dict[str, float | None] = Field(default_factory=dict)
    query: str = Field(default="", max_length=8000)
    answer: str = Field(default="", max_length=16000)
    reason: str = Field(default="", max_length=2000)
    traceIds: list[str] = Field(default_factory=list, max_length=100)
    evidence: dict = Field(default_factory=dict)
    occurredAt: datetime | None = None
