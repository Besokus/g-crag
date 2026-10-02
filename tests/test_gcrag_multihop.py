"""多跳演示集候选题构造（scripts/gcrag_multihop.py）的纯函数部分。"""
import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gcrag_multihop.py"
sys.path.insert(0, str(_PATH.parent))
spec = importlib.util.spec_from_file_location("gcrag_multihop_ut", _PATH)
M = importlib.util.module_from_spec(spec)
sys.modules["gcrag_multihop_ut"] = M
spec.loader.exec_module(M)


def _row(a, b, c, doc1, doc2):
    return {"a": a, "b": b, "c": c, "rel1": "合作", "rel2": "中标", "r1from": a, "r2from": c,
            "chunk1": f"k_{doc1}", "chunk2": f"k_{doc2}", "doc1": doc1, "doc2": doc2}


def test_sample_paths_spread_and_reproducible():
    rows = [_row("甲公司", "碧水源", "项目X", "d1", "d2"), _row("乙公司", "碧水源", "项目Y", "d3", "d4"),
            _row("丙公司", "久安公司", "项目Z", "d2", "d5"), _row("丁公司", "大禹节水", "项目W", "d6", "d7")]
    ps = M.sample_paths(rows, 10, seed=1)
    assert len({p["b"] for p in ps}) == len(ps)                      # 每个桥接实体至多一条
    docs = [d for p in ps for d in (p["doc1"], p["doc2"])]
    assert len(docs) == len(set(docs))                               # 文档不重复使用
    assert ps == M.sample_paths(list(reversed(rows)), 10, seed=1)    # 与输入顺序无关、可复现
    p = next(p for p in ps if p["b"] == "大禹节水")
    assert (p["t1"], p["t2"]) == ("丁公司 -[合作]-> 大禹节水", "项目W -[中标]-> 大禹节水")


def test_parse_generated():
    ok = M.parse_generated('说明 {"ok": true, "question": "与甲签约的企业中标金额？", "answer": "13亿元"}', "碧水源")
    assert ok["valid"] and ok["answer"] == "13亿元"
    assert M.parse_generated('{"ok": true, "question": "碧水源中标金额？", "answer": "13亿"}',
                             "碧水源")["reason"] == "bridge_leaked"
    assert M.parse_generated('{"ok": true, "question": "它此后中标了项目X吗？", "answer": "是"}', "碧水源",
                             "项目X")["reason"] == "bridge_leaked"                # 终点实体也不能出现
    assert M.parse_generated('{"ok": false}', "碧水源")["reason"] == "generator_declined"
    assert not M.parse_generated("不是 JSON", "碧水源")["valid"]


def test_classify_and_render_order():
    base = {"t1": "a", "t2": "b", "question": "q", "answer": "x", "valid": True, "startLinked": True}
    cs = [{**base, "pid": "mh01", "retrievalHit": True, "graphHit": True},
          {**base, "pid": "mh02", "retrievalHit": False, "graphHit": True},
          {**base, "pid": "mh03", "startLinked": False},
          {"pid": "mh04", "t1": "a", "t2": "b", "valid": False, "reason": "generator_declined"}]
    assert [M.classify(c) for c in cs] == ["both", "graph_only", "no_link", "invalid"]
    md = M.render_markdown(cs)
    assert md.index("mh02（graph_only）") < md.index("mh01（both）") < md.index("mh04（invalid）")
    assert "采用：`mh02|`" in md
