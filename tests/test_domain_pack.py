"""领域包加载器（《设计文档》§16.11）：内置包可加载；缺文件、坏模板、坏规则一律启动失败。"""
import shutil
from pathlib import Path

import pytest

from app.core import domain_pack
from app.core.domain_pack import DomainPackError, load_pack

PACKS = Path(domain_pack.__file__).resolve().parents[2] / "domain_packs"


@pytest.fixture
def pack_copy(tmp_path):
    """复制通用包到临时目录，供各用例改坏某个文件。"""
    shutil.copytree(PACKS / "general", tmp_path / "general")
    return tmp_path


@pytest.mark.parametrize("name", ["general", "grid"])
def test_shipped_packs_load(name):
    pack = load_pack(name, PACKS)
    assert pack.name == name
    assert pack.entity_types and pack.relations
    for key in ("extract", "system", "refuse"):
        assert pack.prompts[key].strip()


def test_extract_prompt_renders_schema():
    pack = load_pack("general", PACKS)
    text = pack.render_prompt("extract", text="[1] 华为在深圳发布了新手机。")
    assert "人物、组织" in text and "发布" in text and "[1] 华为" in text
    assert '{"s":"主体"' in text   # 字面花括号已正确转义


def test_system_prompt_renders_display_name():
    assert load_pack("general", PACKS).render_prompt("system").startswith("你是通用知识问答助手")


def test_grid_pack_keeps_original_aliases_and_doc_types():
    pack = load_pack("grid", PACKS)
    assert pack.aliases["主变"] == "主变压器"
    assert pack.doc_type_for("故障案例-110kV_SF6断路器灭弧室气体泄漏.txt") == "故障案例"
    assert pack.doc_type_for("随便一个文件.txt") is None


def test_get_pack_follows_setting(monkeypatch):
    monkeypatch.setattr(domain_pack.settings, "DOMAIN_PACK", "grid")
    domain_pack.get_pack.cache_clear()
    try:
        assert domain_pack.get_pack().name == "grid"
    finally:
        domain_pack.get_pack.cache_clear()


def test_missing_pack_dir(tmp_path):
    with pytest.raises(DomainPackError, match="目录不存在"):
        load_pack("nope", tmp_path)


def test_missing_schema(pack_copy):
    (pack_copy / "general" / "schema.yaml").unlink()
    with pytest.raises(DomainPackError, match="schema.yaml"):
        load_pack("general", pack_copy)


def test_missing_required_prompt(pack_copy):
    (pack_copy / "general" / "prompts" / "refuse.md").unlink()
    with pytest.raises(DomainPackError, match="refuse"):
        load_pack("general", pack_copy)


def test_undefined_template_variable(pack_copy):
    p = pack_copy / "general" / "prompts" / "system.md"
    p.write_text("你是{display_name}助手，领域：{domain}", encoding="utf-8")
    with pytest.raises(DomainPackError, match="domain"):
        load_pack("general", pack_copy)


def test_extract_prompt_requires_text(pack_copy):
    (pack_copy / "general" / "prompts" / "extract.md").write_text("只抽 {entity_types}", encoding="utf-8")
    with pytest.raises(DomainPackError, match=r"\{text\}"):
        load_pack("general", pack_copy)


def test_bad_normalize_regex(pack_copy):
    (pack_copy / "general" / "normalize.yaml").write_text('patterns:\n  - regex: "(未闭合"\n', encoding="utf-8")
    with pytest.raises(DomainPackError):
        load_pack("general", pack_copy)


def test_scorer_thresholds_must_be_ordered(pack_copy):
    p = pack_copy / "general" / "pack.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("high: null", "high: 0.3").replace("low: null", "low: 0.5"),
                 encoding="utf-8")
    with pytest.raises(DomainPackError, match="scorer.low"):
        load_pack("general", pack_copy)


def test_name_must_match_directory(pack_copy):
    p = pack_copy / "general" / "pack.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("name: general", "name: other"), encoding="utf-8")
    with pytest.raises(DomainPackError, match="不一致"):
        load_pack("general", pack_copy)


def test_unknown_linking_strategy(pack_copy):
    p = pack_copy / "general" / "pack.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("chain: [dictionary, embedding]", "chain: [llm]"),
                 encoding="utf-8")
    with pytest.raises(DomainPackError):
        load_pack("general", pack_copy)
