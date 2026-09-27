"""领域包加载器（ADR 0002、0005；契约见《设计文档》§16.11）。

领域包只含数据：DOMAIN_PACKS_DIR/<DOMAIN_PACK>/ 下的 pack.yaml、schema.yaml、aliases.json、
normalize.yaml、prompts/*.md。部署级生效：进程内只加载一个包，启动时校验，失败即抛出。
"""
import json
import re
import string
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parents[2]   # backend/（镜像内为 /app）

# 各 prompt 允许的模板变量；未列出的 prompt 不允许任何变量
_PROMPT_VARS: dict[str, set[str]] = {
    "extract": {"entity_types", "relations", "text"},
    "system": {"display_name"},
    "refuse": set(),
}
_REQUIRED_PROMPTS = ("extract", "system", "refuse")


class DomainPackError(RuntimeError):
    """领域包缺文件或校验失败（启动即失败）。"""


def _check_regex(v: str) -> str:
    try:
        re.compile(v)
    except re.error as e:        # re.error 不是 ValueError，需转换才会进入 pydantic 校验报告
        raise ValueError(f"正则无效 {v!r}：{e}") from e
    return v


class NormalizePattern(BaseModel):
    regex: str
    replace: str = ""

    @field_validator("regex")
    @classmethod
    def _compiles(cls, v: str) -> str:
        return _check_regex(v)


class NormalizeRules(BaseModel):
    mappings: dict[str, str] = Field(default_factory=dict)
    patterns: list[NormalizePattern] = Field(default_factory=list)


class EntityLinkingConfig(BaseModel):
    chain: list[Literal["dictionary", "embedding"]] = Field(default_factory=lambda: ["dictionary", "embedding"])
    embedding_threshold: float = Field(0.75, ge=0.0, le=1.0)

    @field_validator("chain")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("entity_linking.chain 不能为空")
        return v


class GraphEvidenceConfig(BaseModel):
    max_hops: int = Field(2, ge=1, le=3)
    hub_degree: int = Field(30, ge=1)
    start_hub_max_hops: int = Field(1, ge=0)
    max_candidates: int = Field(30, ge=1)


class ScorerConfig(BaseModel):
    name: str = "dashscope_rerank"
    high: Optional[float] = None      # None = 使用该打分器的标定值
    low: Optional[float] = None

    @model_validator(mode="after")
    def _ordered(self):
        if self.high is not None and self.low is not None and not self.low < self.high:
            raise ValueError(f"scorer.low({self.low}) 必须小于 scorer.high({self.high})")
        return self


class ChunkingConfig(BaseModel):
    size: int = Field(500, ge=50)
    overlap: int = Field(50, ge=0)

    @model_validator(mode="after")
    def _overlap_lt_size(self):
        if self.overlap >= self.size:
            raise ValueError("chunking.overlap 必须小于 chunking.size")
        return self


class DocTypeRule(BaseModel):
    pattern: str                      # 匹配文件名的正则
    doc_type: str

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        return _check_regex(v)


class DomainPack(BaseModel):
    name: str
    display_name: str
    entity_types: list[str] = Field(min_length=1)
    relations: list[str] = Field(min_length=1)
    aliases: dict[str, str] = Field(default_factory=dict)       # 别名 → 标准名
    normalize: NormalizeRules = Field(default_factory=NormalizeRules)
    prompts: dict[str, str]
    entity_linking: EntityLinkingConfig = Field(default_factory=EntityLinkingConfig)
    graph_evidence: GraphEvidenceConfig = Field(default_factory=GraphEvidenceConfig)
    scorer: ScorerConfig = Field(default_factory=ScorerConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    doc_type_rules: list[DocTypeRule] = Field(default_factory=list)

    @field_validator("prompts")
    @classmethod
    def _check_prompts(cls, v: dict[str, str]) -> dict[str, str]:
        missing = [k for k in _REQUIRED_PROMPTS if not (v.get(k) or "").strip()]
        if missing:
            raise ValueError(f"缺少 prompt：{missing}（放在 prompts/<name>.md）")
        for key, text in v.items():
            used = {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}
            bad = used - _PROMPT_VARS.get(key, set())
            if bad:
                raise ValueError(f"prompts/{key}.md 含未定义的模板变量 {sorted(bad)}（字面花括号请写成 {{{{ }}}}）")
        if "text" not in {f for _, f, _, _ in string.Formatter().parse(v["extract"]) if f}:
            raise ValueError("prompts/extract.md 必须包含 {text}")
        return v

    def render_prompt(self, key: str, **kwargs) -> str:
        """渲染 prompt；entity_types/relations/display_name 由领域包自动填充。"""
        ctx = {"entity_types": "、".join(self.entity_types), "relations": " / ".join(self.relations),
               "display_name": self.display_name}
        ctx.update(kwargs)
        return self.prompts[key].format(**ctx)

    def doc_type_for(self, filename: str) -> Optional[str]:
        for rule in self.doc_type_rules:
            if re.search(rule.pattern, filename or ""):
                return rule.doc_type
        return None


def _read_yaml(path: Path, required: bool) -> dict:
    if not path.exists():
        if required:
            raise DomainPackError(f"领域包缺少 {path.name}：{path}")
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise DomainPackError(f"{path} 顶层必须是映射")
    return data


def packs_dir() -> Path:
    p = Path(settings.DOMAIN_PACKS_DIR)
    return p if p.is_absolute() else _BACKEND_ROOT / p


def load_pack(name: str, base_dir: Optional[Path] = None) -> DomainPack:
    """从目录加载并校验领域包；任何问题都抛 DomainPackError。"""
    root = (base_dir or packs_dir()) / name
    if not root.is_dir():
        raise DomainPackError(f"领域包目录不存在：{root}（检查 DOMAIN_PACK / DOMAIN_PACKS_DIR）")
    meta = _read_yaml(root / "pack.yaml", required=True)
    schema = _read_yaml(root / "schema.yaml", required=True)
    normalize = _read_yaml(root / "normalize.yaml", required=False)
    aliases_path = root / "aliases.json"
    aliases = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else {}
    prompts = {p.stem: p.read_text(encoding="utf-8") for p in sorted((root / "prompts").glob("*.md"))}
    if meta.get("name", name) != name:
        raise DomainPackError(f"pack.yaml 的 name={meta.get('name')!r} 与目录名 {name!r} 不一致")
    try:
        return DomainPack(**{**meta, "name": name}, entity_types=schema.get("entity_types") or [],
                          relations=schema.get("relations") or [], aliases=aliases,
                          normalize=normalize, prompts=prompts)
    except ValidationError as e:
        raise DomainPackError(f"领域包 {name} 校验失败：\n{e}") from e


@lru_cache
def get_pack() -> DomainPack:
    """当前部署的领域包（进程内缓存）。测试切换包后调用 get_pack.cache_clear()。"""
    return load_pack(settings.DOMAIN_PACK)
