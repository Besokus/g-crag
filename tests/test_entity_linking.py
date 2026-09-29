"""实体链接（设计文档 §7、§14、§16.5）：词典命中 / 别名命中 / 换说法走向量 / 全部未命中。"""
import asyncio

import numpy as np
import pytest

from app.core.domain_pack import get_pack
from app.rag import entity_index as ei
from app.rag import entity_linking as el

ENTITIES = [("华为", 40), ("华为技术有限公司", 3), ("Mate 60 Pro", 2), ("深圳", 25),
            ("中", 99), ("玉门市疏勒河引水工程", 1)]
DIM = 4
VEC = {"华为": [1, 0, 0, 0], "华为技术有限公司": [0.9, 0.1, 0, 0], "Mate 60 Pro": [0, 1, 0, 0],
       "深圳": [0, 0, 1, 0], "中": [0, 0, 0, 1], "玉门市疏勒河引水工程": [0, 0.2, 0, 0.98]}


@pytest.fixture
def index(monkeypatch):
    async def fake_list():
        return list(ENTITIES)
    monkeypatch.setattr(ei.neo4j_client, "list_entities", fake_list)
    monkeypatch.setattr(get_pack(), "aliases", {"菊厂": "华为", "鹏城": "深圳", "不在图谱": "某公司"})
    idx = ei.EntityIndex()
    names = [n for n, _ in ENTITIES]
    m = np.asarray([VEC[n] for n in names], dtype=np.float32)
    asyncio.run(_load_without_db(idx, monkeypatch))
    idx.vec_names, idx.matrix = names, m / np.linalg.norm(m, axis=1, keepdims=True)
    monkeypatch.setattr(ei, "_index", idx)
    monkeypatch.setattr(el, "get_index", lambda: idx)
    return idx


async def _load_without_db(idx, monkeypatch):
    """reload 的词典部分；向量部分由测试直接注入矩阵。"""
    class _NoRows:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            class R:
                def all(self):
                    return []
            return R()
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: _NoRows())
    await idx.reload()


@pytest.fixture
def chain(monkeypatch):
    cfg = get_pack().entity_linking

    def set_chain(c, threshold=0.75):
        monkeypatch.setattr(cfg, "chain", c)
        monkeypatch.setattr(cfg, "embedding_threshold", threshold)
    set_chain(["dictionary", "embedding"])
    return set_chain


@pytest.fixture
def qvec(monkeypatch):
    box = {"v": [0, 0, 0, 0], "err": None, "calls": 0}

    async def fake_embed_query(text, provider=None):
        box["calls"] += 1
        if box["err"]:
            raise box["err"]
        return box["v"]
    monkeypatch.setattr("app.services.embedding_service.embed_query", fake_embed_query)
    return box


def _link(q):
    return asyncio.run(el.link(q))


def test_dictionary_longest_match_wins(index, chain, qvec):
    r = _link("华为技术有限公司在深圳发布了Mate 60 Pro")
    assert r.strategy == "dictionary" and qvec["calls"] == 0     # 词典命中即停，不调 embedding
    names = [e.name for e in r.entities]
    assert "华为技术有限公司" in names and "华为" not in names   # 重叠时取更长者
    assert set(names) == {"华为技术有限公司", "深圳", "Mate 60 Pro"}
    assert all(e.score == 1.0 for e in r.entities)


def test_dictionary_case_insensitive_keeps_original_text(index, chain, qvec):
    r = _link("mate 60 pro 多少钱")
    assert [(e.name, e.matched) for e in r.entities] == [("Mate 60 Pro", "mate 60 pro")]


def test_alias_maps_to_standard_name(index, chain, qvec):
    r = _link("菊厂总部在鹏城吗")
    assert [(e.name, e.matched) for e in r.entities] == [("华为", "菊厂"), ("深圳", "鹏城")]


def test_alias_to_entity_not_in_graph_ignored(index, chain, qvec):
    assert _link("不在图谱的说法").strategy != "dictionary"


def test_single_char_entity_not_in_dictionary(index, chain, qvec):
    assert all(e.name != "中" for e in _link("中国和华为").entities)


