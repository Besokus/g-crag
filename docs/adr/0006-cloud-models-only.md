# 只使用云端模型，打分器用云端 rerank 并做录制回放

开发与演示环境没有本地模型（无 GPU、不部署本地推理），所有 LLM、embedding、rerank 都走云端 API。因此删除 grid-qa 中的本地 bge 稠密检索路（`grid_chunks_bge`）和 Ollama 兜底，改为多个云端 LLM provider 之间互相兜底。检索分级的打分器使用云端 gte-rerank，封装在打分器接口之后；评测时录制（问题, 候选文本）→ 分数并在重跑时回放，以保证评测可复现、成本可控，并且不受欠费或限流影响。

## Consequences

- Provider 划分：LLM 走一个通用的 OpenAI 兼容 provider（`base_url + api_key + model`，可配多条渠道按顺序兜底），取代原 deepseek/qwen/doubao/ollama 四个专用实现，具体渠道后续适配；embedding（text-embedding-v3，1024 维）、rerank（gte-rerank-v2）、OCR（qwen-vl-ocr，取代本地 rapidocr）统一使用阿里百炼。

- 必须修复原有纠错的分级信号缺陷：rerank 实际是否成功要随检索结果返回（原实现只写入轨迹，分级时读取的是配置开关 `RERANK_ENABLE`）；开启 G-CRAG 时不跳过 rerank；rerank 失败时如实降级为"不纠错、只标注不确定"。
- 分级阈值（HIGH/LOW）在信号修复后用 golden 集重新标定，不沿用现有的 0.72/0.3（`CRAG_HIGH`/`CRAG_LOW`）。
- 实体向量召回改为在建图时调用云端 embedding 生成向量，查询时再调用一次，向量保存在内存中。
- 演示依赖云端可用性：界面显示 rerank/LLM 健康状态，演示前预热缓存，并准备录屏备份。
