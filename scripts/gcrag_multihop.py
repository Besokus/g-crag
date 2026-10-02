"""多跳演示集：从图谱跨文档 2 跳路径构造"必须经图谱桥接才能定位证据"的候选题（M6 验收；设计文档 §11）。

与 CRUD-RAG 主测试集分开：本集是针对性构造的子集，报告中须如实标注，只用于展示图谱定位证据在
"检索召回不到桥接文档"时的作用，不与主测试集指标混算。

    docker exec -w /app grid-backend python scripts/gcrag_multihop.py candidates --n 30
    → /app/gcrag_eval/general/multihop/candidates.jsonl + candidates.md（供人工挑选）

路径形如 A -[r1]- B -[r2]- C：r1、r2 出自不同文档，A 与 C 不直接相连，B 非枢纽实体。
题目只提 A、不提 B，答案取自 r2 的出处 chunk（文档 2）。每道候选题探测两件事：
    retrievalHit：普通检索（与问答同一路径，强制打分）top5 是否召回文档 2
    graphHit：    实体链接 + 图谱定位证据（排除检索已召回的 chunk）是否找到文档 2
理想的演示题：retrievalHit=False 且 graphHit=True。

外部调用：Claude 出题（每条路径 1 次）+ 探测时检索内部的改写；百炼 embedding / rerank 每题各 1 次左右。
"""
import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gcrag_eval_runner as R  # noqa: E402

OUT_DEFAULT = Path("/app/gcrag_eval/general/multihop")

PATHS_CYPHER = """
MATCH (a:Entity)-[r1:REL]-(b:Entity)-[r2:REL]-(c:Entity)
WHERE a <> c AND NOT (a)--(c) AND NOT a.name CONTAINS b.name AND NOT c.name CONTAINS b.name
  AND 2 <= coalesce(b.degree, 0) <= $hub AND coalesce(a.degree, 0) <= $hub
  AND size(a.name) >= 3 AND size(b.name) >= 3 AND size(c.name) >= 2
  AND size(coalesce(r1.chunk_ids, [])) > 0 AND size(coalesce(r2.chunk_ids, [])) > 0
  AND none(d IN coalesce(r1.doc_ids, [r1.doc_id]) WHERE d IN coalesce(r2.doc_ids, [r2.doc_id]))
RETURN a.name AS a, b.name AS b, c.name AS c, r1.type AS rel1, r2.type AS rel2,
       startNode(r1).name AS r1from, startNode(r2).name AS r2from,
       r1.chunk_ids[0] AS chunk1, r2.chunk_ids[0] AS chunk2,
       coalesce(r1.doc_ids, [r1.doc_id])[0] AS doc1, coalesce(r2.doc_ids, [r2.doc_id])[0] AS doc2
"""

GEN_PROMPT = """你在为知识图谱问答系统构造"两跳"评测题。下面两段新闻出自不同文档，通过实体「{b}」相连：
- 文档 1 中：{t1}
- 文档 2 中：{t2}

【文档 1 片段】
{c1}

【文档 2 片段】
{c2}

要求：
1. 问题以「{a}」为出发点，**不得出现「{b}」这个名字**（可用文档 1 中的关系来指代它，如"与{a}签约的那家企业"）；
2. **不得出现「{c}」，也不得出现文档 2 中的其他专有名称（人名、机构名、项目名、活动名等）**——问题只能用文档 1 中的信息描述对象，
   只问一个属性（如"它此后中标的项目金额是多少""它在哪个城市设立了新基地"）；
3. 答案只能从文档 2 片段中得到，是具体事实（数字、名称、时间、地点等），一两句话即可；
4. 读者必须先借助文档 1 确定「{b}」，再到文档 2 找答案；只读其中一篇无法回答；
5. 问题自然、无歧义；若两段材料做不出满足以上要求的题，返回 ok=false。

只输出 JSON：{{"ok": true/false, "question": "...", "answer": "...", "why": "一句话说明两跳推理"}}"""


