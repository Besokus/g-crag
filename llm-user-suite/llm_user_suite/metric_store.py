from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import metrics
from .config import settings
from .db import SessionLocal
from .models import MetricAggregate
from .privacy import redact_text


def _minute(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(second=0, microsecond=0)


def _labels(value: dict[str, Any] | None) -> dict[str, str]:
    allowed = settings.metric_label_allowlist()
    return {
        str(key)[:64]: redact_text(str(item), limit=256)
        for key, item in (value or {}).items()
        if str(key) in allowed
    }


def _hash_labels(value: dict[str, str]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def record_metric(
    db: AsyncSession, name: str, value: float, *, labels: dict | None = None,
    occurred_at: datetime | None = None, source: str = "otlp",
) -> bool:
    if name not in settings.metric_allowlist():
        metrics.METRIC_POINTS.labels("not_allowed").inc()
        return False
    safe_labels = _labels(labels)
    minute = _minute(occurred_at)
    labels_hash = _hash_labels(safe_labels)
    row = (await db.execute(select(MetricAggregate).where(
        MetricAggregate.metric_name == name,
        MetricAggregate.minute == minute,
        MetricAggregate.labels_hash == labels_hash,
        MetricAggregate.source == source,
    ))).scalar_one_or_none()
    number = float(value)
    if row:
        row.sample_count += 1
        row.value_sum += number
        row.value_min = min(row.value_min, number)
        row.value_max = max(row.value_max, number)
        row.value_last = number
    else:
        db.add(MetricAggregate(
            metric_name=name, minute=minute, labels_hash=labels_hash,
            labels=safe_labels, source=source, sample_count=1,
            value_sum=number, value_min=number, value_max=number, value_last=number,
        ))
    metrics.METRIC_POINTS.labels("accepted").inc()
    return True


def _attrs(values: list[dict] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values or []:
        value = item.get("value") or {}
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                result[str(item.get("key", ""))] = value[key]
                break
    return result


def _timestamp(point: dict) -> datetime | None:
    nanos = point.get("timeUnixNano")
    if nanos and str(nanos).isdigit():
        return datetime.fromtimestamp(int(nanos) / 1_000_000_000, UTC)
    return None


def extract_otlp_metric_points(document: dict) -> list[tuple[str, float, dict, datetime | None]]:
    points: list[tuple[str, float, dict, datetime | None]] = []
    allowed = settings.metric_allowlist()
    for resource in document.get("resourceMetrics", []):
        resource_attrs = _attrs(resource.get("resource", {}).get("attributes"))
        for scope in resource.get("scopeMetrics", resource.get("instrumentationLibraryMetrics", [])):
            for metric in scope.get("metrics", []):
                name = str(metric.get("name", ""))
                if name not in allowed:
                    continue
                for data_kind in ("gauge", "sum"):
                    for point in (metric.get(data_kind) or {}).get("dataPoints", []):
                        raw = point.get("asDouble", point.get("asInt"))
                        if raw is None:
                            continue
                        points.append((name, float(raw), {**resource_attrs, **_attrs(point.get("attributes"))}, _timestamp(point)))
                for data_kind in ("histogram", "summary"):
                    for point in (metric.get(data_kind) or {}).get("dataPoints", []):
                        count = float(point.get("count") or 0)
                        total = point.get("sum")
                        if total is None:
                            continue
                        value = float(total) / count if count > 0 else float(total)
                        points.append((name, value, {**resource_attrs, **_attrs(point.get("attributes"))}, _timestamp(point)))
    return points


async def ingest_otlp_metrics(db: AsyncSession, document: dict, *, source: str = "otlp") -> int:
    accepted = 0
    for name, value, labels, occurred_at in extract_otlp_metric_points(document)[:5000]:
        accepted += 1 if await record_metric(
            db, name, value, labels=labels, occurred_at=occurred_at, source=source,
        ) else 0
    await db.commit()
    return accepted


async def scrape_prometheus() -> dict:
    if not settings.METRICS_SCRAPE_URL:
        return {"status": "disabled", "accepted": 0}
    accepted = 0
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(settings.METRICS_SCRAPE_URL)
        response.raise_for_status()
    async with SessionLocal() as db:
        allowed = settings.metric_allowlist()
        for family in text_string_to_metric_families(response.text):
            if family.name not in allowed and not any(
                sample.name in allowed for sample in family.samples
            ):
                continue
            if family.name in allowed and family.type in {"histogram", "summary"}:
                grouped: dict[str, dict] = {}
                for sample in family.samples:
                    if not sample.name.endswith(("_sum", "_count")):
                        continue
                    labels = _labels(sample.labels)
                    key = json.dumps(labels, ensure_ascii=False, sort_keys=True)
                    grouped.setdefault(key, {"labels": labels})["sum" if sample.name.endswith("_sum") else "count"] = float(sample.value)
                for values in grouped.values():
                    count = values.get("count", 0.0)
                    if "sum" not in values:
                        continue
                    value = values["sum"] / count if count > 0 else values["sum"]
                    accepted += 1 if await record_metric(
                        db, family.name, value, labels=values["labels"], source="prometheus",
                    ) else 0
                continue
            for sample in family.samples:
                if sample.name.endswith("_bucket"):
                    continue
                name = sample.name if sample.name in allowed else family.name
                if name not in allowed:
                    continue
                accepted += 1 if await record_metric(
                    db, name, float(sample.value), labels=sample.labels, source="prometheus",
                ) else 0
        await db.commit()
    metrics.METRIC_SCRAPES.labels("success").inc()
    return {"status": "success", "accepted": accepted}
