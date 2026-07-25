# 数据链路闭环断点修复 — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接通数据飞轮的两个死指标（FIX_RATE/KB_FRESHNESS）+ 补全队列两处漏料收口（CRAG refused / stream 无结果）+ 前端来源筛选。

**Architecture:** 纯接线/收口，零新底座。复用既有 `evidence_gap_service.collect`（去重）、`knowledge_governance_service.run_scan`（周期钩子）、后台 cron loop 范式（仿 `evolution_cron_loop`）。全部 opt-in 开关，默认安全值。不动在线回答内容。

**Tech Stack:** Python 3 + FastAPI + SQLAlchemy(async) + Redis + prometheus_client；Vue 3 + Element-free。

**对应 spec:** `docs/superpowers/specs/2026-07-25-data-loop-breakpoint-fix-design.md` §4（Phase 1）。

## Global Constraints

- 测试运行：`cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/<file> -v`（Windows venv，PYTHONPATH=backend）。
- prometheus 指标 label 逗号无空格，Histogram buckets 用元组。
- 改完源码必须 `docker compose build grid-backend && docker compose up -d`（源码 bake 进镜像，无 bind mount）。
- 路由前缀 `/api`；端口 8001。
- 新指标必须在 `metrics.py` 的 `init_metric_series()` 预注册 0 值（避免事件驱动指标未触碰前在 /metrics 隐身）。
- 新增配置项必须同步 `backend/app/config.py` + `.env`（项目 P0 要求 .env 字段对齐）。
- degraded() 吞异常范式：`from app.core.obs import degraded`。
- commit message 中文 + `feat/fix/docs/test(scope)` 前缀，参考 recent commits。

---

## File Structure（Phase 1）

| 文件 | 责任 | 动作 |
|------|------|------|
| `backend/app/services/feedback_fix_rate_service.py` | FIX_RATE 周期聚合算率 | 新增 |
| `backend/app/services/knowledge_governance_service.py` | KB_FRESHNESS 在 scan 末尾 set | 修改 |
| `backend/app/services/qa_service.py` | B4/B7 收口点 collect | 修改 |
| `backend/app/services/evidence_gap_service.py` | list_gaps 加 source 过滤 | 修改 |
| `backend/app/routers/system.py` | evidence_gap_list 透传 source | 修改 |
| `backend/app/core/metrics.py` | 新指标预注册（如需） | 修改 |
| `backend/app/config.py` | 5 个新开关 | 修改 |
| `backend/app/main.py` | fix_rate_cron_loop lifespan | 修改 |
| `frontend/src/views/Admin.vue` | source 筛选下拉 | 修改 |
| `tests/test_feedback_fix_rate.py` | B1 测试 | 新增 |
| `tests/test_kb_freshness.py` | B2 测试 | 新增 |
| `tests/test_crag_refused_to_gap.py` | B4/B7 测试 | 新增 |

---

## Task 1: B1 — config 开关 + FIX_RATE 聚合服务

**Files:**
- Modify: `backend/app/config.py`
- Create: `backend/app/services/feedback_fix_rate_service.py`
- Test: `tests/test_feedback_fix_rate.py`

**Interfaces:**
- Produces: `feedback_fix_rate_service.recompute_fix_rate(tenant: str) -> float`（返回 0~1 修复率，异常返回 None）

- [ ] **Step 1: config.py 加 3 个开关**

在 `backend/app/config.py` 的 Settings 类中（紧邻 `ONLINE_FAITHFULNESS_ENABLE` 等开关处）追加：

```python
    # 数据飞轮修复率（B1）
    FIX_RATE_ENABLE: bool = True
    FIX_RATE_WINDOW_DAYS: int = 30
    FIX_RATE_CRON_MINUTES: int = 30
```

- [ ] **Step 2: 写失败测试**

`tests/test_feedback_fix_rate.py`：