def _triple(frm: str, rel: str, x: str, y: str) -> str:
    """无向遍历拿到的边恢复原方向。"""
    head, tail = (x, y) if frm == x else (y, x)
    return f"{head} -[{rel}]-> {tail}"


def sample_paths(rows: list[dict], n: int, seed: int) -> list[dict]:
    """每个桥接实体、每篇文档 2 至多一条，固定种子打乱后取前 n 条，保证题目分散。"""
    rows = sorted(rows, key=lambda r: (r["b"], r["a"], r["c"], r["chunk2"]))
    random.Random(seed).shuffle(rows)
    seen_b, seen_doc, out = set(), set(), []
    for r in rows:
        if r["b"] in seen_b or r["doc2"] in seen_doc or r["doc1"] in seen_doc:
            continue
        seen_b.add(r["b"])
        seen_doc.update((r["doc1"], r["doc2"]))
        out.append({**r, "pid": f"mh{len(out) + 1:02d}",
                    "t1": _triple(r["r1from"], r["rel1"], r["a"], r["b"]),
                    "t2": _triple(r["r2from"], r["rel2"], r["b"], r["c"])})
        if len(out) >= n:
            break
    return out


def build_prompt(p: dict, c1: str, c2: str, limit: int = 900) -> str:
    return GEN_PROMPT.format(a=p["a"], b=p["b"], c=p["c"], t1=p["t1"], t2=p["t2"], c1=c1[:limit], c2=c2[:limit])


