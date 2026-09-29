"""实体索引（设计文档 §7、§16.5）：图谱实体名 + 度数 + 名称向量矩阵，进程内单例。

- 词典：图谱全部实体名 + 领域包别名（别名 → 标准名，仅标准名在图谱中时生效），供最长匹配。
- 向量：entity_embedding 表中当前 embedding 模型的向量，归一化后组成矩阵，供余弦召回。
- 建图后由 rebuild_entity_embeddings() 补齐向量（rebuild_kg.py 调用），reload() 重新加载。
"""
import asyncio
import base64
import hashlib

import numpy as np
from sqlalchemy import delete, select

from app.clients import neo4j_client
from app.config import settings
from app.core.obs import degraded

_MIN_SURFACE_LEN = 2          # 单字实体（"中""美"）在问题里到处都能匹配上，不进词典
_EMBED_BATCH = 200            # 每次交给 embed_texts 的名称数（provider 内部再按 10 条分批并发）


def name_hash(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()


def _encode(vec) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def _decode(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=np.float32)


def _emb_model() -> str:
    return f"{settings.EMB_PROVIDER}:{settings.QWEN_EMB_MODEL}"


class EntityIndex:
    def __init__(self):
        self.names: list[str] = []
        self.degree: dict[str, int] = {}
        self.surfaces: dict[str, list[tuple[str, str]]] = {}   # 首字(小写) → [(写法小写, 标准名)]，按长度降序
        self.vec_names: list[str] = []
        self.matrix: np.ndarray | None = None                   # (n, dim)，行已归一化
        self.loaded = False
        self._lock = asyncio.Lock()

    async def reload(self) -> None:
        from app.core.domain_pack import get_pack
        from app.db.session import AsyncSessionLocal
        from app.models.entity_embedding import EntityEmbedding
        async with self._lock:
            ents = await neo4j_client.list_entities()
            names = [n for n, _ in ents]
            degree = dict(ents)
            in_graph = set(names)

            forms: dict[str, str] = {}
            for n in names:
                if len(n) >= _MIN_SURFACE_LEN:
                    forms.setdefault(n.lower(), n)
            for alias, std in (get_pack().aliases or {}).items():
                if std in in_graph and len(alias) >= _MIN_SURFACE_LEN:
                    forms.setdefault(alias.lower(), std)
            surfaces: dict[str, list[tuple[str, str]]] = {}
            for form, std in forms.items():
                surfaces.setdefault(form[0], []).append((form, std))
            for lst in surfaces.values():
                lst.sort(key=lambda x: -len(x[0]))

            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(EntityEmbedding.name, EntityEmbedding.vector)
                    .where(EntityEmbedding.model == _emb_model()))).all()
            vec_names, vecs = [], []
            for name, v in rows:
                if name in in_graph:
                    vec_names.append(name)
                    vecs.append(_decode(v))
            matrix = None
            if vecs:
                m = np.vstack(vecs)
                matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-10)

            self.names, self.degree, self.surfaces = names, degree, surfaces
            self.vec_names, self.matrix, self.loaded = vec_names, matrix, True

    async def ensure_loaded(self) -> None:
        if not self.loaded:
            await self.reload()

    def match_dictionary(self, query: str) -> list[tuple[str, str]]:
        """最长匹配，重叠时取更长者：返回 [(问题中的原文片段, 标准名)]，按出现顺序、标准名去重。"""
        q = query.lower()
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        i = 0
        while i < len(q):
            hit = None
            for form, std in self.surfaces.get(q[i], ()):
                if q.startswith(form, i):
                    hit = (form, std)
                    break                        # 列表按长度降序，第一个命中即最长
            if hit:
                form, std = hit
                if std not in seen:
                    seen.add(std)
                    out.append((query[i:i + len(form)], std))
                i += len(form)
            else:
                i += 1
        return out

    def nearest(self, qvec, threshold: float, k: int) -> list[tuple[str, float]]:
        if self.matrix is None:
            return []
        v = np.asarray(qvec, dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-10)
        sims = self.matrix @ v
        idx = np.argsort(-sims)[:k]
        return [(self.vec_names[i], float(sims[i])) for i in idx if sims[i] >= threshold]


_index = EntityIndex()


def get_index() -> EntityIndex:
    return _index


async def rebuild_entity_embeddings() -> int:
    """为图谱中尚无当前模型向量的实体补算向量，并删除已不在图谱中的旧行；返回新算的实体数。"""
    from app.db.session import AsyncSessionLocal
    from app.models.entity_embedding import EntityEmbedding
    from app.services import embedding_service

    model = _emb_model()
    names = [n for n, _ in await neo4j_client.list_entities()]
    wanted = {name_hash(n): n for n in names}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(EntityEmbedding.name_hash, EntityEmbedding.model))).all()
        stale = [h for h, m in rows if h not in wanted or m != model]
        for i in range(0, len(stale), 500):
            await db.execute(delete(EntityEmbedding).where(EntityEmbedding.name_hash.in_(stale[i:i + 500])))
        await db.commit()
        have = {h for h, m in rows if m == model and h in wanted}
    todo = [n for h, n in wanted.items() if h not in have]
    done = 0
    for i in range(0, len(todo), _EMBED_BATCH):
        batch = todo[i:i + _EMBED_BATCH]
        try:
            vecs = await embedding_service.embed_texts(batch)
        except Exception as e:
            degraded("entity_embedding", e, f"第 {i // _EMBED_BATCH + 1} 批向量化失败，重跑可补齐")
            continue
        async with AsyncSessionLocal() as db:
            db.add_all([EntityEmbedding(name_hash=name_hash(n), name=n, vector=_encode(v), model=model)
                        for n, v in zip(batch, vecs)])
            await db.commit()
        done += len(batch)
    await _index.reload()
    return done
