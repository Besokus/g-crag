# 只使用云端模型，打分器用云端 rerank 并做录制回放

开发与演示环境没有本地模型（无 GPU、不部署本地推理），所有 LLM、embedding、rerank 都走云端 API。因此删除 grid-qa 中的本地 bge 稠密检索路（`grid_chunks_bge`）和 Ollama 兜底，改为多个云端 LLM provider 之间互相兜底。检索分级的打分器使用云端 gte-rerank，封装在打分器接口之后；评测时录制（问题, 候选文本）→ 分数并在重跑时回放，以保证评测可复现、成本可控，并且不受欠费或限流影响。

## Consequences

- Provider 划分：LLM 走一个通用的 OpenAI 兼容 provider（`base_url + api_key + model`，可配多条渠道按顺序兜底），取代原 deepseek/qwen/doubao/ollama 四个专用实现，具体渠道后续适配；embedding（text-embedding-v3，1024 维）、rerank（gte-rerank-v2）、OCR（qwen-vl-ocr，取代本地 rapidocr）统一使用阿里百炼。

- 必须修复原有纠错的分级信号缺陷：rerank 实际是否成功要随检索结果返回（原实现只写入轨迹，分级时读取的是配置开关 `RERANK_ENABLE`）；开启 G-CRAG 时不跳过 rerank；rerank 失败时如实降级为"不纠错、只标注不确定"。
- 分级阈值（HIGH/LOW）在信号修复后用 golden 集重新标定，不沿用现有的 0.72/0.3（`CRAG_HIGH`/`CRAG_LOW`）。
- 实体向量召回改为在建图时调用云端 embedding 生成向量，查询时再调用一次，向量保存在内存中。
- 演示依赖云端可用性：界面显示 rerank/LLM 健康状态，演示前预热缓存，并准备录屏备份。

## 修订（2026-09-29）：LLM 改用 Claude

LLM 渠道改为一个 Claude 代理（Anthropic Messages 协议，模型 claude-opus-4-8），不是 OpenAI 兼容协议，因此没有做"通用 OpenAI 兼容 provider"，而是新增专用的 `ClaudeLLM`（`providers/llm/claude_llm.py`），在 provider 边界把 OpenAI 格式的消息、工具定义与工具调用历史转换为 Anthropic 格式，业务代码不变。embedding、rerank、OCR 仍走阿里百炼。

- 兜底链只配 `claude` 一项：若代理失败时静默切到 qwen，评测结果会混入两个模型的输出，无法复现。代理不可用时如实报错。
- 该模型拒绝 `temperature` 参数，由 `CLAUDE_SEND_TEMPERATURE`（默认 false）控制；"低温度保证抽取稳定"的做法对它不再成立。
- 答案缓存键原先在未指定模型时一律记作 `default`，换模型后仍会命中旧模型的答案；已改为 `provider@模型名`（见《设计文档》§17）。
- 换模型改变了基线表现：不可答题 tu01 在 qwen 下编造答案，在 Claude 下正确拒答。对照实验必须在同一模型下进行，报告中注明所用模型。
