"""gcrag_eval build：在合成的迷你 CRUD-RAG 目录上验证构造规则（《设计文档》§16.13）。"""
import asyncio
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import numpy as np

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gcrag_eval.py"
spec = importlib.util.spec_from_file_location("gcrag_eval_under_test", _PATH)
ge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ge)

DIM = 64


async def fake_embed(texts):
    """含标记 〔Qk〕 的文本映射到第 k 个基向量（语义相同）；其余文本给哈希随机向量（彼此近似正交）。"""
    out = []
    for t in texts:
        m = re.search(r"〔Q(\d+)〕", t)
        if m:
            v = np.zeros(DIM); v[int(m.group(1)) % DIM] = 1.0
        else:
            v = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)).normal(size=DIM)
        out.append(v.tolist())
    return out


FILLER = "本报记者从有关部门获悉，相关工作正按计划稳步推进，各方反映良好，后续还将持续跟进报道。"


def _article(tag: str, n: int = 6) -> str:
    return f"2023-08-0{len(tag) % 9 + 1}，正文：{tag}项目第{tag}期工作会议召开。" + FILLER * n


def _make_src(root: Path, n_events: int = 12) -> dict:
    """每个事件 E<k> 有专名《E<k>计划》；多文档题的片段取自语料行，单文档题的片段不在语料中。"""
    corpus, qa = [], {"single": [], "double": [], "triple": []}
    for k in range(n_events):
        name = f"星河{chr(0x4e00 + k * 7)}{chr(0x4e10 + k * 3)}计划"
        arts = [f"2023-08-01，正文：《{name}》第{j}篇报道。" + FILLER * 5 + f"（{k}-{j}）" for j in range(3)]
        corpus.extend(arts)
        base = {"id": f"id{k:03d}", "event": f"2023年8月，有关方面启动《{name}》。"}
        qa["single"].append({**base, "news1": f"单文档来源《{name}》独家报道。" + FILLER * 4,
                             "questions": f"《{name}》何时启动？〔Q{k}〕", "answers": "2023年8月"})
        qa["double"].append({**base, "news1": arts[0], "news2": arts[1], "thoughts": "",
                             "questions": f"《{name}》双文档问题〔Q{k}〕", "answers": "答"})
        qa["triple"].append({**base, "news1": arts[0], "news2": arts[1], "news3": arts[2], "thoughts": "",
                             "questions": f"《{name}》三文档问题〔Q{k}〕", "answers": "答"})
    unrelated = [_article(f"无关{chr(0x4e80 + i)}{chr(0x4ea0 + i)}", 5) + f"#{i}" for i in range(40)]
    trad = "2023-08-01，正文：這們國會說時來對發後經與為無關當從點學間現還麼應機開東體" * 6
    dup = unrelated[0]
    # 字面上与任何事件都不相关、但语义上属于事件 k 的报道（换了说法）：只能靠语义过滤剔除
    paraphrases = [f"2023-08-02，正文：另一家媒体从不同角度报道了同一件事〔Q{k}〕。" + FILLER * 5 + f"@{k}"
                   for k in range(n_events)]
    corpus.extend(unrelated + [trad, dup, "太短"] + paraphrases)
    d = root / "unzipped" / "CRUD_Data"
    d.mkdir(parents=True)
    for t, f in ge.QA_FILES.items():
        (d / f).write_text(json.dumps(qa[t], ensure_ascii=False), encoding="utf-8")
    c = root / "repo" / "data" / "80000_docs"
    c.mkdir(parents=True)
    (c / "documents_dup_part_1_part_1").write_text("\n".join(corpus), encoding="utf-8")
    return {"unrelated": unrelated, "trad": trad, "paraphrases": paraphrases}


KW = dict(embed=fake_embed, per_type=3, n_unans_double=1, n_unans_triple=1, corpus_size=40,
          rare_max=30, calib_ratio=1 / 3, event_sim=0.8, doc_sim=0.65)


def _build(src, out, seed=1, **kw):
    return asyncio.run(ge.build(src, out, seed=seed, **{**KW, **kw}))


