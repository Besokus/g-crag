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


# ---------------- 隐藏桥接子集（hard，2026-10-02；构造规则见调研文档 §4.2，按 MuSiQue 思路按构造去捷径）----------------
# 与上面 candidates 的区别：doc2 全文不提 A、doc1 不提 C（桥接真正"藏"在两篇之间）；B 的 r2 同类关系不超过 3 个；
# 出题时给 doc2 以问出具体、唯一的事实，但题面只能用 doc1 信息指代 B（试跑 1 不给 doc2，题目宽泛、答案不唯一，
# 已存档于 hard_pilot1/；题面泄漏由 F4 冻结检索与 leakDoc2 兜底）；
# 逐题过滤 F1 两篇都给可答 → F2 只给 doc1 不可答 → F3 闭卷不可答 → F4 冻结检索 top5 无同时含 B、C 的 chunk。
# 先免费、再 Claude、最后百炼，任一关不过即停，漏斗全部落盘。

HARD_GEN_PROMPT = """你在为知识图谱问答系统构造"两跳"评测题。两篇新闻通过实体「{b}」相连：
- 文档 1 中：{t1}
- 文档 2 中：{t2}

【文档 1 片段】
{c1}

【文档 2 片段】
{c2}

要求：
1. 问题以「{a}」为出发点，**只能用文档 1 中的信息指代「{b}」**（如"与{a}签约的那家企业"），不得出现「{b}」；
2. 问文档 2 中关于「{b}」的**一个具体事实**：金额、数量、日期、地点、人名、机构名、项目名等，答案唯一、可核对；
   不要问"举办了什么活动""对哪里产生了影响"这类答案宽泛或不唯一的问题；
3. 描述所问的事实时只用通用词（如"它此后中标的项目金额是多少"），**不得复述文档 2 中的事件细节、专有名称或特征词**；
4. 只读文档 1 无法回答，必须再找到讲「{b}」的文档 2；
5. 做不出满足以上要求的题，返回 ok=false。

只输出 JSON：{{"ok": true/false, "question": "...", "answer": "简短答案"}}"""

ANSWER_PROMPT = """根据【资料】回答【问题】，答案尽量简短（一个名称、数字或短语即可）。资料不足以回答时只输出"无法回答"。

【资料】
{ctx}

【问题】{question}"""

CLOSED_PROMPT = """不借助任何资料，凭你已有的知识回答下面的问题，答案尽量简短。不知道或无法确定时只输出"无法回答"。

【问题】{question}"""

_REFUSE = ("无法回答", "无法确定", "无法确认", "资料不足", "未提及", "没有提到", "不知道")


def _norm(s: str) -> str:
    return re.sub(r"[\s“”\"'《》「」（）()、，,。.]", "", s or "").lower()


def contains(text: str, name: str) -> bool:
    return bool(name) and _norm(name) in _norm(text)


def leak_ratio(question: str, doc: str, n: int = 3) -> float:
    """题面字符 n-gram 在文档中出现的比例（题面短、文档长，用覆盖率而不是 Jaccard）。"""
    q = _norm(question)
    grams = {q[i:i + n] for i in range(len(q) - n + 1)}
    if not grams:
        return 0.0
    d = _norm(doc)
    return round(sum(g in d for g in grams) / len(grams), 4)


def sample_hidden(rows: list[dict], texts: dict[str, str], n: int, seed: int, max_per_bridge: int = 3,
                  max_fanout: int = 3) -> list[dict]:
    """doc2 全文不含 A、doc1 全文不含 C；B 经 r2 同类关系连接的实体不超过 max_fanout 个（否则"B 的 r2"
    指代不唯一）；每个桥接实体至多 max_per_bridge 条，每篇 doc2 至多一条。"""
    rows = sorted(rows, key=lambda r: (r["b"], r["a"], r["c"], r["chunk2"]))
    random.Random(seed).shuffle(rows)
    per_b: dict[str, int] = {}
    seen_doc2, out = set(), []
    for r in rows:
        d1, d2 = texts.get(r["doc1"], ""), texts.get(r["doc2"], "")
        if not d1 or not d2 or contains(d2, r["a"]) or contains(d1, r["c"]):
            continue
        if r.get("r2Fanout", 1) > max_fanout:
            continue
        if per_b.get(r["b"], 0) >= max_per_bridge or r["doc2"] in seen_doc2:
            continue
        per_b[r["b"]] = per_b.get(r["b"], 0) + 1
        seen_doc2.add(r["doc2"])
        out.append({**r, "pid": f"hb{len(out) + 1:03d}",
                    "t1": _triple(r["r1from"], r["rel1"], r["a"], r["b"]),
                    "t2": _triple(r["r2from"], r["rel2"], r["b"], r["c"])})
        if len(out) >= n:
            break
    return out


