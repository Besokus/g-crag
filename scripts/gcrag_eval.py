"""G-CRAG 评测：基于 CRUD-RAG 构建评测集并导入系统（《设计文档》§16.13；ADR 0007）。

子命令（均在后端容器内运行；compose 挂载 ./data/crud_rag → /app/crud_rag（只读）、
./data/gcrag_eval → /app/gcrag_eval）：

    docker exec grid-backend python /app/scripts/gcrag_eval.py build     # 抽样 + 构造不可答题 + 写语料
    docker exec grid-backend python /app/scripts/gcrag_eval.py import    # 语料入库（上传→解析→向量化）

build 输出（默认 /app/gcrag_eval/general/）：
    questions.jsonl        {id, crudId, event, type, question, answer, goldDocIds, answerable,
                            removedDocIds, split}
    corpus/<docId>.txt     标准文档（题目内联的 news* 片段原文）+ 干扰文档（80000_docs 中的无关新闻）
    removed_docs.json      为构造不可答题而删除、不入语料的标准文档
    build_report.json      参数、各环节计数，供报告引用
    related_samples.jsonl  "相关事件剔除"的抽查样本（被剔除的 + 只差一点被剔除的），用于人工校准阈值

构造规则见《设计文档》§16.13：按事件抽样（各题事件互不相同、语义上不属于同一事件簇）、标准文档
用内联片段、干扰文档排除与任一抽样事件相关的报道（字面 + 语义两道关，语义用云端 embedding）、
不可答题另抽 15 题并删除其全部标准文档。run / judge / report 在 M7 实现。
"""
import argparse
import bisect
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

SRC_DEFAULT = Path("/app/crud_rag")
OUT_DEFAULT = Path("/app/gcrag_eval/general")
QA_FILES = {"single": "1doc_QA.json", "double": "2docs_QA.json", "triple": "3docs_QA.json"}
NEWS_KEYS = {"single": ["news1"], "double": ["news1", "news2"], "triple": ["news1", "news2", "news3"]}

# ---------------- 文本工具（纯函数，单测覆盖） ----------------

# 常见繁体独有字：比例超阈值视为繁体重复稿（语料中存在简繁两份的同一篇新闻）
_TRAD = set("這們國會說時來對發後經與為無關當從點學間現還麼應機開東體與實將說從專員難頭樂邊運過種話讓總義寫區")
_QUOTED = re.compile(r"[《“\"「]([^《》“”\"「」]{2,30})[》”\"」]")
_PUNCT = re.compile(r"[\s\W_]+", re.U)


def doc_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def is_traditional(text: str, ratio: float = 0.01) -> bool:
    if not text:
        return False
    hits = sum(1 for ch in text if ch in _TRAD)
    return hits / max(1, len(text)) >= ratio


def near_dup_key(text: str) -> str:
    """近重复键：去空白与标点后的前 200 字（转载稿常只差标点、空格或来源行）。"""
    return _PUNCT.sub("", text)[:200]


def quoted_names(text: str) -> set[str]:
    return {m.strip() for m in _QUOTED.findall(text or "") if len(m.strip()) >= 4}


def grams4(text: str) -> set[str]:
    t = _PUNCT.sub("", text or "")
    return {t[i:i + 4] for i in range(len(t) - 3) if not t[i:i + 4].isdigit()}


def event_terms(event: str, df: Counter, name_df: Counter, rare_max: int) -> dict:
    """某事件的字面识别特征：事件描述中的专名（书名号/引号内）与稀有 4 字片段，均要求语料行频 ≤ rare_max。

    只取事件描述、不取标准文档里的引号内容：后者多为泛化词（"人民日报""一带一路"），会误剔大量无关新闻。
    """
    names = {n for n in quoted_names(event) if name_df.get(n, 0) <= rare_max}
    rare = {g for g in grams4(event) if 0 < df.get(g, 0) <= rare_max}
    return {"names": names, "rare": rare}


def relatedness(line: str, terms: dict) -> tuple[int, list[str]]:
    """返回 (字面相关度, 命中特征)：命中任一专名记 99；否则为命中的稀有片段数。"""
    for n in terms["names"]:
        if n in line:
            return 99, [n]
    compact = _PUNCT.sub("", line)
    hits = [g for g in terms["rare"] if g in compact]
    return len(hits), hits


def _unit(m):
    import numpy as np
    m = np.asarray(m, dtype=np.float32)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def _event_text(item: dict) -> str:
    return f"{item['event']} {item['questions']}"[:500]


