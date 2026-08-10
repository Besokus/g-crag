"""Sprint3a CIM 解析器单测：合成样本 → 设备节点 + 连通边。

纯解析器测试（无 DB/Milvus 依赖），CI 可跑。从仓库根运行：
    python -m pytest tests/test_cim_parser.py -v
"""
from pathlib import Path

from app.rag.cim_parser import graph_to_triples, parse_cim_xml

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample_cim.xml"


def _graph():
    return parse_cim_xml(SAMPLE.read_text(encoding="utf-8"))


def test_parse_equipments():
    """合成样本含 5 个 ConductingEquipment（母线/2开关/主变/线路）。"""
    g = _graph()
    types = sorted(e["type"] for e in g["equipments"])
    assert types == ["ACLineSegment", "Breaker", "Breaker", "BusbarSection", "PowerTransformer"]
    assert len(g["equipments"]) == 5
    assert g["station"] == "110kV示范站"


def test_parse_connectivity_edges():
    """线性链 BUS1-BRK1-T1-BRK2-LINE1 → 4 条连通边（每 CN 恰好 2 设备）。"""
    g = _graph()
    assert len(g["edges"]) == 4
    assert len(g["connectivityNodes"]) == 4


def test_known_connection():
    """主变 T1 经 CN 连到两个开关 → 图中应有 (BRK1,T1) 与 (T1,BRK2) 边。"""
    g = _graph()
    pairs = {frozenset((e["a"], e["b"])) for e in g["edges"]}
    assert frozenset(("BRK1", "T1")) in pairs
    assert frozenset(("T1", "BRK2")) in pairs


def test_graph_to_triples_uses_names():
    """三元组用设备名（供 KgTriple 存储 + GraphRAG 消费）。"""
    g = _graph()
    triples = graph_to_triples(g)
    subjects_objects = {(s, o) for s, _, o in triples}
    assert ("主变进线开关", "1号主变压器") in subjects_objects
    assert all(rel == "电气连接" for _, rel, _ in triples)


def test_parse_invalid_xml_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_cim_xml("<not valid rdf")
