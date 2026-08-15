from __future__ import annotations

import html
import json

from .artifacts import upload
from .config import settings


def write_report(run_id: str, content: dict) -> tuple[str, str, str]:
    directory = settings.artifact_path() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"
    html_path = directory / "report.html"
    json_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    dims = content.get("dimensions", [])
    markdown = [
        f"# LLM as a User 评测报告 {run_id}", "",
        f"- 结论：**{content.get('verdict', '')}**",
        f"- 综合分：**{content.get('score', 0):.3f}**",
        f"- 根因：**{content.get('rootCause', '')}**", "",
        "## 评测维度", "", "| 维度 | 得分 | 结论 | 原因 |", "|---|---:|---|---|",
    ]
    for item in dims:
        markdown.append(f"| {item.get('dimension')} | {item.get('score')} | {item.get('verdict')} | {str(item.get('reason', '')).replace('|', '/')} |")
    if content.get("baselineRunId"):
        markdown.extend([
            "", "## 回流前后对比", "",
            f"- 自进化草稿：`{content.get('draftId', '')}`",
            f"- 基线 Run：`{content.get('baselineRunId')}`",
            f"- 回流前：**{content.get('beforeScore')}**",
            f"- 回流后：**{content.get('afterScore')}**",
            f"- Lift：**{content.get('lift')}**",
        ])
    models = content.get("models") or {}
    markdown.extend([
        "", "## 可追溯信息", "",
        f"- 原始反馈理由：{(content.get('feedback') or {}).get('observedReason', '') or '无'}",
        f"- 回答模型：`{models.get('answer', '')}`",
        f"- Judge：`{models.get('judge', '')}`",
        f"- Judge 置信等级：`{models.get('confidence', '')}`",
        f"- Trace：{', '.join(content.get('traceIds') or []) or '无'}",
    ])
    markdown.extend(["", "## 优化建议", "", content.get("recommendation", "无")])
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")
    html_path.write_text(
        "<!doctype html><meta charset='utf-8'><title>LLM User Report</title>"
        "<style>body{font-family:sans-serif;max-width:960px;margin:40px auto;line-height:1.6}pre{white-space:pre-wrap}</style>"
        f"<pre>{html.escape(markdown_path.read_text(encoding='utf-8'))}</pre>",
        encoding="utf-8",
    )
    return (
        upload(json_path, f"{run_id}/report.json"),
        upload(markdown_path, f"{run_id}/report.md"),
        upload(html_path, f"{run_id}/report.html"),
    )