def test_paraphrase_falls_back_to_embedding(index, chain, qvec):
    qvec["v"] = [0.1, 0.15, 0, 0.98]           # "疏勒河那个引水项目" → 近似 玉门市疏勒河引水工程
    r = _link("疏勒河那个引水项目中标了多少")
    assert r.strategy == "embedding"
    assert r.entities[0].name == "玉门市疏勒河引水工程" and r.entities[0].matched == ""
    assert 0.75 <= r.entities[0].score <= 1.0


def test_nothing_matches(index, chain, qvec):
    qvec["v"] = [0.5, 0.5, 0.5, 0.5]           # 与所有实体余弦都 < 0.75（除非很接近）
    r = _link("今天天气怎么样")
    assert (r.strategy, r.entities, r.degraded) == ("none", [], "")


def test_embedding_failure_degrades(index, chain, qvec):
    qvec["err"] = RuntimeError("DashScope 超时")
    r = _link("今天天气怎么样")
    assert (r.strategy, r.entities, r.degraded) == ("none", [], "embedding_failed")


def test_chain_order_from_pack(index, chain, qvec):
    chain(["embedding"], threshold=0.9)
    qvec["v"] = [1, 0, 0, 0]
    r = _link("华为技术有限公司")                # 只有向量策略：不走词典
    assert r.strategy == "embedding" and r.entities[0].name == "华为"


def test_at_most_five_entities_prefer_longer(index, chain, qvec, monkeypatch):
    many = [(f"实体{i:02d}" + "长" * i, 1) for i in range(8)]

    async def fake_list():
        return many
    monkeypatch.setattr(ei.neo4j_client, "list_entities", fake_list)
    asyncio.run(_load_without_db(index, monkeypatch))
    r = _link("、".join(n for n, _ in many))
    assert len(r.entities) == el.MAX_ENTITIES
    assert r.entities[0].name == many[-1][0]


# ---- 实体向量补算（真实测试库；embedding 打桩）----

def test_rebuild_entity_embeddings_incremental(monkeypatch):
    from app.db.session import AsyncSessionLocal
    from app.models.entity_embedding import EntityEmbedding
    from sqlalchemy import delete, func, select

    graph = [("甲公司", 1), ("乙公司", 2)]

    async def fake_list():
        return list(graph)
    calls = []

    async def fake_embed(texts, chunk_ids=None):
        calls.append(list(texts))
        return [[float(len(t)), 1.0, 0.0, 0.0] for t in texts]
    monkeypatch.setattr(ei.neo4j_client, "list_entities", fake_list)
    monkeypatch.setattr("app.services.embedding_service.embed_texts", fake_embed)
    monkeypatch.setattr(ei, "_index", ei.EntityIndex())

    async def run():
        async with AsyncSessionLocal() as db:
            await db.execute(delete(EntityEmbedding))
            await db.commit()
        n1 = await ei.rebuild_entity_embeddings()
        graph.append(("丙公司", 1))
        graph.remove(("甲公司", 1))
        n2 = await ei.rebuild_entity_embeddings()
        async with AsyncSessionLocal() as db:
            names = set((await db.execute(select(EntityEmbedding.name))).scalars().all())
        monkeypatch.setattr(ei.settings, "QWEN_EMB_MODEL", "text-embedding-v4")   # 换模型 → 全部重算
        n3 = await ei.rebuild_entity_embeddings()
        async with AsyncSessionLocal() as db:
            total = (await db.execute(select(func.count()).select_from(EntityEmbedding))).scalar()
            await db.execute(delete(EntityEmbedding))
            await db.commit()
        return n1, n2, names, n3, total

    n1, n2, names, n3, total = asyncio.run(run())
    assert (n1, n2) == (2, 1)                          # 第二次只补新增的丙公司
    assert names == {"乙公司", "丙公司"}                # 已不在图谱中的甲公司被删除
    assert (n3, total) == (2, 2)
    assert ei.get_index().loaded and len(ei.get_index().vec_names) == 2
