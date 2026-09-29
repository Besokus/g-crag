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
不可答题另抽 15 题并删除其全部标准文档。

评测（逻辑在 gcrag_eval_runner.py；产物在 /app/gcrag_eval/general/runs/<run_id>/）：
    ... gcrag_eval.py run --split calib --modes none --run-id calib1       # 按模式调 /qa/answer（noCache）
    ... gcrag_eval.py judge --run-id calib1                               # 规则 + 判定模型（默认 Claude）
    ... gcrag_eval.py calibrate --run-id calib1                           # 用 none 模式结果扫描阈值
    ... gcrag_eval.py report --run-id test1                               # 指标表 report.md + metrics.json
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

async def import_corpus(out: Path, *, doc_type: str, batch: int, concurrency: int) -> dict:
    """语料入库：服务层直接调用（不受接口限流），导入期间关闭自动建图，完成后重建 BM25。

    幂等：按文件名跳过已完成向量化的文档；已上传但未完成的继续解析/向量化。
    建图另用 scripts/rebuild_kg.py（受控并发 + 断点续跑）。
    批量优化（仅本进程）：Milvus 插入不逐篇 flush（原实现每篇 flush 一次，约 5 秒/篇且产生大量小段），
    全部完成后统一 flush；向量化按 concurrency 并发。
    """
    import asyncio
    import io

    from sqlalchemy import select
    from starlette.datastructures import UploadFile

    from pymilvus import Collection

    from app.clients import milvus_client
    from app.config import settings
    from app.db.session import AsyncSessionLocal
    from app.models.document import Document
    from app.services import bm25_service, document_service

    settings.KG_AUTO_EXTRACT_ENABLE = False   # 只影响本进程：不为每篇文档起后台抽取任务

    def _insert_no_flush(collection_name, vectors, texts, doc_ids, doc_names, chunk_idxs) -> int:
        milvus_client._connect()
        pks = [f"{doc_ids[i]}_{chunk_idxs[i]}" for i in range(len(vectors))]
        Collection(collection_name).insert([pks, list(vectors), list(texts), list(doc_ids),
                                            list(doc_names), list(chunk_idxs)])
        return len(vectors)
    milvus_client.insert_chunks = _insert_no_flush
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
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def _vec(did: str) -> None:
        nonlocal done
        async with sem:
            try:
                async with AsyncSessionLocal() as db:
                    await document_service.vectorize_document(db, did)
            except Exception as e:
                fails.append(f"{did}({type(e).__name__}: {e})"[:300])
            done += 1
            if done % 50 == 0 or done == len(pending_vec):
                print(f"[import] 已向量化 {done}/{len(pending_vec)}", flush=True)
    await asyncio.gather(*(_vec(d) for d in pending_vec))
    milvus_client._connect()   # 没有待向量化文档时本进程尚未连接 Milvus
    for name, _dim in milvus_client.document_collections():
        Collection(name).flush()
    async with AsyncSessionLocal() as db:
        n_bm25 = await bm25_service.rebuild(db)
    return {"corpus": len(files), "uploaded": len(todo_upload), "vectorized": len(pending_vec) - len(fails),
            "vectorizeFailed": fails, "bm25Docs": n_bm25}