```python
"""B1: 坏case修复率聚合（dislike→补全→同query再like）。"""
import pytest
from datetime import datetime, timedelta, timezone

from app.services import feedback_fix_rate_service as svc


@pytest.mark.asyncio
async def test_recompute_full_cycle(monkeypatch, tmp_path):
    """dislike → synced → like 完整链：修复率 = 1/1 = 1.0。"""
    from app.db.session import AsyncSessionLocal
    from app.models.feedback import Feedback
    from app.models.evidence_gap import EvidenceGap
    from app.services import term_service

    nq = term_service.normalize("主变压器温度异常怎么处理")
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=1)))
        db.add(EvidenceGap(query=nq, status="synced", confidence="refused",
                           original_answer="", grade="", crag_action="", tenant="default"))
        db.add(Feedback(query=nq, feedback="like",
                        created_at=datetime.now(timezone.utc)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 1.0


@pytest.mark.asyncio
async def test_recompute_dislike_only():
    """只 dislike 未补全：分子 0 → rate = 0.0。"""
    from app.db.session import AsyncSessionLocal
    from app.models.feedback import Feedback
    from app.services import term_service

    nq = term_service.normalize("SF6断路器漏气怎么办")
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 0.0


@pytest.mark.asyncio
async def test_recompute_window_excludes_old():
    """window 外的 dislike（>30天）不计入分母。"""
    from app.db.session import AsyncSessionLocal
    from app.models.feedback import Feedback
    from app.services import term_service

    nq = term_service.normalize("老旧问题")
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=40)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 0.0  # 分母 0 → 返回 0（不除零）
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_feedback_fix_rate.py -v`
Expected: FAIL — `ModuleNotFoundError: feedback_fix_rate_service`（或 recompute_fix_rate 不存在）

- [ ] **Step 4: 实现 feedback_fix_rate_service.py**

`backend/app/services/feedback_fix_rate_service.py`：

```python
"""B1: 坏case修复率聚合 — dislike→补全回流→同query再like 的比率。

周期 cron 调 recompute_fix_rate → metrics.FEEDBACK_FIX_RATE.set(rate)。
Grafana data-flywheel.json 已有面板引用 grid_feedback_fix_rate，set 即亮。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.evidence_gap import EvidenceGap
from app.models.feedback import Feedback
from app.models.knowledge_evolution import KnowledgeEvolutionDraft


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def recompute_fix_rate(tenant: str = "default") -> float:
    """返回近 WINDOW_DAYS 的修复率 = |dislike且已补全且再被like| / |dislike unique nq|。

    Feedback.query 是原始 query；EvidenceGap.query/Draft.member_queries 是 nq。
    匹配前必须 normalize Feedback.query（复用 term_service.normalize）。
    分母 0 → 返回 0.0（不除零）。异常返回 None（caller 保持上次值）。
    """
    from app.services.term_service import normalize as _norm
    try:
        cutoff = _utcnow() - timedelta(days=settings.FIX_RATE_WINDOW_DAYS)
        async with AsyncSessionLocal() as db:
            # S1：近 window 天的 dislike unique nq
            dislike_rows = (await db.execute(
                select(Feedback.query).where(
                    Feedback.feedback == "dislike",
                    Feedback.created_at >= cutoff,
                )
            )).scalars().all()
            s1 = {_norm(q or "") for q in dislike_rows if q and q.strip()}
            s1.discard("")
            if not s1:
                return 0.0

            # S2：已补全回流（evidence_gap.synced ∪ evolution.indexed）
            synced = (await db.execute(
                select(EvidenceGap.query).where(
                    EvidenceGap.query.in_(s1),
                    EvidenceGap.status == "synced",
                )
            )).scalars().all()
            s2 = set(synced)
            # evolution draft：member_queries_json 含 S1 中任一 nq 且 status=indexed
            indexed_drafts = (await db.execute(
                select(KnowledgeEvolutionDraft.member_queries_json).where(
                    KnowledgeEvolutionDraft.status == "indexed",
                )
            )).scalars().all()
            import json as _json
            for mq_raw in indexed_drafts:
                try:
                    members = set(_json.loads(mq_raw or "[]"))
                except Exception:
                    members = set()
                s2 |= (members & s1)

            if not s2:
                return 0.0

            # S3：S2 中的 nq 后续被 like
            like_rows = (await db.execute(
                select(Feedback.query).where(
                    Feedback.feedback == "like",
                    Feedback.created_at >= cutoff,
                )
            )).scalars().all()
            like_nqs = {_norm(q or "") for q in like_rows if q and q.strip()}
            s3 = s2 & like_nqs
            return round(len(s3) / len(s1), 3)
    except Exception as e:
        degraded("fix_rate_recompute", e)
        return None  # type: ignore[return-value]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_feedback_fix_rate.py -v`
