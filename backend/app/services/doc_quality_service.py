"""B3: judge 聚合差评文档 → KnowledgeGovernanceIssue(quality_low)。

LLM-judge 离线分（judge_halluc / retrieval_quality）与用户 dislike 只回填
Feedback 表展示，从不回灌改进（B3 断点）。本服务走**路径3**：聚合"高频被
差评文档"自动生成 `KnowledgeGovernanceIssue(issue_type=quality_low)`，进
治理审核台人工兜底。**不动在线 rerank / 检索权重**——零回归风险。

口径：
  - 近 ``DOC_QUALITY_WINDOW_DAYS`` 天的 like/dislike Feedback（有 retrieval_sources）
  - retrieval_sources 是逗号分隔 str（兼容中文逗号），按 doc_name 拆分
  - 每文档：dislike 引用次数 / 总引用次数 = dislike 率
  - dislike 率 ≥ ``DOC_QUALITY_DISLIKE_THRESHOLD`` 且 dislike 次数 ≥
    ``DOC_QUALITY_MIN_COUNT`` → 生成 ``quality_low`` issue

去重：复用 ``KnowledgeGovernanceIssue`` 的 (tenant_id, fingerprint) 唯一约束，
重扫描时 occurrence_count++ 不重复 insert。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.models.document import Document
from app.models.feedback import Feedback
from app.models.knowledge_governance import KnowledgeGovernanceIssue


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _split_sources(raw: str | None) -> list[str]:
    """retrieval_sources 是逗号分隔 str；兼容中英文逗号；空值返回 []。"""
    if not raw:
        return []
    return [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]


async def aggregate_doc_quality(
    db: AsyncSession, tenant: str = "default"
) -> dict[str, int]:
    """扫描近期 Feedback，按文档聚合 dislike 率，超阈值且达最小样本 → 生成 quality_low issue。

    返回 ``{scanned, generated, deduped}``：
      - scanned   : 命中的 Feedback 行数（有 sources 且在窗口内）
      - generated : 本轮新生成的 issue 数
      - deduped   : 命中已存在 issue 的去重计数（occurrence_count++ 而非新增）

    租户隔离说明：Feedback 表无 tenant 列（仅 username），统计为跨租户全表；
    doc_name → doc_id 解析按 ``Document.tenant_id`` 过滤——本租户未命中的
    文档名不生成 issue。与 recompute_fix_rate 同样的"Feedback 跨租户"已知
    限制（Phase 1 Task 1 fix 发现 Feedback 无 tenant 列）。

    开关：``DOC_QUALITY_ISSUE_ENABLE=False`` 直接返回零值结果。
    异常：``degraded("doc_quality_aggregate", e)`` 吞，不阻塞 governance scan 主流程。
    """
    result = {"scanned": 0, "generated": 0, "deduped": 0}
    if not getattr(settings, "DOC_QUALITY_ISSUE_ENABLE", True):
        return result
    try:
        cutoff = _utcnow() - timedelta(days=settings.DOC_QUALITY_WINDOW_DAYS)
        # S1：Feedback 表无 tenant 列，跨租户扫描（doc 查询用 Document.tenant_id 过滤）
        rows = (await db.execute(
            select(Feedback.feedback, Feedback.retrieval_sources).where(
                Feedback.created_at >= cutoff,
                Feedback.retrieval_sources.is_not(None),
                Feedback.retrieval_sources != "",
            )
        )).all()
        result["scanned"] = len(rows)

        # 聚合：doc_name → {dislike, total}
        stats: dict[str, dict[str, int]] = defaultdict(lambda: {"dislike": 0, "total": 0})
        for feedback, sources_raw in rows:
            for doc_name in _split_sources(sources_raw):
                stats[doc_name]["total"] += 1
                if feedback == "dislike":
                    stats[doc_name]["dislike"] += 1

        if not stats:
            return result

        threshold = float(settings.DOC_QUALITY_DISLIKE_THRESHOLD)
        min_count = int(settings.DOC_QUALITY_MIN_COUNT)

        # doc_name → doc_id（按 tenant 过滤；跨租户引用不入本租户 issue）
        doc_names = list(stats.keys())
        doc_rows = (await db.execute(
            select(Document.id, Document.doc_name).where(
                Document.tenant_id == tenant,
                Document.doc_name.in_(doc_names),
            )
        )).all()
        name_to_id: dict[str, str] = {name: did for did, name in doc_rows}

        now = _utcnow()
        for doc_name, counts in stats.items():
            doc_id = name_to_id.get(doc_name)
            if not doc_id:
                continue
            dislike = counts["dislike"]
            total = counts["total"]
            if total == 0 or dislike < min_count:
                continue
            rate = dislike / total
            if rate < threshold:
                continue
            fingerprint = f"quality_low:{doc_id}"
            evidence = {
                "docName": doc_name,
                "dislikeCount": dislike,
                "totalCount": total,
                "rate": round(rate, 3),
                "windowDays": int(settings.DOC_QUALITY_WINDOW_DAYS),
                "explanation": (
                    f"近 {settings.DOC_QUALITY_WINDOW_DAYS} 天内被引用 {total} 次，"
                    f"其中 dislike {dislike} 次（率 {rate:.0%}），需人工复核内容质量。"
                ),
            }
            evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
            existing = (await db.execute(
                select(KnowledgeGovernanceIssue).where(
                    KnowledgeGovernanceIssue.tenant_id == tenant,
                    KnowledgeGovernanceIssue.fingerprint == fingerprint,
                )
            )).scalar_one_or_none()
            if existing is None:
                db.add(KnowledgeGovernanceIssue(
                    tenant_id=tenant,
                    fingerprint=fingerprint,
                    issue_type="quality_low",
                    severity="warning",
                    status="open",
                    doc_id=doc_id,
                    title=f"文档频繁差评：{doc_name}",
                    summary=(
                        f"dislike 率 {rate:.0%}（{dislike}/{total}），"
                        f"超过阈值 {threshold:.0%}。"
                    ),
                    evidence_json=evidence_json,
                    occurrence_count=1,
                    detected_at=now,
                    last_seen_at=now,
                ))
                result["generated"] += 1
            else:
                # 已存在：刷新证据 + occurrence_count++（同 _persist_findings 范式，
                # 不覆盖人工设置的 status / reviewer / review_note）
                existing.evidence_json = evidence_json
                existing.last_seen_at = now
                existing.occurrence_count = (existing.occurrence_count or 0) + 1
                result["deduped"] += 1
        await db.commit()
        return result
    except Exception as e:
        degraded("doc_quality_aggregate", e)
        return result