def _body(text: str) -> str:
    return text.split("正文：", 1)[-1].strip()[:500]


# ---------------- build ----------------

def _load_qa(src: Path) -> dict[str, list[dict]]:
    base = src / "unzipped" / "CRUD_Data"
    return {t: json.loads((base / f).read_text(encoding="utf-8")) for t, f in QA_FILES.items()}


def _load_corpus(src: Path) -> list[str]:
    lines: list[str] = []
    for p in sorted((src / "repo" / "data" / "80000_docs").glob("documents_dup_part_*")):
        lines.extend(line.rstrip("\n") for line in p.read_text(encoding="utf-8").split("\n"))
    return lines


class _Locator:
    """把片段定位到 80000_docs 的行号（CRUD-RAG 的多文档片段是从拼接语料中切出的）。"""

    def __init__(self, lines: list[str]):
        self.big = "\n".join(lines)
        self.starts, pos = [], 0
        for line in lines:
            self.starts.append(pos)
            pos += len(line) + 1

    def line_of(self, passage: str) -> int | None:
        head = passage.strip()[:60]
        if len(head) < 20:
            return None
        p = self.big.find(head)
        return None if p < 0 else bisect.bisect_right(self.starts, p) - 1


def _gold_texts(item: dict, qtype: str) -> list[str]:
    return [item[k].strip() for k in NEWS_KEYS[qtype]]


def _valid(item: dict, qtype: str, loc: _Locator) -> bool:
    if not (item.get("questions", "").strip() and item.get("answers", "").strip()):
        return False
    golds = _gold_texts(item, qtype)
    if any(len(g) < 150 or is_traditional(g) for g in golds):
        return False
    if qtype == "single":
        return True
    lines = [loc.line_of(g) for g in golds]
    return None not in lines and len(set(lines)) == len(lines)   # 各片段出自不同原文