@pytest.fixture
def built(tmp_path):
    info = _make_src(tmp_path / "src")
    rep = _build(tmp_path / "src", tmp_path / "out")
    qs = [json.loads(line) for line in (tmp_path / "out" / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    corpus = {p.stem: p.read_text(encoding="utf-8") for p in (tmp_path / "out" / "corpus").glob("*.txt")}
    removed = json.loads((tmp_path / "out" / "removed_docs.json").read_text(encoding="utf-8"))
    return rep, qs, corpus, removed, info


def test_counts_and_distinct_events(built):
    rep, qs, _, _, _ = built
    assert rep["questions"] == {"single": 3, "double": 3, "triple": 3,
                                "double_unanswerable": 1, "triple_unanswerable": 1}
    assert len({q["crudId"] for q in qs}) == len(qs)          # 每题事件互不相同


def test_gold_docs_in_corpus_except_removed(built):
    _, qs, corpus, removed, _ = built
    removed_ids = {r["docId"] for r in removed}
    assert len(removed_ids) == 2 + 3          # 1 道双文档 + 1 道三文档不可答题，全部标准文档都删
    for q in qs:
        for g in q["goldDocIds"]:
            assert (g in corpus) != (g in removed_ids)
        if q["answerable"]:
            assert q["removedDocIds"] == []
        else:
            assert q["removedDocIds"] == q["goldDocIds"] and not any(g in corpus for g in q["goldDocIds"])


def test_single_doc_gold_is_inline_news(built):
    _, qs, corpus, _, _ = built
    single = [q for q in qs if q["type"] == "single"]
    assert all(corpus[q["goldDocIds"][0]].startswith("单文档来源") for q in single)


def test_distractors_exclude_related_trad_and_dup(built):
    rep, qs, corpus, _, info = built
    names = [q["event"].split("《")[1].split("》")[0] for q in qs]
    gold = {g for q in qs for g in q["goldDocIds"]}
    distractors = [t for i, t in corpus.items() if i not in gold]
    assert distractors
    assert not any(n in t for t in distractors for n in names)   # 抽样事件的报道一律不做干扰文档
    assert sum(t == info["unrelated"][0] for t in corpus.values()) <= 1   # 重复稿只留一份
    assert info["trad"] not in corpus.values()
    assert rep["distractorFilter"]["lexical_related"] > 0


def test_split_assigns_calibration_subset(built):
    _, qs, _, _, _ = built
    for t in ("single", "double", "triple"):
        group = [q for q in qs if q["type"] == t and q["answerable"]]
        assert sum(q["split"] == "calib" for q in group) == 1


def test_semantic_filter_removes_paraphrased_same_event_reports(built):
    rep, qs, corpus, _, info = built
    sampled = {int(re.search(r"〔Q(\d+)〕", q["question"]).group(1)) for q in qs}
    kept = [p for p in info["paraphrases"] if p in corpus.values()]
    assert all(int(re.search(r"〔Q(\d+)〕", p).group(1)) not in sampled for p in kept)
    assert rep["distractorFilter"]["semantic_related"] > 0


def test_event_cluster_dedup(tmp_path):
    """语义同簇的事件只入选一个：事件 2m 与 2m+1 标记相同（6 簇），选 6 个事件时每簇恰好一个。"""
    _make_src(tmp_path / "src")
    for f in ge.QA_FILES.values():
        p = tmp_path / "src" / "unzipped" / "CRUD_Data" / f
        text = p.read_text(encoding="utf-8")
        for k in range(1, 12, 2):
            text = text.replace(f"〔Q{k}〕", f"〔Q{k - 1}〕")
        p.write_text(text, encoding="utf-8")
    rep = _build(tmp_path / "src", tmp_path / "out", per_type=2, n_unans_double=0, n_unans_triple=0)
    qs = [json.loads(line) for line in (tmp_path / "out" / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    clusters = [int(q["crudId"][2:]) // 2 for q in qs]
    assert len(clusters) == 6 and len(set(clusters)) == 6
    assert rep["eventClusterRejected"] >= 1


def test_deterministic(tmp_path):
    _make_src(tmp_path / "src")
    _build(tmp_path / "src", tmp_path / "a", seed=7)
    _build(tmp_path / "src", tmp_path / "b", seed=7)
    assert (tmp_path / "a" / "questions.jsonl").read_text() == (tmp_path / "b" / "questions.jsonl").read_text()


def test_text_helpers():
    assert ge.quoted_names("发布《防控近视核心知识十条》和“启明行动”") == {"防控近视核心知识十条", "启明行动"}
    assert ge.is_traditional("這們國會說時來對發後經與為無關" * 3)
    assert not ge.is_traditional("这是一段简体中文新闻正文" * 3)
    assert ge.near_dup_key("华为 发布，新品！") == ge.near_dup_key("华为发布新品")
