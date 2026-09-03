"""检索评测纯函数单测：recall@k / MRR / nDCG（口径对齐 scripts/eval_retrieval.py）。

口径要点（2026-09 实跑修复）：golden expect 是内容关键词（如"主变压器"），docName
是文档标题（如"主变压器运行规程.txt"）——匹配必须走 docName 子串，精确相等会把
全部 query 判 0 分（eval_matrix 首次实跑暴露）。
"""
from app.services.retrieval_eval_service import _doc_relevance_binary, _mrr, _ndcg, _recall_at_k


# ---- 二元相关性（子串口径） ----

def test_binary_substring_hit():
    assert _doc_relevance_binary(["主变压器"], "主变压器运行规程.txt") == 1


def test_binary_no_hit():
    assert _doc_relevance_binary(["避雷器"], "主变压器运行规程.txt") == 0


def test_binary_empty_expect():
    assert _doc_relevance_binary([], "任意.txt") == 0


# ---- recall@k（query 级 0/1） ----

def test_recall_hit_via_substring():
    assert _recall_at_k(["主变压器"], ["SF6断路器维护手册.txt", "主变压器运行规程.txt"]) == 1.0


def test_recall_miss():
    assert _recall_at_k(["避雷器"], ["主变压器运行规程.txt"]) == 0.0


def test_recall_exact_equality_is_not_required():
    """回归：expect 关键词 ≠ 完整 docName 时靠分级标注兜底（旧精确相等口径全 0 的根因）。"""
    assert _recall_at_k(["重瓦斯"], ["主变压器运行规程.txt"]) == 0.0  # 标题无该词
    rd = {"主变压器运行规程": 3}
    assert _recall_at_k(["重瓦斯"], ["主变压器运行规程.txt"], rd) == 1.0


def test_recall_graded_relevant_docs_take_precedence():
    rd = {"SF6断路器": 3}
    # relevant_docs 存在时走 key 子串；expect 不再参与
    assert _recall_at_k(["主变压器"], ["SF6断路器维护手册.txt"], rd) == 1.0
    assert _recall_at_k(["SF6"], ["主变压器运行规程.txt"], rd) == 0.0


def test_recall_empty_everything():
    assert _recall_at_k([], []) == 0.0


# ---- MRR（首个相关文档倒数排名） ----

def test_mrr_first_rank():
    assert _mrr(["主变"], ["主变压器运行规程.txt", "SF6断路器维护手册.txt"]) == 1.0


def test_mrr_second_rank():
    assert _mrr(["主变"], ["SF6断路器维护手册.txt", "主变压器运行规程.txt"]) == 0.5


def test_mrr_no_hit():
    assert _mrr(["避雷器"], ["主变压器运行规程.txt"]) == 0.0


def test_mrr_graded():
    rd = {"SF6断路器": 2}
    assert _mrr(["主变"], ["主变压器运行规程.txt", "SF6断路器维护手册.txt"], rd) == 0.5


# ---- nDCG（分级 + 线性折损，对齐 ndcg_at_k） ----

def test_ndcg_ideal_order():
    rd = {"主变压器运行规程": 3, "SF6断路器": 1}
    got = ["主变压器运行规程.txt", "SF6断路器维护手册.txt"]
    assert abs(_ndcg(rd, got) - 1.0) < 1e-6


def test_ndcg_suboptimal():
    rd = {"主变压器运行规程": 3, "SF6断路器": 1}
    got = ["SF6断路器维护手册.txt", "主变压器运行规程.txt"]
    assert 0 < _ndcg(rd, got) < 1.0


def test_ndcg_key_is_substring_of_docname():
    """回归：relevant_docs key 不带 .txt 也能按子串匹配到带后缀的 docName。"""
    rd = {"主变压器运行规程": 3}
    assert _ndcg(rd, ["主变压器运行规程.txt"]) == 1.0


def test_ndcg_binary_fallback_without_relevant_docs():
    """无分级标注 → 二元 nDCG（相关=expect 子串命中）。"""
    assert _ndcg({}, ["主变压器运行规程.txt"], ["主变压器"], 2) == 1.0
    assert _ndcg({}, ["SF6断路器维护手册.txt"], ["主变压器"], 2) == 0.0


def test_ndcg_no_relevant():
    assert _ndcg({"A": 3}, ["B.txt", "C.txt"]) == 0.0