async def build(src: Path, out: Path, *, embed, seed: int, per_type: int, n_unans_double: int,
                n_unans_triple: int, corpus_size: int, rare_max: int, calib_ratio: float,
                event_sim: float, doc_sim: float) -> dict:
    """构建评测集。embed：async (list[str]) -> list[vector]，用于事件去重与干扰文档语义过滤。

    相关性判定两道关：字面（专名 / ≥2 个稀有片段）+ 语义（余弦相似度 ≥ 阈值）。
    事件之间 ≥ event_sim 视为同一事件簇，只保留一个；干扰文档与任一事件 ≥ doc_sim 即剔除。
    """
    import numpy as np

    t0 = time.time()
    rng = random.Random(seed)
    qa = _load_qa(src)
    lines = _load_corpus(src)
    loc = _Locator(lines)
    print(f"[build] 题目 {', '.join(f'{t}={len(v)}' for t, v in qa.items())}；语料行 {len(lines)}", flush=True)

    # 1) 候选池：每类独立多抽一些（事件互斥在第 3 步正式入选时保证）
    need = {"triple": per_type + n_unans_triple, "double": per_type + n_unans_double, "single": per_type}
    pools: dict[str, list[dict]] = {}
    for qtype in ("triple", "double", "single"):
        items = qa[qtype][:]
        rng.shuffle(items)
        pool = []
        for it in items:
            if _valid(it, qtype, loc):
                pool.append(it)
                if len(pool) >= need[qtype] * 3 + sum(need.values()):   # 预留被其他类占用的事件
                    break
        pools[qtype] = pool

    # 2) 字面特征的稀有度：候选事件描述中的 4 字片段与专名在语料中的行频
    cand_grams, cand_names = set(), set()
    for pool in pools.values():
        for it in pool:
            cand_grams |= grams4(it["event"])
            cand_names |= quoted_names(it["event"])
    df: Counter = Counter()
    for line in lines:
        compact = _PUNCT.sub("", line)
        df.update({compact[i:i + 4] for i in range(len(compact) - 3)} & cand_grams)
    name_df = Counter({n: loc.big.count(n) for n in cand_names})
    print(f"[build] 字面特征统计完成（{len(cand_grams)} 个片段、{len(cand_names)} 个专名，{time.time() - t0:.0f}s）",
          flush=True)

    # 3) 逐类正式入选：事件互不相同、字面不相关、语义上不属于同一事件簇
    pool_items = {it["id"] + qtype: it for qtype, pool in pools.items() for it in pool}
    keys = list(pool_items)
    ev_vec = dict(zip(keys, _unit(await embed([_event_text(pool_items[k]) for k in keys]))))
    chosen: dict[str, list[dict]] = {}
    accepted: list[tuple[dict, "np.ndarray"]] = []
    accepted_events: list[str] = []
    used_ids: set[str] = set()
    event_sim_rejects: list[dict] = []
    for qtype in ("triple", "double", "single"):   # 三文档条件最严，先选
        chosen[qtype] = []
        for it in pools[qtype]:
            if it["id"] in used_ids:
                continue
            golds = _gold_texts(it, qtype)
            v = ev_vec[it["id"] + qtype]
            if accepted:
                sims = np.array([float(v @ av) for _, av in accepted])
                j = int(sims.argmax())
                if sims[j] >= event_sim:
                    if len(event_sim_rejects) < 30:
                        event_sim_rejects.append({"sim": round(float(sims[j]), 3), "event": it["event"][:80],
                                                  "conflictWith": accepted_events[j][:80]})
                    continue
                if any(max(relatedness(g, t)[0] for g in golds) >= 2 for t, _ in accepted):
                    continue
            terms = event_terms(it["event"], df, name_df, rare_max)
            it = {**it, "_terms": terms}
            used_ids.add(it["id"])
            chosen[qtype].append(it)
            accepted.append((terms, v))
            accepted_events.append(it["event"])
            if len(chosen[qtype]) >= need[qtype]:
                break
        if len(chosen[qtype]) < need[qtype]:
            raise RuntimeError(f"{qtype} 可用题不足：{len(chosen[qtype])}/{need[qtype]}")
    print(f"[build] 入选事件 {len(accepted)} 个（语义同簇剔除 {len(event_sim_rejects)}+）", flush=True)

    # 4) 题目、标准文档、不可答题
    questions, gold_docs, removed = [], {}, []
    for qtype in ("single", "double", "triple"):
        items = chosen[qtype]
        n_unans = {"double": n_unans_double, "triple": n_unans_triple}.get(qtype, 0)
        answerable, unans = items[:per_type], items[per_type:per_type + n_unans]
        for group, is_ans in ((answerable, True), (unans, False)):
            n_calib = round(len(group) * calib_ratio)
            for k, it in enumerate(group):
                golds = _gold_texts(it, qtype)
                ids = [doc_id(g) for g in golds]
                for i, g in zip(ids, golds):
                    gold_docs[i] = g
                removed_ids_q: list[str] = []
                if not is_ans:
                    # 删除该题全部标准文档：多文档题的几篇片段常是同一事件的不同报道、内容高度重合
                    # （实测被删片段与剩余片段余弦相似度多在 0.74~0.97），只删一篇答案往往仍在语料中
                    removed_ids_q = list(ids)
                    removed.extend({"docId": i, "crudId": it["id"], "text": gold_docs[i]} for i in ids)
                prefix = {"single": "s", "double": "d", "triple": "t"}[qtype] + ("" if is_ans else "u")
                questions.append({
                    "id": f"{prefix}{k + 1:02d}", "crudId": it["id"], "event": it["event"], "type": qtype,
                    "question": it["questions"].strip(), "answer": it["answers"].strip(),
                    "goldDocIds": ids, "answerable": is_ans, "removedDocIds": removed_ids_q,
                    "split": "calib" if k < n_calib else "test",
                })
    removed_ids = {r["docId"] for r in removed}
    corpus = {i: t for i, t in gold_docs.items() if i not in removed_ids}

    # 5) 干扰文档：长度、近重复、繁体重复、字面相关 → 候选（多取一倍）→ 语义相关 → 随机取足
    all_terms = [t for t, _ in accepted]
    ev_mat = np.stack([v for _, v in accepted])
    seen_keys = {near_dup_key(t) for t in gold_docs.values()}
    stats = Counter()
    lexical_rejects, lexical_near, candidates = [], [], []
    order = list(range(len(lines)))
    rng.shuffle(order)
    target = corpus_size - len(corpus)
    for idx in order:
        line = lines[idx].strip()
        if not (200 <= len(line) <= 2000):
            stats["length"] += 1
            continue
        key = near_dup_key(line)
        if key in seen_keys:
            stats["near_dup"] += 1
            continue
        if is_traditional(line):
            stats["traditional"] += 1
            continue
        best, hits = 0, []
        for t in all_terms:
            score, h = relatedness(line, t)
            if score > best:
                best, hits = score, h
            if best >= 99:
                break
        if best >= 2:
            stats["lexical_related"] += 1
            if len(lexical_rejects) < 30:
                lexical_rejects.append({"kind": "lexical_rejected", "hits": hits, "text": line[:300]})
            continue
        if best == 1 and len(lexical_near) < 30:
            lexical_near.append({"kind": "lexical_near_miss", "hits": hits, "text": line[:300]})
        seen_keys.add(key)
        candidates.append(line)
        if len(candidates) >= target * 2:
            break

    doc_mat = _unit(await embed([_body(c) for c in candidates]))
    sims = doc_mat @ ev_mat.T
    best_sim, best_ev = sims.max(axis=1), sims.argmax(axis=1)
    semantic_samples, distractors = [], []
    for i, line in enumerate(candidates):
        s = float(best_sim[i])
        if doc_sim - 0.05 <= s < doc_sim + 0.05 and len(semantic_samples) < 40:
            semantic_samples.append({"kind": "semantic_rejected" if s >= doc_sim else "semantic_near_miss",
                                     "sim": round(s, 3), "event": accepted_events[int(best_ev[i])][:80],
                                     "text": _body(line)[:200]})
        if s >= doc_sim:
            stats["semantic_related"] += 1
            continue
        if len(distractors) < target:
            distractors.append(line)
    if len(distractors) < target:
        raise RuntimeError(f"干扰文档不足：{len(distractors)}/{target}（放宽 --doc-sim 或增大候选）")
    for line in distractors:
        corpus[doc_id(line)] = line
    stats["distractors"] = len(distractors)

    # 6) 写出
    (out / "corpus").mkdir(parents=True, exist_ok=True)
    for old in (out / "corpus").glob("*.txt"):
        old.unlink()
    for i, text in corpus.items():
        (out / "corpus" / f"{i}.txt").write_text(text, encoding="utf-8")
    with (out / "questions.jsonl").open("w", encoding="utf-8") as f:
        for q in questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    (out / "removed_docs.json").write_text(json.dumps(removed, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "related_samples.jsonl").open("w", encoding="utf-8") as f:
        for s in lexical_rejects + lexical_near + semantic_samples + [{"kind": "event_cluster_rejected", **e}
                                                                      for e in event_sim_rejects]:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    report = {
        "builtAt": time.strftime("%Y-%m-%d %H:%M:%S"), "seed": seed, "source": "CRUD-RAG CRUD_Data (3199×3)",
        "params": {"perType": per_type, "unanswerableDouble": n_unans_double, "unanswerableTriple": n_unans_triple,
                   "corpusSize": corpus_size, "rareMax": rare_max, "calibRatio": calib_ratio,
                   "eventSim": event_sim, "docSim": doc_sim},
        "questions": dict(Counter(f"{q['type']}{'' if q['answerable'] else '_unanswerable'}" for q in questions)),
        "splits": dict(Counter(q["split"] for q in questions)),
        "corpus": {"total": len(corpus), "gold": len(corpus) - len(distractors), "distractors": len(distractors),
                   "removedGold": len(removed)},
        "eventClusterRejected": len(event_sim_rejects),
        "distractorFilter": dict(stats),
        "distractorMaxSim": {p: round(float(np.percentile(best_sim, p)), 3) for p in (50, 90, 99)},
        "elapsedSec": round(time.time() - t0, 1),
    }
    (out / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


# ---------------- import ----------------

async def import_corpus(out: Path, *, doc_type: str, batch: int) -> dict:
    """语料入库：服务层直接调用（不受接口限流），导入期间关闭自动建图，完成后重建 BM25。

    幂等：按文件名跳过已完成向量化的文档；已上传但未完成的继续解析/向量化。
    建图另用 scripts/rebuild_kg.py（受控并发 + 断点续跑）。
    """
    import io

    from sqlalchemy import select
    from starlette.datastructures import UploadFile

    from app.config import settings
    from app.db.session import AsyncSessionLocal
    from app.models.document import Document
    from app.services import bm25_service, document_service

    settings.KG_AUTO_EXTRACT_ENABLE = False   # 只影响本进程：不为每篇文档起后台抽取任务
    files = sorted((out / "corpus").glob("*.txt"))
    async with AsyncSessionLocal() as db:
        existing = {d.doc_name: d for d in (await db.execute(select(Document))).scalars().all()}
    todo_upload = [p for p in files if p.name not in existing]
    print(f"[import] 语料 {len(files)} 篇；待上传 {len(todo_upload)}")
    for i in range(0, len(todo_upload), batch):
        chunk = todo_upload[i:i + batch]
        async with AsyncSessionLocal() as db:
            res = await document_service.upload_documents(
                db, [UploadFile(file=io.BytesIO(p.read_bytes()), filename=p.name) for p in chunk],
                doc_type, "gcrag_eval")
        if res.get("failList"):
            raise RuntimeError(f"上传失败：{res['failList']}")
        if (i // batch) % 20 == 0:
            print(f"[import] 已上传 {min(i + batch, len(todo_upload))}/{len(todo_upload)}", flush=True)

    names = {p.name for p in files}
    async with AsyncSessionLocal() as db:
        docs = [d for d in (await db.execute(select(Document))).scalars().all() if d.doc_name in names]
    pending_parse = [d.id for d in docs if d.status == "pending"]
    pending_vec = [d.id for d in docs if d.status != "vectorized"]
    print(f"[import] 待解析 {len(pending_parse)}；待向量化 {len(pending_vec)}")
    for i in range(0, len(pending_parse), 20):
        async with AsyncSessionLocal() as db:
            await document_service.parse_documents(db, pending_parse[i:i + 20])
    fails: list[str] = []
    for i in range(0, len(pending_vec), 20):
        async with AsyncSessionLocal() as db:
            r = await document_service.vectorize_documents(db, pending_vec[i:i + 20])
        fails.extend(r["failList"])
        print(f"[import] 已向量化 {min(i + 20, len(pending_vec))}/{len(pending_vec)}", flush=True)
    async with AsyncSessionLocal() as db:
        n_bm25 = await bm25_service.rebuild(db)
    return {"corpus": len(files), "uploaded": len(todo_upload), "vectorized": len(pending_vec) - len(fails),
            "vectorizeFailed": fails, "bm25Docs": n_bm25}


def main() -> int:
    ap = argparse.ArgumentParser(description="G-CRAG 评测集构建与导入")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="抽样、构造不可答题、写出语料")
    b.add_argument("--src", type=Path, default=SRC_DEFAULT)
    b.add_argument("--out", type=Path, default=OUT_DEFAULT)
    b.add_argument("--seed", type=int, default=20260927)
    b.add_argument("--per-type", type=int, default=30)
    b.add_argument("--unans-double", type=int, default=8)
    b.add_argument("--unans-triple", type=int, default=7)
    b.add_argument("--corpus-size", type=int, default=1000)
    b.add_argument("--rare-max", type=int, default=30, help="稀有 4 字片段的最大语料行频")
    b.add_argument("--calib-ratio", type=float, default=1 / 3)
    b.add_argument("--event-sim", type=float, default=0.65, help="事件之间余弦相似度 ≥ 此值视为同一事件簇")
    b.add_argument("--doc-sim", type=float, default=0.65, help="干扰文档与任一事件余弦相似度 ≥ 此值即剔除")
    im = sub.add_parser("import", help="语料入库（上传→解析→向量化→重建 BM25）")
    im.add_argument("--out", type=Path, default=OUT_DEFAULT)
    im.add_argument("--doc-type", default="新闻")
    im.add_argument("--batch", type=int, default=5)
    args = ap.parse_args()

    if args.cmd == "build":
        import asyncio
        sys.path.insert(0, "/app")
        from app.providers.factory import get_embedding_provider

        provider = get_embedding_provider("qwen")
        rep = asyncio.run(build(args.src, args.out, embed=provider.embed, seed=args.seed,
                                per_type=args.per_type, n_unans_double=args.unans_double,
                                n_unans_triple=args.unans_triple, corpus_size=args.corpus_size,
                                rare_max=args.rare_max, calib_ratio=args.calib_ratio,
                                event_sim=args.event_sim, doc_sim=args.doc_sim))
        print("BUILD_OK", json.dumps(rep, ensure_ascii=False))
        return 0
    if args.cmd == "import":
        import asyncio
        sys.path.insert(0, "/app")
        rep = asyncio.run(import_corpus(args.out, doc_type=args.doc_type, batch=args.batch))
        print(("IMPORT_OK " if not rep["vectorizeFailed"] else "IMPORT_PARTIAL ") + json.dumps(rep, ensure_ascii=False))
        return 0 if not rep["vectorizeFailed"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
