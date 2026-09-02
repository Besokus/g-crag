"""问答反馈服务：记录 👍/👎 + 坏 case 自动 judge + 列表管理 + 一键回流 golden 评测集。

闭环：用户 dislike → 异步 LLM-judge 打分 → 管理员在反馈台确认 → 一键标 golden
      → golden_qa.json 增长 → CI 评测门禁越来越硬（坏 case 永久进入回归）。
"""
import asyncio
import json
from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.models.document import Document
from app.models.feedback import Feedback

_GOLDEN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"
_bg_tasks: set = set()  # 持有后台 task 引用，防 GC

# 结构化来源字段别名（camelCase 前端 → snake_case 存储）
_SOURCE_FIELD_ALIASES = {
    "doc_id": ("docId", "doc_id"),
    "doc_name": ("docName", "doc_name", "name"),
    "doc_type": ("docType", "doc_type"),
    "chunk_id": ("chunkId", "chunk_id"),
    "chunk_idx": ("chunkIdx", "chunk_idx"),
    "score": ("score", "rerankScore", "rerank_score"),
    "chunk": ("text", "chunk", "chunkText", "chunk_text"),
    "retrieval_channels": ("sources", "retrieval_channels", "channels"),
}


def _norm_tenant(tenant_id: str | None) -> str:
    return (tenant_id or "default").strip() or "default"


def _pick_source_field(item: dict, canonical: str):
    for alias in _SOURCE_FIELD_ALIASES[canonical]:
        value = item.get(alias)
        if value is not None and value != "":
            return value
    return None


def normalize_sources(sources, legacy: str = "") -> list[dict]:
    """检索来源归一化：结构化列表(camelCase/snake_case 兼容)优先，旧逗号串兜底。

    结构化条目只保留出现的字段；旧串拆分为 [{"doc_name": ...}]。
    """
    if not sources:
        items: list = []
        for part in str(legacy or "").split(","):
            part = part.strip()
            if part:
                items.append({"doc_name": part})
        return items
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except (TypeError, json.JSONDecodeError):
            sources = [p.strip() for p in sources.split(",") if p.strip()]
    if not isinstance(sources, list):
        return []
    result = []
    for item in sources:
        if isinstance(item, str):
            name = item.strip()
            if name:
                result.append({"doc_name": name})
            continue
        if not isinstance(item, dict):
            continue
        row = {}
        for canonical in _SOURCE_FIELD_ALIASES:
            value = _pick_source_field(item, canonical)
            if value is not None and value != "":
                row[canonical] = value
        if row:
            result.append(row)
    return result


