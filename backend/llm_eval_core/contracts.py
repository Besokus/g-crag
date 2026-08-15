from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def input_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Evidence:
    kind: str
    value: Any
    trace_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Score:
    dimension: str
    score: float | None
    verdict: str
    reason: str = ""
    hard_fail: bool = False
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    input_digest: str = ""
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class Step:
    index: int
    intent: str
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    assertions: dict[str, Any] = field(default_factory=dict)
    hint_level: int = 0
    success: bool = False
    latency_ms: float | None = None
    trace_id: str = ""


@dataclass(slots=True)
class Case:
    id: str
    scenario_version_id: str
    persona: dict[str, Any]
    goal: str
    hidden_oracle: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Run:
    id: str
    case_id: str
    environment: str
    target: str
    steps: list[Step] = field(default_factory=list)
    status: str = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(slots=True)
class Report:
    run: Run
    scores: list[Score]
    verdict: str
    root_cause: str
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