Expected: 3 PASS

- [ ] **Step 6: commit**

```bash
git add backend/app/config.py backend/app/services/feedback_fix_rate_service.py tests/test_feedback_fix_rate.py
git commit -m "feat(data-loop): B1 FIX_RATE聚合服务+config开关(周期算dislike→补全→再like比率)"
```

---

## Task 2: B1 — main.py lifespan 起 fix_rate cron loop + set 指标

**Files:**
- Modify: `backend/app/main.py`（在 lifespan startup 注册 loop，仿 `evolution_cron_loop`）
- Test: 集成验证（无新测试文件，靠 Task 1 测试 + 手动 /metrics）

**Interfaces:**
- Consumes: `feedback_fix_rate_service.recompute_fix_rate(tenant)`

- [ ] **Step 1: 定位 evolution_cron_loop 注册处**

在 `backend/app/main.py` 的 lifespan startup 段，grep `evolution_cron_loop` 找到现有注册点（通常形如 `asyncio.create_task(evolution_cron_loop("default"))`）。

- [ ] **Step 2: 新增 fix_rate_cron_loop 函数**

在 `feedback_fix_rate_service.py` 末尾追加 loop：

```python
async def fix_rate_cron_loop(tenant: str = "default"):
    """定时重算修复率 → metrics.FEEDBACK_FIX_RATE.set。

    lifespan 启动；周期=FIX_RATE_CRON_MINUTES，FIX_RATE_ENABLE=False 或周期<=0 时关闭。
    """
    import asyncio
    if not getattr(settings, "FIX_RATE_ENABLE", True):
        return
    interval = float(getattr(settings, "FIX_RATE_CRON_MINUTES", 30))
    if interval <= 0:
        return
    last_rate: float | None = None
    while True:
        await asyncio.sleep(interval * 60)
        try:
            rate = await recompute_fix_rate(tenant)
            if rate is None:
                rate = last_rate or 0.0   # 异常保持上次值
            else:
                last_rate = rate
            from app.core import metrics
            metrics.FEEDBACK_FIX_RATE.set(rate)
        except Exception as e:
            degraded("fix_rate_cron_loop", e)
```

- [ ] **Step 3: main.py 注册 loop**

在 lifespan startup（evolution_cron_loop 注册旁）加：

```python
        from app.services.feedback_fix_rate_service import fix_rate_cron_loop
        _bg.add(asyncio.create_task(fix_rate_cron_loop("default")))
```

（`_bg` 为 main.py 现有的后台 task 持有集合；若该处变量名不同，对齐现有命名。）

- [ ] **Step 4: 手动验证**

Run: `docker compose build grid-backend && docker compose up -d grid-backend`
启动后等待 `FIX_RATE_CRON_MINUTES`（或临时改 config 为 1 分钟加速），curl：
```bash
curl -s http://127.0.0.1:8001/metrics | findstr feedback_fix_rate
```
Expected: 输出 `grid_feedback_fix_rate <数值>`（不再是 init 的 0）。

- [ ] **Step 5: commit**