def load_sources_json(raw: str | None) -> list:
    """读回 sources_json：非法 JSON 或非列表一律返回 []（不抛错、不信任形状）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


async def record_feedback(
    db: AsyncSession, *, conversation_id: str, query: str,
    answer: str, feedback: str, username: str, reason: str = "",
    retrieval_sources: str = "",  # 检索命中的文档名列表（逗号分隔，旧客户端兼容）
    trace_id: str = "",
    sources: list | None = None,  # 结构化检索来源（新客户端；优先于 retrieval_sources）
    tenant_id: str = "default",
) -> None:
    structured = normalize_sources(sources or [], "")
    if structured and not retrieval_sources:
        # 旧列保留可读投影（文档名逗号串），追溯与旧报表不断
        retrieval_sources = ", ".join(
            item["doc_name"] for item in structured if item.get("doc_name")
        )
    if not structured and retrieval_sources:
        structured = normalize_sources([], retrieval_sources)
    fb = Feedback(
        conversation_id=conversation_id or "", query=query, answer=answer,
        feedback=feedback, username=username, reason=(reason or "")[:256],
        retrieval_sources=(retrieval_sources or "")[:2000],
        trace_id=(trace_id or "")[:64],
        sources_json=json.dumps(structured, ensure_ascii=False) if structured else "",
        tenant_id=tenant_id or "default",
    )
    db.add(fb)
    await db.commit()
    try:
        from app.core import metrics
        metrics.FEEDBACK.labels(feedback).inc()
    except Exception:
        pass
    # dislike 自动异步打 judge 分 + 检索质量评估（坏 case 沉淀，不阻塞反馈接口）
    if feedback == "dislike" and getattr(settings, "ONLINE_FAITHFULNESS_ENABLE", False):
        try:
            _t = asyncio.create_task(_judge_bg(fb.id, query, answer, retrieval_sources, tenant_id=tenant_id))
            _bg_tasks.add(_t)
            _t.add_done_callback(_bg_tasks.discard)
        except Exception as e:
            degraded("feedback_judge_dispatch", e)


async def _judge_bg(
    feedback_id: str, query: str, answer: str, retrieval_sources: str = "",
    tenant_id: str = "default",
) -> None:
    """后台对 dislike 答案跑 LLM-judge + 检索质量评估，回填 judge_supported/judge_halluc/retrieval_quality。"""
    from app.db.session import AsyncSessionLocal
    from app.rag import judge

    judge_res = None
    retrieval_label = None
    try:
        judge_res = await judge.judge_hallucination(answer, [query], settings.LLM_PROVIDER)
        _h = judge_res.get("hallucination") if judge_res else None
        if isinstance(_h, (int, float)):   # 只在 judge 给出真值时计指标，None 不污染（不假报 1.0）
            try:
                from app.core import metrics
                metrics.HALLUC.observe(_h)
            except Exception:
                pass
    except Exception as e:
        degraded("feedback_judge", e)

    # 有检索来源时，额外评估检索质量
    if retrieval_sources:
        try:
            sources_list = [s.strip() for s in retrieval_sources.split(",") if s.strip()]
            if sources_list:
                ctx_res = await judge.judge_context_relevance(query, sources_list, settings.LLM_PROVIDER)
                score = ctx_res.get("relevance_score", 0.0)
                if score >= 0.7:
                    retrieval_label = "good"
                elif score >= 0.4:
                    retrieval_label = "partial"
                else:
                    retrieval_label = "poor"
        except Exception as e:
            degraded("feedback_retrieval_judge", e)

    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(Feedback).where(
                    Feedback.id == feedback_id,
                    Feedback.tenant_id == _norm_tenant(tenant_id),
                )
            )).scalar_one_or_none()
            if row:
                if judge_res:
                    row.judge_supported = judge_res.get("supported_ratio")
                    row.judge_halluc = judge_res.get("hallucination")
                if retrieval_label:
                    row.retrieval_quality = retrieval_label
                await db.commit()
    except Exception as e:
        degraded("feedback_judge_write", e)


async def list_feedbacks(
    db: AsyncSession, feedback: str = "", page: int = 1, size: int = 20,
    tenant_id: str = "default",
) -> dict:
    """反馈列表（管理台用，可按 like/dislike 过滤；租户域内）。"""
    stmt = select(Feedback).where(Feedback.tenant_id == _norm_tenant(tenant_id))
    cnt = select(func.count()).select_from(Feedback).where(
        Feedback.tenant_id == _norm_tenant(tenant_id)
    )
    if feedback:
        stmt = stmt.where(Feedback.feedback == feedback)
        cnt = cnt.where(Feedback.feedback == feedback)
    total = (await db.execute(cnt)).scalar() or 0
    rows = (
        await db.execute(
            stmt.order_by(desc(Feedback.created_at)).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()
    return {
        "total": total,
        "list": [
            {
                "id": r.id, "query": r.query, "answer": (r.answer or "")[:300],
                "feedback": r.feedback, "reason": r.reason or "",
                "judgeSupported": r.judge_supported, "judgeHalluc": r.judge_halluc,
                "retrievalQuality": r.retrieval_quality,
                "retrievalSources": (r.retrieval_sources or "")[:500],
                "username": r.username,
                "createdAt": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            }
            for r in rows
        ],
    }


async def get_feedback(
    db: AsyncSession, feedback_id: str, tenant_id: str = "default",
) -> dict | None:
    """反馈详情（租户域内；跨租户主键一律视为不存在）。"""
    row = (await db.execute(
        select(Feedback).where(
            Feedback.id == feedback_id,
            Feedback.tenant_id == _norm_tenant(tenant_id),
        )
    )).scalar_one_or_none()
    if not row:
        return None
    return {
        "id": row.id, "conversationId": row.conversation_id,
        "query": row.query, "answer": row.answer or "",
        "feedback": row.feedback, "reason": row.reason or "",
        "judgeSupported": row.judge_supported, "judgeHalluc": row.judge_halluc,
        "retrievalQuality": row.retrieval_quality,
        "retrievalSources": row.retrieval_sources or "",
        "sources": load_sources_json(row.sources_json),
        "traceId": row.trace_id or "",
        "username": row.username,
        "createdAt": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
    }


async def mark_golden(
    db: AsyncSession, feedback_id: str, tenant_id: str = "default",
) -> dict:
    """一键把坏 case 回流到 golden_qa.json（去重），让 CI 评测门禁覆盖它（租户域内）。"""
    fb = (await db.execute(select(Feedback).where(
        Feedback.id == feedback_id,
        Feedback.tenant_id == _norm_tenant(tenant_id),
    ))).scalar_one_or_none()
    if not fb:
        return {"added": False, "reason": "反馈不存在"}
    try:
        items = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")) if _GOLDEN_PATH.exists() else []
    except Exception:
        items = []
    if any((it.get("query") or "").strip() == fb.query.strip() for it in items):
        return {"added": False, "total": len(items), "reason": "该问题已在 golden 集"}
    items.append({"query": fb.query.strip(), "expect": [], "category": "用户反馈", "source": "feedback"})
    _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"added": True, "total": len(items), "query": fb.query.strip()}


async def feedback_stats(db: AsyncSession, tenant_id: str = "default") -> dict:
    """反馈趋势聚合：点赞/点踩分布 + 坏 case 设备聚类 + 高频问题 + 平均幻觉率（租户域内）。"""
    _tenant = _norm_tenant(tenant_id)
    by_fb = (await db.execute(
        select(Feedback.feedback, func.count())
        .where(Feedback.tenant_id == _tenant)
        .group_by(Feedback.feedback)
    )).all()
    fb_map = {r[0]: r[1] for r in by_fb}
    total = sum(fb_map.values())

    # 坏 case 按设备聚类（术语表标准词匹配 query）
    dislike_rows = (await db.execute(
        select(Feedback.query).where(
            Feedback.feedback == "dislike",
            Feedback.tenant_id == _tenant,
        ).order_by(Feedback.created_at.desc()).limit(100)
    )).scalars().all()
    try:
        from app.services.term_service import _load_terms
        std = {w for w in _load_terms().values() if w}
    except Exception:
        std = set()
    device_counts: dict = {}
    for q in dislike_rows:
        for w in std:
            if w in (q or ""):
                device_counts[w] = device_counts.get(w, 0) + 1
    top_devices = sorted(device_counts.items(), key=lambda x: -x[1])[:10]

    # 高频坏 case
    top_bad = (await db.execute(
        select(Feedback.query, func.count()).where(
            Feedback.feedback == "dislike",
            Feedback.tenant_id == _tenant,
        ).group_by(Feedback.query).order_by(func.count().desc()).limit(10)
    )).all()
    # 平均幻觉率（dislike 的 judge 分）
    avg_halluc = (await db.execute(
        select(func.avg(Feedback.judge_halluc)).where(
            Feedback.feedback == "dislike",
            Feedback.tenant_id == _tenant,
        )
    )).scalar()
    # 检索→回答一致性矩阵（2×2：检索好坏 vs 回答好坏）
    cross_rows = (await db.execute(
        select(Feedback.retrieval_quality, Feedback.judge_halluc)
        .where(Feedback.feedback == "dislike")
        .where(Feedback.tenant_id == _tenant)
        .where(Feedback.retrieval_quality.isnot(None))
        .where(Feedback.judge_halluc.isnot(None))
    )).all()
    # 矩阵：{"good_retrieval_good_answer": N, "good_retrieval_bad_answer": N,
    #         "poor_retrieval_good_answer": N, "poor_retrieval_bad_answer": N}
    matrix = {
        "retrieval_good_answer_good": 0,    # ✅ 正常
        "retrieval_good_answer_bad": 0,     # 🔧 生成问题
        "retrieval_poor_answer_good": 0,    # ⚠️ LLM 编造（危险）
        "retrieval_poor_answer_bad": 0,     # ❌ 检索根因
        "retrieval_poor_answer_good_queries": [],  # 编造 case 具体 query
    }
    for rq, hall in cross_rows:
        if rq == "good":
            if hall is not None and hall < 0.3:
                matrix["retrieval_good_answer_good"] += 1
            else:
                matrix["retrieval_good_answer_bad"] += 1
        elif rq in ("poor", "partial"):
            if hall is not None and hall < 0.3:
                matrix["retrieval_poor_answer_good"] += 1
            else:
                matrix["retrieval_poor_answer_bad"] += 1
    # 拉出"检索差但回答好"的具体 query（疑似 LLM 编造）
    if matrix["retrieval_poor_answer_good"] > 0:
        fudge_rows = (await db.execute(
            select(Feedback.query).where(
                Feedback.feedback == "dislike",
                Feedback.tenant_id == _tenant,
                Feedback.retrieval_quality.in_(["poor", "partial"]),
                Feedback.judge_halluc < 0.3,
            ).limit(10)
        )).scalars().all()
        matrix["retrieval_poor_answer_good_queries"] = list(fudge_rows)[:10]

    # 知识盲区：高频 dislike 设备词 × 已上传文档覆盖情况交叉
    coverage_gaps: list[dict] = []
    if top_devices:
        doc_tags_rows = (await db.execute(
            select(Document.doc_name, Document.equipment_tags, Document.doc_type)
            .where(Document.equipment_tags.isnot(None), Document.equipment_tags != "")
            .where(Document.tenant_id == _tenant)
        )).all()
        # 所有文档覆盖的设备词集合
        covered: set[str] = set()
        for _, tags, _ in doc_tags_rows:
            for t in (tags or "").split(","):
                t = t.strip()
                if t:
                    covered.add(t)
        for device, cnt in top_devices:
            is_covered = any(device in c or c in device for c in covered)
            coverage_gaps.append({
                "device": device,
                "dislikeCount": cnt,
                "covered": is_covered,
                "suggestion": "" if is_covered else f"建议上传【{device}】相关运维规程或故障案例",
            })

    return {
        "total": total, "like": fb_map.get("like", 0), "dislike": fb_map.get("dislike", 0),
        "dislikeRate": round(fb_map.get("dislike", 0) / total, 3) if total else 0,
        "topDevices": [{"device": d, "count": c} for d, c in top_devices],
        "topBadCases": [{"query": (q or "")[:60], "count": c} for q, c in top_bad],
        "avgHallucination": round(avg_halluc, 3) if avg_halluc is not None else None,
        "consistencyMatrix": matrix,
        "coverageGaps": coverage_gaps,
    }
