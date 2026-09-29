"""分级信号修复（M3，设计文档 §5.2、§16.3）与故障注入：rerank 成功 / 超时 / 无 key / 跳过。

原缺陷：分级读配置开关 RERANK_ENABLE（恒 True），rerank 失败时拿 RRF 分（~0.02）或全 0 分比阈值
→ 全部 incorrect → 改写 → 拒答；高置信 sparse 跳过 rerank → BM25 归一分 1.0 → 恒 correct。
"""
import pytest

from app.rag import grading
from app.services import qa_service
from app.services.retrieval_service import ScoringStatus, SearchResult

TH = grading.Thresholds(high=0.7, low=0.3)


def _ok():
    return ScoringStatus("stub", True, "ok", 3)


def _fail(reason):
    return ScoringStatus("stub", False, reason)


# ---- grade() ----

@pytest.mark.parametrize("top1,expected", [(0.9, "correct"), (0.7, "correct"), (0.5, "ambiguous"),
                                           (0.3, "ambiguous"), (0.1, "incorrect")])
def test_grade_by_top1(top1, expected):
    assert grading.grade([{"score": top1}], _ok(), TH)[0] == expected


@pytest.mark.parametrize("reason", ["timeout", "no_key", "error", "skipped_by_route", "disabled"])
def test_grade_unscored_when_scoring_failed(reason):
    # 分数再低也不判 incorrect：分数不是打分器给的，不可信
    assert grading.grade([{"score": 0.02}], _fail(reason), TH) == ("unscored", reason)


def test_grade_no_items_is_incorrect():
    assert grading.grade([], _ok(), TH) == ("incorrect", "no_items")


# ---- get_thresholds() 优先级 ----

@pytest.fixture
def layers(monkeypatch, tmp_path):
    from app.core import domain_pack
    from app.services import config_service
    pack = domain_pack.get_pack()
    monkeypatch.setattr(pack.scorer, "high", None)
    monkeypatch.setattr(pack.scorer, "low", None)
    monkeypatch.setattr(grading, "_THRESHOLDS_DIR", tmp_path)
    grading._calibrated.cache_clear()
    monkeypatch.setattr(config_service, "_RUNTIME", {})
    monkeypatch.setattr(grading.settings, "CRAG_HIGH", 0.72)
    monkeypatch.setattr(grading.settings, "CRAG_LOW", 0.3)
    yield {"pack": pack, "dir": tmp_path, "rt": config_service}
    grading._calibrated.cache_clear()


def _calib(layers, text):
    (layers["dir"] / f"{grading.settings.SCORER}.yaml").write_text(text, encoding="utf-8")
    grading._calibrated.cache_clear()


def test_thresholds_default_to_settings(layers):
    th = grading.get_thresholds()
    assert (th.high, th.low, th.source) == (0.72, 0.3, "high=settings,low=settings")


def test_thresholds_priority_each_field_independent(layers, monkeypatch):
    monkeypatch.setattr(layers["rt"], "_RUNTIME", {"crag_high": 0.8, "crag_low": 0.2})
    _calib(layers, "high: 0.6\nlow: null\n")
    monkeypatch.setattr(layers["pack"].scorer, "low", 0.1)
    th = grading.get_thresholds()
    assert (th.high, th.low) == (0.6, 0.1)
    assert th.source == "high=calibrated,low=pack"


def test_thresholds_conflict_falls_back(layers, monkeypatch):
    monkeypatch.setattr(layers["pack"].scorer, "low", 0.9)   # 高于 settings 的 high 0.72
    th = grading.get_thresholds()
    assert (th.high, th.low) == (0.72, 0.3) and th.source == "settings(fallback)"


# ---- 故障注入：_crag_correct 在四种打分情况下的行为 ----

@pytest.fixture
def crag_env(monkeypatch):
    for k, v in {"CRAG_ENABLE": True, "CRAG_PERDOC_ENABLE": False, "CRAG_V3_ENABLE": False,
                 "RERANK_ENABLE": True}.items():
        monkeypatch.setattr(qa_service.settings, k, v)
    monkeypatch.setattr(grading, "get_thresholds", lambda: TH)
    calls = {"rewrite": 0, "search": []}

    async def fake_rewrite(q, mt=None, force=False):
        calls["rewrite"] += 1
        return q + "（改写）"

    monkeypatch.setattr("app.services.query_rewrite.rewrite_query", fake_rewrite)

    def set_retry(items, scoring):
        async def fake_ex(db, q, topk, **kw):
            calls["search"].append(kw.get("force_score"))
            return SearchResult(items=items, scoring=scoring)
        monkeypatch.setattr(qa_service.retrieval_service, "mixed_search_ex", fake_ex)

    calls["set_retry"] = set_retry
    return calls


async def _correct(ctx, scoring):
    return await qa_service._crag_correct(None, "问题", ctx, "claude", 5, scoring=scoring)


@pytest.mark.asyncio
async def test_rerank_ok_high_score_correct(crag_env):
    _, conf, action, grade, extras = await _correct([{"score": 0.9}], _ok())
    assert (grade, action, conf) == ("correct", "normal", "high")
    assert extras["scoringOk"] is True and crag_env["rewrite"] == 0


@pytest.mark.asyncio
async def test_rerank_ok_low_score_rewrites_then_refuses(crag_env):
    crag_env["set_retry"]([{"score": 0.1}], _ok())
    _, conf, action, grade, _ = await _correct([{"score": 0.1}], _ok())
    assert (grade, action, conf) == ("incorrect", "refused", "refused")
    assert crag_env["search"] == [True]              # 改写重检索同样强制打分


@pytest.mark.asyncio
async def test_rerank_ok_rewrite_recovers(crag_env):
    crag_env["set_retry"]([{"score": 0.9}], _ok())
    _, conf, action, grade, _ = await _correct([{"score": 0.1}], _ok())
    assert (grade, action) == ("correct", "rewritten") and conf == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "no_key", "skipped_by_route"])
async def test_scoring_failure_does_not_correct_or_refuse(crag_env, reason):
    # 原实现：RRF 分 0.02 < LOW → incorrect → 改写 → 拒答；现在：不纠错，置信度 medium
    ctx = [{"score": 0.02}]
    out_ctx, conf, action, grade, extras = await _correct(ctx, _fail(reason))
    assert (grade, action, conf) == ("unscored", "unscored", "medium")
    assert extras == {"scoringOk": False, "scoringReason": reason}
    assert out_ctx == ctx and crag_env["rewrite"] == 0


@pytest.mark.asyncio
async def test_rewrite_scoring_failure_not_refused(crag_env):
    crag_env["set_retry"]([{"score": 0.02}], _fail("timeout"))
    _, conf, action, grade, extras = await _correct([{"score": 0.1}], _ok())
    assert grade == "unscored" and action == "rewritten" and conf == "medium"
    assert extras["scoringOk"] is False and extras["scoringReason"] == "timeout"


@pytest.mark.asyncio
async def test_legacy_caller_without_scoring_unchanged(crag_env, monkeypatch):
    """不传 scoring 的旧调用方沿用原逻辑（读配置开关 + crag.grade），且不带 scoringOk 字段。"""
    _, conf, _, grade, extras = await qa_service._crag_correct(None, "q", [{"score": 0.9}], "claude", 5)
    assert grade == "correct" and "scoringOk" not in extras
