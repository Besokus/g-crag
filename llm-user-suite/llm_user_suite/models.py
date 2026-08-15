from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex


class BehaviorSession(Base):
    __tablename__ = "behavior_sessions"
    __table_args__ = (
        Index("ix_behavior_session_lookup", "tenant_id", "user_hash", "last_event_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    user_hash: Mapped[str] = mapped_column(String(64), default="anonymous", index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    has_dislike: Mapped[bool] = mapped_column(Boolean, default=False)
    has_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    has_degradation: Mapped[bool] = mapped_column(Boolean, default=False)
    min_faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)
    signature: Mapped[str] = mapped_column(String(128), default="", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_event_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BehaviorEvent(Base):
    __tablename__ = "behavior_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_event_id", name="uq_behavior_event_source"),
        Index("ix_behavior_event_session_time", "session_id", "occurred_at"),
        Index("ix_behavior_event_kind_time", "kind", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("behavior_sessions.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    user_hash: Mapped[str] = mapped_column(String(64), default="anonymous", index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    qa_trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(12), default="")
    path: Mapped[str] = mapped_column(String(512), default="")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class MetricAggregate(Base):
    __tablename__ = "metric_aggregates"
    __table_args__ = (
        UniqueConstraint("metric_name", "minute", "labels_hash", "source", name="uq_metric_minute"),
        Index("ix_metric_aggregate_time", "metric_name", "minute"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    metric_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    minute: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    labels_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(64), default="otlp")
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    value_sum: Mapped[float] = mapped_column(Float, default=0.0)
    value_min: Mapped[float] = mapped_column(Float, default=0.0)
    value_max: Mapped[float] = mapped_column(Float, default=0.0)
    value_last: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Scenario(Base):
    __tablename__ = "scenarios"
    __table_args__ = (
        UniqueConstraint("tenant_id", "signature", name="uq_scenario_tenant_signature"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ScenarioVersion(Base):
    __tablename__ = "scenario_versions"
    __table_args__ = (UniqueConstraint("scenario_id", "version", name="uq_scenario_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    scenario_id: Mapped[str] = mapped_column(String(64), ForeignKey("scenarios.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_session_ids: Mapped[list] = mapped_column(JSON, default=list)
    prompt_hash: Mapped[str] = mapped_column(String(128), default="")
    last_auto_regression_hash: Mapped[str] = mapped_column(String(64), default="")
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    scenario_version_id: Mapped[str] = mapped_column(String(64), ForeignKey("scenario_versions.id"), index=True)
    environment: Mapped[str] = mapped_column(String(32), default="test", index=True)
    target_base_url: Mapped[str] = mapped_column(String(512), default="")
    credential_profile: Mapped[str] = mapped_column(String(64), default="default")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(24), default="")
    root_cause: Mapped[str] = mapped_column(String(32), default="")
    baseline_run_id: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    runner_job_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    dispatch_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (Index("ix_run_step_order", "run_id", "step_index"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("test_runs.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    intent: Mapped[str] = mapped_column(Text, default="")
    hint_level: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (Index("ix_evaluation_run_dimension", "run_id", "dimension"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("test_runs.id"), index=True)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(24), default="")
    hard_fail: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_hash: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    input_digest: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("test_runs.id"), unique=True, index=True)
    verdict: Mapped[str] = mapped_column(String(24), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    json_path: Mapped[str] = mapped_column(String(512), default="")
    markdown_path: Mapped[str] = mapped_column(String(512), default="")
    html_path: Mapped[str] = mapped_column(String(512), default="")
    callback_status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class EvolutionRetest(Base):
    __tablename__ = "evolution_retests"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_evolution_retest_event"),
        Index("ix_evolution_retest_draft", "draft_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    draft_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    scenario_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    baseline_run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rerun_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), default="received", index=True)
    before_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    after_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    lift: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RawArtifact(Base):
    __tablename__ = "raw_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    object_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    envelope: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class Task(Base):
    __tablename__ = "suite_tasks"
    __table_args__ = (UniqueConstraint("task_type", "idempotency_key", name="uq_suite_task_idem"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    task_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(191), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    locked_by: Mapped[str] = mapped_column(String(128), default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
