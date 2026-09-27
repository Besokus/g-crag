"""知识图谱实体消歧 + 关系 schema 约束（A5，图谱质量保障）。

LLM 自由抽取会产生两类噪声：
- 同义实体分裂："主变/主变压器/#1主变/1号主变" 成 4 个节点，多跳推理断链
- 关系发散："是/有/关于/涉及" 等无语义关系，图谱 schema 不收敛

收敛策略（规则全部来自当前领域包，《设计文档》§3.3"实体归一化"扩展点）：
- canonical_entity：normalize.yaml 的 patterns（正则，如电网包的编号前缀剥离）→ mappings（精确映射）
  → term_service 别名归一（aliases.json，主变→主变压器）
- canonical_relation：schema.yaml 白名单原样保留；否则按 normalize.yaml 的 relation_aliases 子串匹配到
  标准关系；仍无法映射归为 fallback_relation（默认"相关"，保留连通性，不丢边）
"""
import re
from functools import lru_cache

from app.core.domain_pack import get_pack
from app.services.term_service import normalize as term_normalize


@lru_cache
def _entity_patterns(pack_name: str) -> tuple:
    return tuple((re.compile(p.regex), p.replace) for p in get_pack().normalize.patterns)


def _relation_alias_table() -> list[tuple[str, str]]:
    """(同义写法, 标准关系) 列表：先标准关系自身，再按白名单顺序展开同义写法。"""
    pack = get_pack()
    table = [(r, r) for r in pack.relations]
    for std in pack.relations:
        table.extend((alias, std) for alias in pack.normalize.relation_aliases.get(std, []))
    return table


def canonical_entity(name: str) -> str:
    """实体消歧：领域包正则 + 精确映射 + 术语别名归一。"""
    if not name:
        return ""
    pack = get_pack()
    s = name.strip()
    for pat, repl in _entity_patterns(pack.name):
        s = pat.sub(repl, s)
    s = pack.normalize.mappings.get(s.strip(), s)
    s = term_normalize(s)
    return s.strip()


def canonical_relation(rel: str) -> str:
    """关系 schema 约束：映射到领域包白名单中的标准关系，无法映射归为兜底关系。"""
    pack = get_pack()
    fallback = pack.normalize.fallback_relation
    if not rel:
        return fallback
    r = rel.strip()
    if r in pack.relations:
        return r
    for alias, std in _relation_alias_table():
        if alias and alias in r:
            return std
    return fallback