```bash
git add backend/app/services/feedback_fix_rate_service.py backend/app/main.py
git commit -m "feat(data-loop): B1 fix_rate_cron_loop接入main lifespan+set指标"
```

---

## Task 3: B2 — KB_FRESHNESS 在 governance scan 末尾 set

**Files:**
- Modify: `backend/app/services/knowledge_governance_service.py`（`run_scan` 末尾 + 新 `_set_freshness_metric`）
- Test: `tests/test_kb_freshness.py`

**Interfaces:**
- Produces: `_set_freshness_metric(db, tenant)`（内部函数，set `metrics.KB_FRESHNESS`）

- [ ] **Step 1: 写失败测试**

`tests/test_kb_freshness.py`：

```python
"""B2: KB_FRESHNESS = active未过期文档占比。"""
import pytest
from datetime import datetime, timedelta, timezone

from app.services import knowledge_governance_service as gov


@pytest.mark.asyncio
async def test_freshness_active_ratio():
    """3 文档：1 active未过期 + 1 expired + 1 draft → freshness = 1/3 ≈ 0.333。"""
    from app.db.session import AsyncSessionLocal
    from app.models.document import Document
    from app.models.knowledge_governance import KnowledgeDocumentMetadata

    async with AsyncSessionLocal() as db:
        # 文档
        d1, d2, d3 = Document(doc_name="d1", minio_object="x1"), \
                     Document(doc_name="d2", minio_object="x2"), \
                     Document(doc_name="d3", minio_object="x3")
        db.add_all([d1, d2, d3])
        await db.flush()
        now = datetime.now(timezone.utc)
        db.add(KnowledgeDocumentMetadata(
            doc_id=d1.id, version_status="active", is_permanent=True))
        db.add(KnowledgeDocumentMetadata(
            doc_id=d2.id, version_status="active",
            expires_at=now - timedelta(days=1)))   # 已过期
        db.add(KnowledgeDocumentMetadata(
            doc_id=d3.id, version_status="draft"))  # 非 active
        await db.commit()

        rate = await gov._set_freshness_metric(db, "default")
        assert rate == round(1 / 3, 3)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_kb_freshness.py -v`
Expected: FAIL — `_set_freshness_metric` 不存在

- [ ] **Step 3: 实现 _set_freshness_metric + 挂 run_scan**

在 `knowledge_governance_service.py` 加（import 段已有 `func/select/Document/KnowledgeDocumentMetadata`）：

```python
async def _set_freshness_metric(db: AsyncSession, tenant_id: str) -> float:
    """B2: active未过期文档占比 → metrics.KB_FRESHNESS.set。返回占比。

    active = version_status='active' 且 (is_permanent 或 expires_at is null 或 expires_at>now)。
    占比 = active数 / Document总文档数。异常 degraded 不崩。
    """
    try:
        from datetime import datetime, timezone
        from app.core import metrics
        now = datetime.now(timezone.utc)
        total = (await db.execute(
            select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id)
        )).scalar() or 0
        if total == 0:
            metrics.KB_FRESHNESS.set(0)
            return 0.0
        active = (await db.execute(
            select(func.count()).select_from(KnowledgeDocumentMetadata).where(
                KnowledgeDocumentMetadata.tenant_id == tenant_id,
                KnowledgeDocumentMetadata.version_status == "active",
                (KnowledgeDocumentMetadata.is_permanent.is_(True))
                | (KnowledgeDocumentMetadata.expires_at.is_(None))
                | (KnowledgeDocumentMetadata.expires_at > now),
            )
        )).scalar() or 0
        rate = round(active / total, 3)
        metrics.KB_FRESHNESS.set(rate)
        return rate
    except Exception as e:
        degraded("kb_freshness_set", e)
        return 0.0
```

在 `run_scan(...)` 函数 `return` 前（成功路径末尾）加：

```python
    await _set_freshness_metric(db, tenant_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_kb_freshness.py -v`
Expected: 1 PASS

