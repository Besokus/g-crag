# 裁剪到单一主链路，删除 Agent/MCP 与大部分数据飞轮

fork 后只保留服务于"文档 → 图谱 → 检索 → 纠错 → 生成 → 评测/轨迹"的模块。删除业务功能、内部 Agent 与 MCP、平台杂项，以及依赖真实用户流量的知识治理/数据飞轮模块（knowledge_governance、knowledge_evolution、governance_propagate、doc_quality、feedback_optimizer、fix_rate、doc_signoff、doc_annotation）。原因：这些都与 G-CRAG 创新点无关；飞轮模块在没有用户流量的课程环境中无法演示或评测；删减后系统更小，演示更稳定，答辩追问范围也更可控。原版 grid-qa 不做改动，复盘这些功能时以原版为准。

## Consequences

- 保留 evidence_gap、knowledge_backbone（evidence_gap 依赖它）、feedback_service，用于"拒答 → 证据补全 → 再答对"闭环和积累评测集。
- 不提供 MCP 入口，使用方式只有 Web 问答界面和 REST API。
- qa_service 中对已删模块的按需 import 分支（如 `:115` 反馈黑名单、`:193` 知识治理、`:396` 辩论诊断）需同步剪除。