def parse_generated(text: str, bridge: str, end: str = "") -> dict:
    """解析出题结果；JSON 不合法、ok=false、问题里出现桥接实体或终点实体名都视为无效。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    q, a = str(d.get("question", "")).strip(), str(d.get("answer", "")).strip()
    if not d.get("ok") or not q or not a:
        return {"valid": False, "reason": "generator_declined"}
    if bridge in q or (end and end in q):
        return {"valid": False, "reason": "bridge_leaked", "question": q, "answer": a}
    return {"valid": True, "question": q, "answer": a, "why": str(d.get("why", "")).strip()}


def classify(c: dict) -> str:
    if not c.get("valid"):
        return "invalid"
    if not c.get("startLinked"):
        return "no_link"
    if c.get("graphHit") and not c.get("retrievalHit"):
        return "graph_only"          # 理想演示题
    if c.get("graphHit"):
        return "both"
    return "retrieval_only" if c.get("retrievalHit") else "neither"


_RANK = {"graph_only": 0, "both": 1, "retrieval_only": 2, "neither": 3, "no_link": 4, "invalid": 5}


def render_markdown(cands: list[dict]) -> str:
    cands = sorted(cands, key=lambda c: (_RANK[classify(c)], c["pid"]))
    lines = ["# 多跳演示集候选题", "",
             "在每条的“采用”处填 y / n（可直接修改问题与答案的文字），保存后告诉我。",
             "分类：graph_only＝检索 top5 没召回文档 2、图谱找到了（推荐）；both＝两者都找到；"
             "retrieval_only / neither / no_link（问题里的起点实体未被链接）一般不选。", ""]
    for i, c in enumerate(cands, 1):
        kind = classify(c)
        lines += [f"## {i}. {c['pid']}（{kind}）", "",
                  f"**路径**：{c['t1']}（文档 1）；{c['t2']}（文档 2）", ""]
        if c.get("question"):
            lines += [f"**问题**：{c['question']}", "", f"**答案**：{c['answer']}", ""]
        if c.get("why"):
            lines += [f"**推理**：{c['why']}", ""]
        if c.get("valid"):
            lines += [f"**探测**：检索 top5 召回文档 2：{'是' if c.get('retrievalHit') else '否'}；"
                      f"图谱找到文档 2：{'是' if c.get('graphHit') else '否'}"
                      f"（链接实体：{'、'.join(c.get('linked', [])) or '无'}）", ""]
        else:
            lines += [f"**无效**：{c.get('reason', '')}", ""]
        lines += [f"采用：`{c['pid']}|`", ""]
    return "\n".join(lines)


async def _probe(db, question: str, start: str, doc2: str) -> dict:
    from app.core.domain_pack import get_pack
    from app.rag import entity_linking, graph_evidence
    from app.services import retrieval_service
    sr = await retrieval_service.mixed_search_ex(db, question, 5, force_score=True)
    link = await entity_linking.link(question)
    names = [e.name for e in link.entities]
    ev = await graph_evidence.find(db, names, cfg=get_pack().graph_evidence,
                                   exclude_chunk_ids={i.get("chunkId") for i in sr.items if i.get("chunkId")})
    return {"retrievalHit": any(i.get("docId") == doc2 for i in sr.items),
            "retrievalDocs": [i.get("docId") for i in sr.items], "scoringOk": sr.scoring.ok,
            "linked": names, "startLinked": start in names, "graphStatus": ev.status,
            "graphHit": any(i.get("docId") == doc2 for i in ev.candidates)}


async def candidates(out: Path, *, n: int, seed: int, interval: float) -> dict:
    from app.clients import neo4j_client
    from app.core.domain_pack import get_pack
    from app.db.session import AsyncSessionLocal
    from app.providers.factory import _get_raw_provider
    from app.services.chunk_loader import load_items

    out.mkdir(parents=True, exist_ok=True)
    path = out / "candidates.jsonl"
    done = {c["pid"]: c for c in R.load_jsonl(path)} if path.exists() else {}
    hub = get_pack().graph_evidence.hub_degree
    async with neo4j_client._get().session() as s:
        rows = [dict(r) async for r in await s.run(PATHS_CYPHER, hub=hub)]
    paths = sample_paths(rows, n, seed)
    llm, pacer = _get_raw_provider("claude"), R._Pacer(interval)
    for p in paths:
        if p["pid"] in done:
            continue
        async with AsyncSessionLocal() as db:
            items = await load_items(db, [p["chunk1"], p["chunk2"]], expand_parent=False,
                                     origin="graph", sources=[])
            text = {i["chunkId"]: i.get("chunk", "") for i in items}
            await pacer.wait()
            raw = await llm.chat([{"role": "user", "content": build_prompt(
                p, text.get(p["chunk1"], ""), text.get(p["chunk2"], ""))}], temperature=0.3, max_tokens=500)
            rec = {**p, **parse_generated(raw, p["b"], p["c"])}
            if rec["valid"]:
                await pacer.wait()
                rec.update(await _probe(db, rec["question"], p["a"], p["doc2"]))
        rec["kind"] = classify(rec)
        R.append_jsonl(path, rec)
        done[p["pid"]] = rec
        print(f"[mh] {p['pid']} {rec['kind']} {rec.get('question', '')[:40]}", flush=True)
    cands = [done[p["pid"]] for p in paths if p["pid"] in done]
    (out / "candidates.md").write_text(render_markdown(cands), encoding="utf-8")
    counts: dict[str, int] = {}
    for c in cands:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return {"paths": len(rows), "sampled": len(paths), "kinds": counts}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates", help="采样路径 → Claude 出题 → 检索/图谱探测")
    c.add_argument("--out", type=Path, default=OUT_DEFAULT)
    c.add_argument("--n", type=int, default=30)
    c.add_argument("--seed", type=int, default=20261002)
    c.add_argument("--interval", type=float, default=4.0, help="Claude 请求最小间隔秒（代理有频率限制）")
    args = ap.parse_args()
    if args.cmd == "candidates":
        sys.path.insert(0, "/app")
        res = asyncio.run(candidates(args.out, n=args.n, seed=args.seed, interval=args.interval))
        print("MH_CANDIDATES_OK " + json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