- [ ] **Step 5: commit**

```bash
git add backend/app/services/knowledge_governance_service.py tests/test_kb_freshness.py
git commit -m "feat(data-loop): B2 KB_FRESHNESS接线(governance scan末尾set active未过期占比)"
```

---

## Task 4: B4 + B7 — CRAG refused / stream 无结果 自动入 evidence_gap

**Files:**
- Modify: `backend/app/config.py`（加 `CRAG_REFUSED_TO_GAP_ENABLE`）
- Modify: `backend/app/services/qa_service.py`（3 处收口点）
- Test: `tests/test_crag_refused_to_gap.py`

**Interfaces:**
- Consumes: `evidence_gap_service.collect(query, answer, confidence, grade, action, source, tenant)`

- [ ] **Step 1: config.py 加开关**

```python
    # CRAG refused / 无结果 自动入证据补全队列（B4/B7）
    CRAG_REFUSED_TO_GAP_ENABLE: bool = True
```

- [ ] **Step 2: 写失败测试**

`tests/test_crag_refused_to_gap.py`：

```python
"""B4/B7: CRAG refused 与 stream 无结果 自动入 evidence_gap。"""
import pytest

from app.services import qa_service


@pytest.mark.asyncio
async def test_refused_collects_to_gap(monkeypatch):
    """confidence=refused → collect 被调用，source=auto_crag。"""
    called = {}

    async def fake_collect(query, answer, confidence, grade, action, source="auto", tenant="default"):
        called["args"] = dict(query=query, confidence=confidence,
                              grade=grade, action=action, source=source, tenant=tenant)
        return 1

    monkeypatch.setattr("app.services.qa_service.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", True, raising=False)

    # 直接调内部收口函数（见 Step 4 抽出的 helper）
    await qa_service._maybe_collect_refused(
        nq="主变异常", answer="", confidence="refused",
        grade="incorrect", action="rewritten_failed", tenant="default")
    assert called["args"]["source"] == "auto_crag"
    assert called["args"]["confidence"] == "refused"


@pytest.mark.asyncio
async def test_non_refused_no_collect(monkeypatch):
    """confidence=high → 不调 collect。"""
    called = []
    async def fake_collect(*a, **kw):
        called.append(1); return 1
    monkeypatch.setattr("app.services.qa_service.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", True, raising=False)

    await qa_service._maybe_collect_refused(
        nq="x", answer="", confidence="high",
        grade="correct", action="normal", tenant="default")
    assert called == []
```

- [ ] **Step 3: 运行确认失败**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_crag_refused_to_gap.py -v`
Expected: FAIL — `_maybe_collect_refused` 不存在

- [ ] **Step 4: 实现 helper + 3 处收口点**

在 `qa_service.py` 加 helper（放在 `_crag_correct` 附近）：

```python
async def _maybe_collect_refused(
    *, nq: str, answer: str, confidence: str,
    grade: str, action: str, tenant: str = "default",
) -> None:
    """B4/B7：refused / 无结果 自动入证据补全队列（fire-and-forget 由 caller 决定）。

    触发条件：confidence=='refused' 或 action in {rewritten_failed, refused} 或 contexts 空(no_recall)。
    collect 本身去重（同 query pending 跳过），多次调用安全。
    开关 CRAG_REFUSED_TO_GAP_ENABLE 关时整体跳过。
    """
    if not getattr(settings, "CRAG_REFUSED_TO_GAP_ENABLE", True):
        return
    is_refused = confidence == "refused" or action in ("rewritten_failed", "refused")
    if not is_refused:
        return
    source = "auto_no_recall" if (grade == "incorrect" and not action) else "auto_crag"
    try:
        from app.services import evidence_gap_service
        await evidence_gap_service.collect(
            nq, answer or "", confidence or "refused",
            grade or "", action or "", source, tenant or "default")
    except Exception as e:
        degraded("crag_refused_to_gap", e)
