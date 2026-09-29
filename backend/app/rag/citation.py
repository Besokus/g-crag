"""答案后处理：引用统计与幻觉率启发式估算 + 证据溯源（P4-⑮）。

增强版：
1. 引用编号统计（已有）
2. 句级证据溯源：分析答案中每句话的引用来源
3. 证据高亮标记：返回 sentence→source 映射，供前端渲染
"""
import re


def count_citations(answer: str) -> int:
    """统计答案中出现的引用编号 [n] 数量（去重）。"""
    return len(set(re.findall(r"\[(\d+)\]", answer)))


def estimate_hallucination(answer: str, ref_count: int) -> float:
    """启发式幻觉率：未被引用的参考资料占比（0~1）。"""
    if ref_count <= 0:
        return 1.0
    cited = count_citations(answer)
    covered = min(cited, ref_count)
    return round(max(0.0, 1.0 - covered / ref_count), 3)


# ===== P4-⑮ 证据溯源 & 高亮 =====

# 句末：中文句末标点；ASCII 的 . ! ? 后面紧跟数字或字母时不算句末（小数点 13.57、版本号 V1.2、
# 缩写 U.S.）。原正则把小数点当句号，auto_cite 再把角标插在"句末标点"前 → 13[1].57亿元。
_SENTENCE_END = re.compile(r"[。！？]|[.!?](?![0-9A-Za-z])")


def split_sentences_keep(text: str) -> list[tuple[str, str]]:
    """分句并保留句间空白：[(句子含句末标点, 其后的空白)]。拼回 s+sep 与原文完全一致。"""
    text = text or ""
    out: list[tuple[str, str]] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        end = m.end()
        ws = end
        while ws < len(text) and text[ws].isspace():
            ws += 1
        out.append((text[start:end], text[end:ws]))
        start = ws
    if start < len(text):
        out.append((text[start:], ""))
    return out


def split_sentences(text: str) -> list[str]:
    """按句末标点分句，保留标点。"""
    return [s.strip() for s, _ in split_sentences_keep(text) if s.strip()]


_CITATION_PATTERN = re.compile(r"\[(\d+(?:,\s*\d+)*)\]")


def extract_sentence_sources(sentence: str) -> list[int]:
    """从句子中提取引用的资料编号列表。

    eg: "根据规程[1][2]，主变油温应≤85℃[3]" → [1, 2, 3]
    """
    matches = _CITATION_PATTERN.findall(sentence)
    sources = []
    for m in matches:
        for num in m.split(","):
            try:
                sources.append(int(num.strip()))
            except ValueError:
                pass
    return sources


def evidence_trace(answer: str) -> dict:
    """对答案做句级证据溯源。

    Returns:
        {"sentences": [
            {"text": "句原文", "sources": [1,2], "supported": true/false},
            ...
        ], "totalSupported": int, "totalSentences": int,
        "supportRatio": float}
    """
    if not answer:
        return {"sentences": [], "totalSupported": 0, "totalSentences": 0, "supportRatio": 0.0}

    sentences = split_sentences(answer)
    traced = []
    supported_count = 0
    for s in sentences:
        sources = extract_sentence_sources(s)
        has_support = len(sources) > 0
        if has_support:
            supported_count += 1
        traced.append({
            "text": s,
            "sources": sources,
            "supported": has_support,
            "sourceCount": len(sources),
        })

    return {
        "sentences": traced,
        "totalSupported": supported_count,
        "totalSentences": len(traced),
        "supportRatio": round(supported_count / max(len(traced), 1), 3),
    }


def mark_evidence(answer: str, sources: list[dict]) -> str:
    """给答案做证据标记，为前端渲染准备。

    对每个引用的句子，标注对应的资料名称。
    格式：句子 [来源：文档名]
    """
    if not answer or not sources:
        return answer

    traced = evidence_trace(answer)
    result_parts = []
    for s in traced["sentences"]:
        if s["sources"]:
            names = []
            for idx in s["sources"]:
                if 1 <= idx <= len(sources):
                    doc_name = sources[idx - 1].get("docName", f"资料{idx}")
                    names.append(doc_name)
            if names:
                result_parts.append(f"{s['text']} 📎{','.join(names[:2])}")
                continue
        result_parts.append(s["text"])
    return "".join(result_parts)


def _cosine_mat(sent_vecs: list[list[float]], chunk_vecs: list[list[float]]) -> list[list[float]]:
    """句子×chunk 的 cosine 相似度矩阵 (n_sent, n_chunk)。不假设向量已归一化。"""
    import numpy as np
    if not sent_vecs or not chunk_vecs:
        return []
    S = np.asarray(sent_vecs, dtype=np.float32)
    C = np.asarray(chunk_vecs, dtype=np.float32)
    sn = S / (np.linalg.norm(S, axis=1, keepdims=True) + 1e-10)
    cn = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-10)
    return (sn @ cn.T).tolist()


async def auto_cite(answer: str, contexts: list[dict],
                    threshold: float | None = None) -> tuple[str, dict]:
    """对答案中无 [n] 角标的句子，用向量相似度匹配 chunk 补标。

    返回 (annotated_answer, evidence_trace_dict)。
    contexts: mixed_search _to_item 产物，每项含 "chunk"。
    embed 异常 → degraded，返回 (answer, evidence_trace(answer))。
    """
    from app.config import settings
    from app.services import embedding_service

    if not answer or not contexts:
        return answer, evidence_trace(answer)

    if threshold is None:
        threshold = getattr(settings, "CITATION_SIM_THRESHOLD", 0.6)

    # 保留句间空白（换行、Markdown 分隔线/标题前的空行），原实现拼回时丢掉了全部换行
    parts = split_sentences_keep(answer)
    sentences = [p[0] for p in parts]
    bare_idx = [i for i, s in enumerate(sentences)
                if s.strip() and not extract_sentence_sources(s)]
    annotated = list(sentences)

    if bare_idx:
        try:
            chunk_texts = [c.get("chunk", "") or c.get("text", "") for c in contexts]
            # A1/A4：chunk 内容入库后稳定，按 contexts[i].chunkId 走 chunk 向量缓存
            # （EMBED_CHUNK_CACHE_ENABLE 默认关；关时 chunk_ids 被忽略，行为=现状）
            chunk_ids = [c.get("chunkId") or c.get("chunk_id") or "" for c in contexts]
            chunk_embs = await embedding_service.embed_texts(chunk_texts, chunk_ids=chunk_ids)
            bare_embs = await embedding_service.embed_texts([sentences[i].strip() for i in bare_idx])
            sim = _cosine_mat(bare_embs, chunk_embs)   # (len(bare), len(chunks))
            for row, si in enumerate(bare_idx):
                if not sim[row]:
                    continue
                best_k = max(range(len(sim[row])), key=lambda k: sim[row][k])
                if sim[row][best_k] >= threshold:
                    s = sentences[si]
                    tag = f"[{best_k + 1}]"
                    # 角标插在句末标点之前，避免分句时跑到下一句（与 LLM 原生角标位置一致）
                    body = s.rstrip()
                    tail = s[len(body):]
                    if body and body[-1] in "。！？!.?":
                        annotated[si] = body[:-1] + tag + body[-1] + tail
                    else:
                        annotated[si] = body + tag + tail
        except Exception as e:
            try:
                from app.core.obs import degraded
                degraded("auto_cite_embed", e)
            except Exception:
                pass

    new_answer = "".join(a + sep for a, (_, sep) in zip(annotated, parts))
    return new_answer, evidence_trace(new_answer)