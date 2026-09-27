"""M5 三元组出处 chunk（ADR 0004，《设计文档》§4.2、§16.7）与领域包驱动的归一化。"""
import asyncio
import shutil

import pytest

from app.core import domain_pack
from app.core.domain_pack import DomainPackError, load_pack
from app.services import kg_service as svc
from app.services.kg_normalize import canonical_entity, canonical_relation


@pytest.fixture(autouse=True)
def _default_general(request, monkeypatch, tmp_path):
    """默认按通用包执行；显式要求 grid_pack 的用例由该夹具切换。"""
    if "grid_pack" not in request.fixturenames:
        request.getfixturevalue("general_pack")


def _tp(s, r, o, chunk_no, batch, **kw):
    return {"s": s, "r": r, "o": o, "chunk_no": chunk_no, "_batch": batch, **kw}


BATCH = [("c1", "华为在深圳发布了 Mate 60 Pro。"), ("c2", "发布会由余承东主持。"), ("c3", "天气晴。")]


def test_verified_when_named_chunk_contains_both():
    out = svc._normalize_triples([_tp("华为", "发布", "Mate 60 Pro", 1, BATCH)])
    assert out[0]["chunk_id"] == "c1" and out[0]["provenance"] == "verified"


def test_rematched_when_named_chunk_wrong():
    out = svc._normalize_triples([_tp("华为", "发布", "Mate 60 Pro", 3, BATCH)])
    assert out[0]["chunk_id"] == "c1" and out[0]["provenance"] == "rematched"


def test_none_when_no_chunk_has_both():
    out = svc._normalize_triples([_tp("余承东", "任职于", "华为", 2, BATCH)])
    assert out[0]["chunk_id"] == "" and out[0]["provenance"] == "none"


def test_match_ignores_whitespace_and_case():
    out = svc._normalize_triples([_tp("华为", "发布", "mate60pro", 1, BATCH)])
    assert out[0]["provenance"] == "verified"


def test_invalid_chunk_no_falls_back_to_batch_search():
    out = svc._normalize_triples([_tp("华为", "发布", "Mate 60 Pro", 99, BATCH)])
    assert out[0]["provenance"] == "rematched"


def test_same_fact_in_two_chunks_kept_per_chunk():
    batch = [("c1", "华为发布Mate 60 Pro。"), ("c2", "再次强调：华为发布Mate 60 Pro。")]
    out = svc._normalize_triples([_tp("华为", "发布", "Mate 60 Pro", 1, batch),
                                  _tp("华为", "发布", "Mate 60 Pro", 2, batch),
                                  _tp("华为", "发布", "Mate 60 Pro", 1, batch)])
    assert sorted(t["chunk_id"] for t in out) == ["c1", "c2"]


def test_raw_forms_and_types_kept():
    out = svc._normalize_triples([_tp("《华为》", "发布", "Mate 60 Pro", 1, BATCH,
                                      s_type="组织", o_type="不存在的类型")])
    t = out[0]
    assert t["s"] == "华为" and t["s_raw"] == "《华为》"
    assert t["s_type"] == "组织" and t["o_type"] == ""


def test_legacy_text_chunks_have_no_provenance():
    out = svc._normalize_triples([{"s": "华为", "r": "发布", "o": "Mate 60 Pro"}])
    assert out[0]["chunk_id"] == "" and out[0]["provenance"] == "none"


def test_grid_alias_counts_as_surface_form(grid_pack):
    batch = [("c1", "主变冷却系统故障时应减负荷。")]
    out = svc._normalize_triples([_tp("主变压器", "原因", "冷却系统故障", 1, batch)])
    assert out[0]["s"] == "主变压器" and out[0]["provenance"] == "verified"   # 原文写的是别名"主变"


def test_prompt_numbers_chunks_and_renders_pack_schema():
    seen = {}

    class _Prov:
        async def chat(self, msgs, **kw):
            seen["prompt"] = msgs[0]["content"]
            return '[{"s":"华为","r":"发布","o":"Mate 60 Pro","s_type":"组织","o_type":"产品","chunk_no":1}]'

    raw = asyncio.run(svc._extract_from_chunks(_Prov(), BATCH[:2]))
    assert "[1] 华为在深圳" in seen["prompt"] and "[2] 发布会" in seen["prompt"]
    assert "人物、组织" in seen["prompt"] and "任职于" in seen["prompt"]
    assert raw[0]["chunk_no"] == 1 and raw[0]["_batch"] == BATCH[:2]


def test_general_relations_normalized_by_pack():
    assert canonical_relation("发布") == "发布"
    assert canonical_relation("正式发布") == "发布"
    assert canonical_relation("召开") == "举办"
    assert canonical_relation("签订") == "签署"
    assert canonical_relation("喜欢") == "相关"


def test_general_pack_keeps_number_prefix():
    assert canonical_entity("3号线") == "3号线"          # 编号前缀剥离只属于电网包


def test_grid_pack_strips_number_prefix(grid_pack):
    assert canonical_entity("1号主变") == "主变压器"
    assert canonical_relation("动作于") == "保护"


def test_relation_alias_outside_whitelist_rejected(tmp_path):
    packs = domain_pack.packs_dir()
    shutil.copytree(packs / "general", tmp_path / "general")
    p = tmp_path / "general" / "normalize.yaml"
    p.write_text("relation_aliases:\n  不存在的关系: [x]\n", encoding="utf-8")
    with pytest.raises(DomainPackError, match="relation_aliases"):
        load_pack("general", tmp_path)