```

**收口点 1（非流式 answer，`_crag_correct` 返回后）**：在 `answer()` 里 `contexts, confidence, crag_action, crag_grade, crag_extras = await _crag_correct(...)`（qa_service.py:644 附近）之后插入：

```python
    if confidence == "refused" or crag_action in ("rewritten_failed", "refused"):
        _bg_tasks.add(asyncio.create_task(_maybe_collect_refused(
            nq=nq, answer="", confidence=confidence,
            grade=crag_grade, action=crag_action, tenant=tenant or "default")))
```

**收口点 2（流式 stream_answer，`_crag_correct` 后）**：在 `stream_answer` 的 `contexts, confidence, crag_action, crag_grade, crag_extras = await _crag_correct(...)`（qa_service.py:1148 附近）之后插入同上逻辑（stream 路径用同一 helper）。

**收口点 3（B7 stream 无结果分支）**：在 `stream_answer` 的 `if not contexts:` 分支（qa_service.py:1143）的 `yield {...done...}` 之前插入：

```python
        if getattr(settings, "CRAG_REFUSED_TO_GAP_ENABLE", True):
            _bg_tasks.add(asyncio.create_task(_maybe_collect_refused(
                nq=nq, answer="", confidence="refused",
                grade="incorrect", action="", tenant=tenant or "default")))
```

> 注：`_bg_tasks` 是 qa_service.py 模块级已有的后台 task 集合（见文件顶部 `_bg_tasks: set = set()`）。若该处变量名不同，对齐现有命名。

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_crag_refused_to_gap.py tests/test_confidence_refinement.py -v`
Expected: 新测试 2 PASS + 既有 CRAG 测试零回归。

- [ ] **Step 6: commit**

```bash
git add backend/app/config.py backend/app/services/qa_service.py tests/test_crag_refused_to_gap.py
git commit -m "feat(data-loop): B4+B7 CRAG refused与stream无结果自动入证据补全队列(source=auto_crag/auto_no_recall)"
```

---

## Task 5: F1 — 前端 + 后端 证据补全列表 source 筛选

**Files:**
- Modify: `backend/app/services/evidence_gap_service.py`（`list_gaps` 加 source 参数）
- Modify: `backend/app/routers/system.py`（`evidence_gap_list` 透传 source）
- Modify: `frontend/src/views/Admin.vue`（source 下拉 + 绑 loadEvidenceGaps）
- Test: 后端 list_gaps source 过滤

**Interfaces:**
- Produces: `list_gaps(status, source, page, size)` 签名扩展

- [ ] **Step 1: 写后端失败测试**

追加到 `tests/test_crag_refused_to_gap.py`（或新建 `tests/test_evidence_gap_filter.py`）：

```python
@pytest.mark.asyncio
async def test_list_gaps_filter_by_source():
    """list_gaps(source='auto_crag') 只返回该来源。"""
    from app.services import evidence_gap_service as eg
    from app.db.session import AsyncSessionLocal
    from app.models.evidence_gap import EvidenceGap

    async with AsyncSessionLocal() as db:
        db.add(EvidenceGap(query="q_crag", status="pending", confidence="refused",
                           source="auto_crag", original_answer="", grade="", crag_action=""))
        db.add(EvidenceGap(query="q_manual", status="pending", confidence="medium",
                           source="manual", original_answer="", grade="", crag_action=""))
        await db.commit()

    res = await eg.list_gaps(status="pending", source="auto_crag")
    queries = [g["query"] for g in res["list"]]
    assert "q_crag" in queries and "q_manual" not in queries
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_evidence_gap_filter.py -v`
Expected: FAIL — `list_gaps() got an unexpected keyword argument 'source'`

- [ ] **Step 3: 后端 list_gaps 加 source**

`evidence_gap_service.py` 的 `list_gaps` 签名改为：

```python
async def list_gaps(status: str | None = None, source: str | None = None,
                    page: int = 1, size: int = 20) -> dict:
```

