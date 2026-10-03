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


# ---- 隐藏桥接子集（hard）----

def test_contains_and_leak_ratio():
    assert M.contains("据《人民日报》报道", "人民日报") and not M.contains("新华社", "人民日报")
    assert M.leak_ratio("碧水源中标金额", "碧水源中标金额13亿") == 1.0
    assert M.leak_ratio("与甲签约的企业", "完全无关的文字") == 0.0


def test_sample_hidden_requires_bridge_hidden():
    rows = [_row("甲公司", "碧水源", "项目X", "d1", "d2"),      # d2 提到了甲公司 → 不是隐藏桥接
            _row("乙公司", "碧水源", "项目Y", "d3", "d4"),      # 合格
            _row("丙公司", "久安公司", "项目Z", "d5", "d6")]    # d5 提到了项目Z → doc1 已含答案
    texts = {"d1": "甲公司与碧水源签约", "d2": "碧水源中标项目X，甲公司参与", "d3": "乙公司与碧水源签约",
             "d4": "碧水源中标项目Y", "d5": "丙公司与久安公司合作项目Z", "d6": "久安公司中标项目Z"}
    ps = M.sample_hidden(rows, texts, 10, seed=1)
    assert [p["a"] for p in ps] == ["乙公司"] and ps[0]["pid"] == "hb001"


def test_sample_hidden_bridge_cap():
    rows = [_row(f"公司{i}", "碧水源", f"项目{i}", f"a{i}", f"b{i}") for i in range(5)]
    texts = {f"a{i}": f"公司{i}与碧水源签约" for i in range(5)} | {f"b{i}": f"碧水源中标项目{i}" for i in range(5)}
    assert len(M.sample_hidden(rows, texts, 10, seed=1, max_per_bridge=3)) == 3


def test_sample_hidden_fanout():
    rows = [{**_row("甲公司", "碧水源", "项目X", "d1", "d2"), "r2Fanout": 5},
            {**_row("乙公司", "久安公司", "项目Y", "d3", "d4"), "r2Fanout": 2}]
    texts = {"d1": "甲公司与碧水源签约", "d2": "碧水源中标项目X", "d3": "乙公司与久安公司签约", "d4": "久安公司中标项目Y"}
    assert [p["b"] for p in M.sample_hidden(rows, texts, 10, seed=1)] == ["久安公司"]     # "B 的 r2"指代不唯一


def test_quick_verdict_and_funnel():
    assert M.quick_verdict("答案是项目X。", "项目X") == "correct"
    assert M.quick_verdict("无法回答", "项目X") == "refused"
    assert M.quick_verdict("项目Q", "项目X") is None
    cs = [{"valid": False, "stage": "invalid"}, {"valid": True, "stage": "F1_answerable"},
          {"valid": True, "stage": "F3_closed_book"}, {"valid": True, "stage": "F4_retrieval_hard"},
          {"valid": True, "stage": "passed"}]
    assert M.funnel(cs) == {"sampled": 5, "generated": 4, "F1_answerable": 3, "F2_doc2_needed": 3,
                            "F3_closed_book": 2, "F4_retrieval_hard": 1}


def test_excerpt_and_hard_review():
    assert M.excerpt("前文" * 200 + "碧水源中标项目X", ["碧水源"], width=20).startswith("…")
    assert "**碧水源**" in M.excerpt("甲公司与碧水源签约", ["碧水源", "甲公司"])
    base = {**_row("甲公司", "碧水源", "项目X", "d1", "d2"), "t1": "甲公司 -[合作]-> 碧水源", "t2": "项目X -[中标]-> 碧水源",
            "question": "与甲公司合作的企业中标了什么？", "answer": "项目X", "valid": True,
            "retrieval": {"top1": 0.41}, "graph": {"doc2Rank": 3, "candidates": 12}}
    cs = [{**base, "pid": "hb002", "stage": "passed"}, {**base, "pid": "hb001", "stage": "F4_retrieval_hard"},
          {**base, "pid": "hb003", "stage": "passed", "graph": {"doc2Rank": None, "candidates": 0}}]
    md = M.render_hard_review(cs, {"k_d1": "甲公司与碧水源签约", "k_d2": "碧水源中标项目X"})
    assert md.count("合格：`") == 2 and "hb001" not in md                     # 只列通过全部过滤的题
    assert md.index("hb002|") < md.index("hb003|") and "排第 3" in md and "未找到" in md


def test_parse_review_and_build_hardset():
    base = {**_row("甲公司", "碧水源", "项目X", "d1", "d2"), "t1": "t1", "t2": "t2", "question": "原问题？", "answer": "原答案",
            "stage": "passed"}
    cs = [{**base, "pid": "hb001"}, {**base, "pid": "hb002"}, {**base, "pid": "hb003"},
          {**base, "pid": "hb004", "stage": "F4_retrieval_hard"}]
    md = ("# 抽检\n\n在“合格”处填 y 或 n（如 `hb012|n 指代不唯一`）\n\n"
          "## 1. hb001\n\n**问题**：原问题？\n\n**标准答案**：原答案\n\n合格：`hb001|y`\n\n"
          "## 2. hb002\n\n**问题**：改过的问题？\n\n**标准答案**：原答案\n\n合格：`hb002|Y`\n\n"
          "## 3. hb003\n\n**问题**：原问题？\n\n**标准答案**：原答案\n\n合格：`hb003|n 指代错`\n\n"
          "## 4. hb004\n\n**问题**：原问题？\n\n**标准答案**：原答案\n\n合格：`hb004|y`\n")
    rv = M.parse_review(md)
    assert [p for p, r in rv.items() if r["ok"]] == ["hb001", "hb002", "hb004"] and rv["hb003"]["note"] == "指代错"
    qs = M.build_hardset(cs, rv)
    assert [(q["id"], q["edited"]) for q in qs] == [("hb001", False), ("hb002", True)]   # 未通过自动过滤的不收
    assert qs[1]["question"] == "改过的问题？" and qs[0]["answerable"] and qs[0]["bridge"] == "碧水源"
