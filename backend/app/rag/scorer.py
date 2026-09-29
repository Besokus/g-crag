"""检索打分器接口与录制回放（《设计文档》§5.3、§5.4、§16.2；ADR 0006）。

分级只认打分器"本次是否真的打分成功"（ScoreResult.ok），不认配置开关、也不认分数本身：
原实现无 key 时返回全 0 分、超时时保留 RRF 分数（约 0.016~0.03），都被当作有效分数参与分级。

- DashscopeRerankScorer：gte-rerank（rerank_service 的 HTTP 实现），top_n=len(texts)，
  按输入顺序还原分数；无 key / 超时 / 出错都返回 ok=False 与原因。
- CachedScorer：按 sha256(打分器标识|query|text) 逐条缓存到 JSONL，评测首跑 record、重跑 replay，
  保证重跑可复现、不受限流或欠费影响；replay 未命中时实时打分并补录。任一条失败整次失败、不写缓存。
"""
import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ScoreResult:
    ok: bool
    scores: list[float] = field(default_factory=list)   # ok 时与 texts 等长、同序；失败时为空
    reason: str = "ok"                                   # ok | timeout | error | no_key


class Scorer(Protocol):
    name: str
    cache_id: str   # 打分器 + 模型标识，录制回放的键前缀（换模型不会回放旧分数）

    async def score(self, query: str, texts: list[str], *, timeout: float) -> ScoreResult: ...


class DashscopeRerankScorer:
    name = "dashscope_rerank"

    def __init__(self):
        self.model = settings.RERANK_MODEL
        self.cache_id = f"{self.name}@{self.model}"

    async def score(self, query: str, texts: list[str], *, timeout: float) -> ScoreResult:
        if not texts:
            return ScoreResult(ok=True)
        if not settings.DASHSCOPE_API_KEY:
            return ScoreResult(ok=False, reason="no_key")
        from app.services.rerank_service import get_reranker
        try:
            ranked = await asyncio.wait_for(
                get_reranker().rerank(query, texts, top_n=len(texts)), timeout=timeout)
        except asyncio.TimeoutError:
            return ScoreResult(ok=False, reason="timeout")
        except Exception:
            return ScoreResult(ok=False, reason="error")
        scores: list[float | None] = [None] * len(texts)
        for idx, s in ranked:
            if 0 <= idx < len(texts):
                scores[idx] = float(s)
        if any(s is None for s in scores):   # 返回条数不全，不能当作完整打分
            return ScoreResult(ok=False, reason="error")
        return ScoreResult(ok=True, scores=scores)  # type: ignore[arg-type]


class CachedScorer:
    """录制回放包装。mode: record（全部实时打分并写入）| replay（命中用缓存，未命中实时打分并补录）。"""

    def __init__(self, inner: Scorer, mode: str, path: Path):
        if mode not in ("record", "replay"):
            raise ValueError(f"未知 SCORER_CACHE: {mode}（支持: off | record | replay）")
        self.inner, self.mode, self.path = inner, mode, path
        self.name, self.cache_id = inner.name, inner.cache_id
        self._table: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.hits = self.misses = 0
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._table[rec["k"]] = float(rec["s"])

    def _key(self, query: str, text: str) -> str:
        return hashlib.sha256(f"{self.cache_id}|{query}|{text}".encode("utf-8")).hexdigest()

    async def score(self, query: str, texts: list[str], *, timeout: float) -> ScoreResult:
        keys = [self._key(query, t) for t in texts]
        if self.mode == "replay":
            todo = [i for i, k in enumerate(keys) if k not in self._table]
        else:
            todo = list(range(len(texts)))
        self.hits += len(texts) - len(todo)
        self.misses += len(todo)
        if todo:
            res = await self.inner.score(query, [texts[i] for i in todo], timeout=timeout)
            if not res.ok:
                return res
            async with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    for i, s in zip(todo, res.scores):
                        self._table[keys[i]] = s
                        f.write(json.dumps({"k": keys[i], "s": s}) + "\n")
        return ScoreResult(ok=True, scores=[self._table[k] for k in keys])


def _build_inner() -> Scorer:
    if settings.SCORER == "dashscope_rerank":
        return DashscopeRerankScorer()
    raise ValueError(f"未知 SCORER: {settings.SCORER}（支持: dashscope_rerank）")


def cache_path(scorer: Scorer) -> Path:
    if settings.SCORER_CACHE_PATH:
        return Path(settings.SCORER_CACHE_PATH)
    return _BACKEND_ROOT / "data" / "scorer_cache" / f"{scorer.cache_id}.jsonl"


_instances: dict[tuple, Scorer] = {}


def get_scorer() -> Scorer:
    """按 SCORER 选实现，按 SCORER_CACHE 包录制回放；同一配置复用同一实例（缓存表只加载一次）。"""
    key = (settings.SCORER, settings.RERANK_MODEL, settings.SCORER_CACHE, settings.SCORER_CACHE_PATH)
    if key not in _instances:
        inner = _build_inner()
        mode = settings.SCORER_CACHE
        _instances[key] = inner if mode == "off" else CachedScorer(inner, mode, cache_path(inner))
    return _instances[key]