在现有 `if status:` 过滤块后追加：

```python
            if source:
                q = q.where(EvidenceGap.source == source)
                cq = cq.where(EvidenceGap.source == source)
```

- [ ] **Step 4: 路由透传 source**

`routers/system.py` 的 `evidence_gap_list`：

```python
@router.get("/evidence-gap")
async def evidence_gap_list(
    status: str | None = None,
    source: str | None = None,
    page: int = 1, size: int = 20,
    admin: User = Depends(require_admin),
):
    from app.services.evidence_gap_service import list_gaps
    return success(await list_gaps(status, source, page, size), "查询成功")
```

- [ ] **Step 5: 运行后端测试通过**

Run: `cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_evidence_gap_filter.py -v`
Expected: 1 PASS

- [ ] **Step 6: 前端 Admin.vue 加 source 下拉**

定位证据补全列表的过滤区（`egFilter` 附近，约 Admin.vue:1008）。在现有 status 筛选旁加：

```vue
<select v-model="egSourceFilter">
  <option value="">全部来源</option>
  <option value="auto">自动·无结果</option>
  <option value="auto_crag">自动·CRAG refused</option>
  <option value="auto_no_recall">自动·流式无结果</option>
  <option value="overconfident">自动·过自信</option>
  <option value="manual">用户上报</option>
</select>
```

script 段加：
```javascript
const egSourceFilter = ref('')
// loadEvidenceGaps 的 params 追加 source: egSourceFilter.value || undefined
```

- [ ] **Step 7: rebuild + 手动验证**

Run: `docker compose build grid-backend grid-frontend && docker compose up -d`
登录 Admin → 证据补全，确认 source 下拉筛选生效。

- [ ] **Step 8: commit**

```bash
git add backend/app/services/evidence_gap_service.py backend/app/routers/system.py frontend/src/views/Admin.vue tests/test_evidence_gap_filter.py
git commit -m "feat(data-loop): F1 证据补全列表加source筛选(auto_crag/auto_no_recall/manual...)"
```

---

## 收口验证（Phase 1 整体）

- [ ] 跑全量飞轮指标测试：`cd backend && set PYTHONPATH=backend && .\venv\Scripts\pytest.exe tests/test_flywheel_metrics.py tests/test_feedback_fix_rate.py tests/test_kb_freshness.py tests/test_crag_refused_to_gap.py tests/test_evidence_gap_filter.py -v`
- [ ] rebuild 后台：`docker compose build grid-backend grid-frontend && docker compose up -d`
- [ ] curl 指标验证：
  - `curl -s http://127.0.0.1:8001/metrics | findstr "feedback_fix_rate kb_freshness"`（等待 cron 周期后有真实值）
- [ ] Grafana data-flywheel 看板：fix rate / freshness 面板有曲线。
- [ ] 证据补全列表：出现 `auto_crag`/`auto_no_recall` 来源 + 可筛选。

---

## Self-Review（写完后自查）

**Spec 覆盖（§4 Phase 1）**：
- B1 FIX_RATE → Task 1 + 2 ✓
- B2 KB_FRESHNESS → Task 3 ✓
- B4 CRAG refused → Task 4 ✓
- B7 stream 无结果 → Task 4 收口点 3 ✓
- F1 前端 source 筛选 → Task 5 ✓

**Placeholder 扫描**：无 TBD/TODO；每步含完整代码或确切命令。

**类型/命名一致性**：`recompute_fix_rate(tenant)` / `_set_freshness_metric(db, tenant_id)` / `_maybe_collect_refused(*, nq, answer, confidence, grade, action, tenant)` / `list_gaps(status, source, page, size)` 全 plan 一致。

**已知偏离**：Task 2/4 的 `_bg_tasks` 集合变量名、main.py lifespan 注册点需 implementer 按现有代码对齐（plan 已标注"若变量名不同，对齐现有命名"）。
