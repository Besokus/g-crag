# 基于 fork grid-qa 改造，而非从零自建

课程项目 G-CRAG 以 fork `zhyese/grid-qa`（CC BY-NC 4.0）为底座改造成通用 GraphRAG，而不是按原计划自建精简版。原因：一次改造同时服务三个目标——课程项目、复盘简历中的 grid-qa（文档解析/OCR/检索全链路）、沉淀一个通用 GraphRAG；且 grid-qa 已有成熟的混合检索、Neo4j 图谱、CRAG 与轨迹可视化，可省数周。

## Consequences

- 许可证义务：衍生作品须署名原作者 zhyese、附许可证链接、注明修改，并以 CC BY-NC 发布，禁止商用。
- 报告与答辩须单列"既有平台 vs 本项目新增贡献"，创新点仅主张"图谱驱动纠错"（原 CRAG 纠错动作不感知图谱，见 grid-qa `qa_service.py:256-288`），不主张"CRAG+GraphRAG 结合"本身。
- 任课老师已确认允许基于开源项目做增量开发（2026-09-27）。
- 原按自建方案写的《开发方案文档》《设计文档》v1.0（Streamlit/LangGraph）已作废，v2.0 按 grid-qa 实际栈（FastAPI + Vue3 + Milvus + Neo4j）重写，见 `docs/g-crag/`。
