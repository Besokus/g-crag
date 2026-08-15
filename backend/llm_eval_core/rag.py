from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

ChatFn = Callable[..., Awaitable[str]]


def _json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def verify_claims(chat: ChatFn, claims: list[str], sources: list[str]) -> list[dict]:
    claims = [str(item).strip() for item in claims or [] if str(item).strip()]
    sources = [str(item).strip() for item in sources or [] if str(item).strip()]
    if not claims or not sources:
        return [{"text": claim, "label": "neutral"} for claim in claims]
    refs = "\n".join(f"[{index + 1}] {source}" for index, source in enumerate(sources))
    prompt = (
        "你是证据核验员。逐条判断声明能否被参考资料支撑。"
        "标签只能是 support、contradict、neutral。严格输出 JSON："
        '{"claims":[{"text":"声明","label":"support|contradict|neutral"}]}\n\n'
        f"参考资料：\n{refs}\n\n声明：\n" + "\n".join(f"- {claim}" for claim in claims)
    )
    try:
        data = _json_object(await chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=800))
        rows = data.get("claims", []) if data else []
        labels = {str(row.get("text", "")): str(row.get("label", "neutral")) for row in rows}
        return [
            {"text": claim, "label": labels.get(claim, "neutral") if labels.get(claim) in {"support", "contradict", "neutral"} else "neutral"}
            for claim in claims
        ]
    except Exception:
        return [{"text": claim, "label": "neutral"} for claim in claims]


async def judge_hallucination(chat: ChatFn, answer: str, sources: list[str]) -> dict:
    sources = [str(item).strip() for item in sources or [] if str(item).strip()]
    if not answer or not sources:
        return {"supported_ratio": None, "hallucination": None, "reason": "答案或资料为空，跳过核查"}
    refs = "\n".join(f"[{index + 1}] {source}" for index, source in enumerate(sources))
    prompt = (
        "你是问答忠实度核查员。提取答案中的原子事实，并逐条判断是否能从资料推出。"
        "严格输出一行 JSON："
        '{"claims":[{"text":"声明","supported":true}],"supported_count":N,"total_count":M}\n\n'
        f"参考资料：\n{refs}\n\n答案：\n{answer}"
    )
    try:
        data = _json_object(await chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=800))
        if not data:
            return {"supported_ratio": None, "hallucination": None, "reason": "核查输出无 JSON（已忽略）"}
        claims = data.get("claims") or []
        total = int(data.get("total_count") or len(claims) or 0)
        supported = int(
            data.get("supported_count")
            if data.get("supported_count") is not None
            else sum(1 for claim in claims if claim.get("supported"))
        )
        if total <= 0:
            return {"supported_ratio": None, "hallucination": None, "reason": "未提取到声明（已忽略）"}
        ratio = round(min(supported, total) / total, 3)
        return {"supported_ratio": ratio, "hallucination": round(1 - ratio, 3), "reason": f"{supported}/{total} 条声明被资料支撑"}
    except Exception as exc:
        return {"supported_ratio": None, "hallucination": None, "reason": f"核查解析失败（已忽略）: {type(exc).__name__}"}


async def judge_context_relevance(chat: ChatFn, query: str, chunks: list[str]) -> dict:
    if not chunks:
        return {"relevance_score": 0.0, "relevant_count": 0, "irrelevant_count": 0, "irrelevant_indices": [], "reason": "无检索结果"}
    refs = "\n".join(f"[{index}] {str(chunk)[:500]}" for index, chunk in enumerate(chunks))
    prompt = (
        "判断每个检索分块与用户问题的相关性，标签为 relevant、partial、irrelevant。"
        '输出 JSON：{"relevance_score":0到1,"labels":{"0":"relevant"},"reason":"说明"}\n\n'
        f"问题：{query}\n分块：\n{refs}"
    )
    try:
        data = _json_object(await chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=500))
        labels = data.get("labels", {}) if data else {}
        return {
            "relevance_score": max(0.0, min(1.0, float((data or {}).get("relevance_score", 0.5)))),
            "relevant_count": sum(1 for value in labels.values() if value == "relevant"),
            "partial_count": sum(1 for value in labels.values() if value == "partial"),
            "irrelevant_count": sum(1 for value in labels.values() if value == "irrelevant"),
            "irrelevant_indices": sorted(int(key) for key, value in labels.items() if value == "irrelevant" and str(key).isdigit()),
            "reason": str((data or {}).get("reason", "")),
        }
    except Exception:
        return {"relevance_score": 0.5, "relevant_count": 0, "partial_count": 0, "irrelevant_count": 0, "irrelevant_indices": [], "reason": "judge 调用失败"}


async def judge_answerability(chat: ChatFn, query: str, chunks: list[str]) -> dict:
    if not chunks:
        return {"answerable": False, "confidence": 1.0, "missing_info": "检索结果为空"}
    refs = "\n".join(f"[{index}] {str(chunk)[:500]}" for index, chunk in enumerate(chunks[:8]))
    prompt = (
        "判断仅凭参考资料能否完整回答用户问题。"
        '输出 JSON：{"answerable":true,"confidence":0到1,"missing_info":""}\n\n'
        f"问题：{query}\n参考资料：\n{refs}"
    )
    try:
        data = _json_object(await chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=300)) or {}
        return {
            "answerable": bool(data.get("answerable", False)),
            "confidence": max(0.0, min(1.0, float(data.get("confidence", 0.0)))),
            "missing_info": str(data.get("missing_info", "")),
        }
    except Exception:
        return {"answerable": False, "confidence": 0.0, "missing_info": "judge 调用失败"}


async def judge_completeness(chat: ChatFn, query: str, answer: str) -> float:
    prompt = (
        "评估答案对问题的完整性。严格输出 JSON："
        '{"score":0到1,"reason":"说明"}\n\n'
        f"问题：{query}\n答案：{answer[:4000]}"
    )
    try:
        data = _json_object(await chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=200)) or {}
        return max(0.0, min(1.0, float(data.get("score", 0.5))))
    except Exception:
        return 0.5


async def judge_citation(chat: ChatFn, answer: str, sources: list[str]) -> dict:
    faith = await judge_hallucination(chat, answer, sources)
    cited = len(re.findall(r"\[\d+\]", answer or ""))
    return {
        "citation_count": cited,
        "faithfulness": faith.get("supported_ratio"),
        "reason": faith.get("reason", ""),
    }