def quick_verdict(answer: str, gold: str) -> str | None:
    """先用规则判：含标准答案 → correct；表示无法回答 → refused；其余交给判定模型。"""
    if contains(answer, gold):
        return "correct"
    if not (answer or "").strip() or any(k in answer for k in _REFUSE):
        return "refused"
    return None


FILTERS = ("F1_answerable", "F2_doc2_needed", "F3_closed_book", "F4_retrieval_hard")


def funnel(cands: list[dict]) -> dict:
    """各关剩余数；stage = 首个未通过的关（passed = 全部通过）。"""
    out = {"sampled": len(cands), "generated": sum(1 for c in cands if c.get("valid"))}
    alive = [c for c in cands if c.get("valid")]
    for f in FILTERS:
        alive = [c for c in alive if c.get("stage") not in ("invalid",) + FILTERS[:FILTERS.index(f) + 1]]
        out[f] = len(alive)
    return out


async def hard(out: Path, *, n: int, seed: int, interval: float) -> dict:
    from sqlalchemy import select

    from app.clients import neo4j_client
    from app.core.domain_pack import get_pack
    from app.db.session import AsyncSessionLocal
    from app.models.chunk import Chunk
    from app.providers.factory import _get_raw_provider
    from app.rag import entity_linking, graph_evidence
    from app.services import retrieval_service
    from app.services.chunk_loader import load_items

    out.mkdir(parents=True, exist_ok=True)
    path = out / "hard_candidates.jsonl"
    done = {c["pid"]: c for c in R.load_jsonl(path)} if path.exists() else {}
    hub = get_pack().graph_evidence.hub_degree
    async with neo4j_client._get().session() as s:
        rows = [dict(r) async for r in await s.run(PATHS_CYPHER, hub=hub)]
        fan = {(r["b"], r["t"]): r["n"] async for r in await s.run(
            "MATCH (b:Entity)-[r:REL]-(x:Entity) RETURN b.name AS b, r.type AS t, count(DISTINCT x) AS n")}
    rows = [{**r, "r2Fanout": fan.get((r["b"], r["rel2"]), 1)} for r in rows]
    texts: dict[str, list[str]] = {}
    async with AsyncSessionLocal() as db:
        # 边上的 doc_ids 与 chunk_ids 不保证按位对齐：以出处 chunk 实际所属文档为准
        cids = sorted({r[k] for r in rows for k in ("chunk1", "chunk2")})
        owner = dict((await db.execute(select(Chunk.id, Chunk.doc_id).where(Chunk.id.in_(cids)))).all())
        rows = [{**r, "doc1": owner[r["chunk1"]], "doc2": owner[r["chunk2"]]} for r in rows
                if r["chunk1"] in owner and r["chunk2"] in owner and owner[r["chunk1"]] != owner[r["chunk2"]]]
        doc_ids = sorted({r[k] for r in rows for k in ("doc1", "doc2")})
        for did, content in (await db.execute(select(Chunk.doc_id, Chunk.content).where(Chunk.doc_id.in_(doc_ids))
                                              .order_by(Chunk.doc_id, Chunk.chunk_idx))).all():
            texts.setdefault(did, []).append(content or "")
    full = {d: "\n".join(v) for d, v in texts.items()}
    eligible = len(sample_hidden(rows, full, 10 ** 9, seed))     # 满足隐藏桥接条件的全部路径（不调用模型）
    paths = sample_hidden(rows, full, n, seed)
    llm, pacer = _get_raw_provider("claude"), R._Pacer(interval)

    async def ask(prompt: str, max_tokens: int = 200) -> str:
        await pacer.wait()
        return await llm.chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=max_tokens)

    async def verdict(question: str, gold: str, answer: str) -> str:
        v = quick_verdict(answer, gold)
        if v:
            return v
        return R.parse_verdict(await ask(R.JUDGE_PROMPT.format(question=question, gold=gold, answer=answer)))[0]

    for p in paths:
        if p["pid"] in done:
            continue
        rec = dict(p)
        async with AsyncSessionLocal() as db:
            items = await load_items(db, [p["chunk1"], p["chunk2"]], expand_parent=False, origin="graph", sources=[])
            text = {i["chunkId"]: i.get("chunk", "") for i in items}
            c1, c2 = text.get(p["chunk1"], ""), text.get(p["chunk2"], "")
            raw = await ask(HARD_GEN_PROMPT.format(a=p["a"], b=p["b"], t1=p["t1"], t2=p["t2"],
                                                   c1=c1[:900], c2=c2[:900]), max_tokens=400)
            rec.update(parse_generated(raw, p["b"]))
            rec["stage"] = "invalid" if not rec["valid"] else "passed"
            if rec["valid"]:
                q, gold = rec["question"], rec["answer"]
                rec["leakDoc2"], rec["leakDoc1"] = leak_ratio(q, full.get(p["doc2"], "")), leak_ratio(q, c1)
                checks = [("F1_answerable", f"{c1[:1500]}\n\n{c2[:1500]}", "correct"),
                          ("F2_doc2_needed", c1[:1500], None),
                          ("F3_closed_book", None, None)]
                for name, ctx, want in checks:
                    prompt = CLOSED_PROMPT.format(question=q) if ctx is None else ANSWER_PROMPT.format(ctx=ctx, question=q)
                    ans = await ask(prompt)
                    v = await verdict(q, gold, ans)
                    rec[name] = {"answer": ans[:300], "verdict": v}
                    if (v != "correct") if want == "correct" else (v == "correct"):
                        rec["stage"] = name
                        break
                if rec["stage"] == "passed":
                    sr = await retrieval_service.mixed_search_ex(db, q, 5, force_score=True)
                    both = [i for i in sr.items if contains(i.get("chunk", ""), p["b"]) and contains(i.get("chunk", ""), p["c"])]
                    rec["retrieval"] = {"docs": [i.get("docId") for i in sr.items], "top1": sr.items[0]["score"] if sr.items else None,
                                        "scoringOk": sr.scoring.ok, "doc2InTop5": any(i.get("docId") == p["doc2"] for i in sr.items),
                                        "chunkWithBC": len(both)}
                    link = await entity_linking.link(q)
                    names = [e.name for e in link.entities]
                    ev = await graph_evidence.find(db, names, cfg=get_pack().graph_evidence, exclude_chunk_ids=set())
                    gdocs = [i.get("docId") for i in ev.candidates]
                    rec["graph"] = {"linked": names, "startLinked": p["a"] in names, "status": ev.status,
                                    "doc2Rank": gdocs.index(p["doc2"]) + 1 if p["doc2"] in gdocs else None,
                                    "candidates": len(gdocs)}
                    if both or rec["retrieval"]["doc2InTop5"]:
                        rec["stage"] = "F4_retrieval_hard"
        R.append_jsonl(path, rec)
        done[p["pid"]] = rec
        print(f"[hb] {p['pid']} {rec['stage']} {rec.get('question', '')[:40]}", flush=True)
    cands = [done[p["pid"]] for p in paths if p["pid"] in done]
    res = {"paths": len(rows), "eligible": eligible, "funnel": funnel(cands)}
    (out / "hard_funnel.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hard", help="隐藏桥接子集：枚举 → 出题（不给 doc2）→ F1~F4 过滤")
    h.add_argument("--out", type=Path, default=OUT_DEFAULT)
    h.add_argument("--n", type=int, default=40)
    h.add_argument("--seed", type=int, default=20261003)
    h.add_argument("--interval", type=float, default=4.0)
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
    if args.cmd == "hard":
        sys.path.insert(0, "/app")
        res = asyncio.run(hard(args.out, n=args.n, seed=args.seed, interval=args.interval))
        print("MH_HARD_OK " + json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
