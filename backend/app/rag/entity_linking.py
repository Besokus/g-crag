"""实体链接策略链（设计文档 §7、§16.5）：按领域包 entity_linking.chain 顺序执行，前一个命中即停。

- dictionary：问题文本上对"图谱实体名 + 领域包别名"做最长匹配；零 LLM 成本、结果确定，换说法时漏召回。
- embedding：问题做一次云端 embedding，与实体名向量矩阵算余弦，取 ≥ embedding_threshold 的前 5 个；
  embedding 调用失败时 degraded="embedding_failed"，该策略结果为空。
"""
from dataclasses import dataclass, field
from typing import Literal

from app.core.obs import degraded
from app.rag.entity_index import get_index

MAX_ENTITIES = 5


@dataclass
class LinkedEntity:
    name: str          # 图谱中的标准实体名
    matched: str       # 问题中命中的原文片段（向量召回时为空）
    score: float       # 词典命中为 1.0；向量召回为余弦相似度


@dataclass
class LinkingResult:
    strategy: Literal["dictionary", "embedding", "none"]
    entities: list[LinkedEntity] = field(default_factory=list)
    degraded: str = ""


def _by_dictionary(query: str) -> list[LinkedEntity]:
    hits = get_index().match_dictionary(query)
    hits.sort(key=lambda h: -len(h[0]))          # 超过上限时保留更长（更具体）的实体
    return [LinkedEntity(name=std, matched=m, score=1.0) for m, std in hits[:MAX_ENTITIES]]


async def _by_embedding(query: str, threshold: float) -> list[LinkedEntity]:
    from app.services import embedding_service
    qvec = await embedding_service.embed_query(query)
    return [LinkedEntity(name=n, matched="", score=round(s, 4))
            for n, s in get_index().nearest(qvec, threshold, MAX_ENTITIES)]


async def link(query: str) -> LinkingResult:
    from app.core.domain_pack import get_pack
    cfg = get_pack().entity_linking
    try:
        await get_index().ensure_loaded()
    except Exception as e:
        degraded("entity_index_load", e, "实体索引加载失败 → 跳过图谱定位证据")
        return LinkingResult("none", degraded="index_unavailable")
    deg = ""
    for strategy in cfg.chain:
        if strategy == "dictionary":
            ents = _by_dictionary(query)
        else:
            try:
                ents = await _by_embedding(query, cfg.embedding_threshold)
            except Exception as e:
                degraded("entity_link_embedding", e, "实体向量召回失败 → 只用词典结果")
                deg, ents = "embedding_failed", []
        if ents:
            return LinkingResult(strategy, ents, deg)
    return LinkingResult("none", [], deg)