async def _eval_cmd(args) -> int:
    import shutil

    import gcrag_eval_runner as R
    questions = R.load_jsonl(args.out / "questions.jsonl")
    run_dir = args.out / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = run_dir / "config.json"
    if args.cmd == "run":
        from app.config import settings
        from app.core.domain_pack import get_pack
        from app.providers.factory import get_llm_provider, llm_cache_tag
        from app.rag.grading import get_thresholds
        from app.rag.scorer import cache_path, get_scorer
        qs = R.select_questions(questions, args.split, args.limit)
        modes = [m.strip() for m in args.modes.split(",") if m.strip()]
        bad = [m for m in modes if m not in R.MODES_ALL]
        if bad:
            raise SystemExit(f"未知模式: {bad}")
        if not cfg_path.exists():
            th = get_thresholds()
            cfg_path.write_text(json.dumps({
                "runId": args.run_id, "split": args.split, "modes": modes, "n": len(qs),
                "thresholds": {"high": th.high, "low": th.low, "source": th.source},
                "scorer": get_scorer().cache_id, "scorerCache": settings.SCORER_CACHE,
                "domainPack": get_pack().name, "llm": llm_cache_tag(None), "commit": args.commit,
                "startedAt": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1), encoding="utf-8")

        async def closed_book(question: str) -> str:
            return await get_llm_provider().chat(
                [{"role": "system", "content": R.CLOSED_BOOK_SYSTEM}, {"role": "user", "content": question}],
                temperature=0.2, max_tokens=settings.LLM_MAX_TOKENS)

        summary = await R.run(qs, modes, run_dir, base=args.base, username=settings.ADMIN_USERNAME,
                              password=settings.ADMIN_PASSWORD, concurrency=args.concurrency,
                              interval=args.interval, timeout=args.timeout, closed_book_fn=closed_book)
        src = cache_path(get_scorer())
        if src.exists():
            shutil.copy(src, run_dir / "scorer_cache.jsonl")
        failed = sum(len(v["failed"]) for v in summary.values())
        print(("RUN_OK " if not failed else "RUN_PARTIAL ") + json.dumps(summary, ensure_ascii=False))
        return 0 if not failed else 1
    if args.cmd == "judge":
        from app.providers.factory import _get_raw_provider
        prov = _get_raw_provider(args.judge_provider)

        model = args.judge_model or getattr(prov, "model", "")

        async def chat(prompt: str) -> str:
            return await prov.chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=300,
                                   model=model)
        counts = await R.judge(run_dir, questions, chat_fn=chat, judge_model=f"{args.judge_provider}:{model}",
                               concurrency=args.concurrency, output=args.output)
        print("JUDGE_OK " + json.dumps(counts, ensure_ascii=False))
        return 0
    judgments = R.load_jsonl(run_dir / "judgments.jsonl")
    verdicts = {(v["id"], v["mode"]): v["verdict"] for v in judgments}
    if args.cmd == "review-sample":
        sample = R.review_sample(judgments, args.ratio)
        records = {(r["id"], r["mode"]): r for m in R.MODES_ALL for r in R.load_jsonl(run_dir / f"{m}.jsonl")}
        (run_dir / "review_sample.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in sample), encoding="utf-8")
        (run_dir / "review_sample.md").write_text(R.review_markdown(sample, questions, records), encoding="utf-8")
        print(f"REVIEW_SAMPLE_OK {len(sample)} 条 → {run_dir / 'review_sample.md'}")
        return 0
    if args.cmd == "review-agree":
        sample = R.load_jsonl(run_dir / "review_sample.jsonl")
        manual = R.parse_manual((run_dir / "review_sample.md").read_text(encoding="utf-8"))
        res = R.agreement(sample, manual)
        (run_dir / "review_agreement.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print("REVIEW_AGREE " + json.dumps(res, ensure_ascii=False))
        return 0
    if args.cmd == "calibrate":
        recs = R.load_jsonl(run_dir / "none.jsonl")
        res = R.calibrate(recs, verdicts, questions, max_false_refusal=args.max_false_refusal,
                          high_precision=args.high_precision)
        (run_dir / "calibration.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print("CALIBRATE_OK " + json.dumps({"chosen": res["chosen"], "bestLow": res["bestLow"],
                                             "bestHigh": res["bestHigh"]}, ensure_ascii=False))
        return 0
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    jm = sorted({v.get("judgeModel") for v in R.load_jsonl(run_dir / "judgments.jsonl")} - {"rule"})
    cfg["judgeModel"] = ", ".join(jm)
    runs = {m: R.load_jsonl(run_dir / f"{m}.jsonl") for m in R.MODES_ALL if (run_dir / f"{m}.jsonl").exists()}
    m = R.metrics(questions, runs, verdicts)
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    (run_dir / "report.md").write_text(R.render_report(m, cfg), encoding="utf-8")
    print(R.render_report(m, cfg))
    return 0


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
    im.add_argument("--batch", type=int, default=5, help="每次上传的文件数（服务层上限 5）")
    im.add_argument("--concurrency", type=int, default=4, help="向量化并发数")
    r = sub.add_parser("run", help="按模式跑评测题（断点续跑）")
    r.add_argument("--out", type=Path, default=OUT_DEFAULT)
    r.add_argument("--run-id", required=True)
    r.add_argument("--split", choices=["calib", "test", "all"], default="test")
    r.add_argument("--modes", default="none,legacy,gcrag,closed_book")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--concurrency", type=int, default=3)
    r.add_argument("--interval", type=float, default=2.2, help="请求起始最小间隔秒（接口限流 30 次/分钟）")
    r.add_argument("--timeout", type=float, default=300)
    r.add_argument("--base", default="http://localhost:8001")
    r.add_argument("--commit", default="", help="宿主机 git rev-parse HEAD（容器内无 .git）")
    j = sub.add_parser("judge", help="判定 correct / wrong / refused")
    j.add_argument("--out", type=Path, default=OUT_DEFAULT)
    j.add_argument("--run-id", required=True)
    j.add_argument("--judge-provider", default="claude", help="默认 Claude：阿里百炼只用于 embedding/rerank/OCR")
    j.add_argument("--judge-model", default="", help="空 = 该 provider 的默认模型")
    j.add_argument("--output", default="judgments.jsonl", help="判定结果文件名（换判定模型对比时另存）")
    j.add_argument("--concurrency", type=int, default=4)
    c = sub.add_parser("calibrate", help="用标定集 none 模式结果扫描 (high, low)")
    c.add_argument("--out", type=Path, default=OUT_DEFAULT)
    c.add_argument("--run-id", required=True)
    c.add_argument("--max-false-refusal", type=float, default=0.1)
    c.add_argument("--high-precision", type=float, default=0.9)
    rp = sub.add_parser("report", help="生成指标表")
    rp.add_argument("--out", type=Path, default=OUT_DEFAULT)
    rp.add_argument("--run-id", required=True)
    for name, hlp in (("review-sample", "按模式×判定分层抽 20% 生成人工抽查表"),
                      ("review-agree", "读回人工判定，计算与判定模型的一致率")):
        x = sub.add_parser(name, help=hlp)
        x.add_argument("--out", type=Path, default=OUT_DEFAULT)
        x.add_argument("--run-id", required=True)
        x.add_argument("--ratio", type=float, default=0.2)
    args = ap.parse_args()
    if args.cmd in ("run", "judge", "calibrate", "report", "review-sample", "review-agree"):
        import asyncio
        sys.path.insert(0, "/app")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        return asyncio.run(_eval_cmd(args))

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
        rep = asyncio.run(import_corpus(args.out, doc_type=args.doc_type, batch=args.batch,
                                        concurrency=args.concurrency))
        print(("IMPORT_OK " if not rep["vectorizeFailed"] else "IMPORT_PARTIAL ") + json.dumps(rep, ensure_ascii=False))
        return 0 if not rep["vectorizeFailed"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
