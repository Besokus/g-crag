# Superpowers 三线调研索引

> 日期：2026-07-26  
> 范围：数据飞轮/知识自进化、主动运维/两票闭环、检索质量/可信答案  
> 方法：沿用 `.superpowers/sdd` 的 brief/report 思路，所有结论必须有文档、代码或测试支撑；无支撑项标记为待补调研。

## 调研结论

| 线 | 结论 | 支撑文档 |
|---|---|---|
| A | 数据飞轮已有事件总线、证据缺口、自进化和治理传播基础，但默认多为 opt-in，下一步应先做端到端可观测闭环。 | `docs/superpowers/research/2026-07-26-track-a-data-flywheel.md` |
| B | 主动运维到两票已有事件接入、只读 Agent、人工确认、转两票和任务中心，下一步应补建议质量指标与转票后状态回写。 | `docs/superpowers/research/2026-07-26-track-b-active-ops-ticket.md` |
| C | 检索可信链路已有 rewrite、混合检索、CRAG、引用校验和 debug trace，下一步应把反馈/评测和 trace 串联，并补直接测试。 | `docs/superpowers/research/2026-07-26-track-c-retrieval-trust.md` |

## 分步骤执行

| Step | 标题 | 目标 | 文档支撑 |
|---|---|---|---|
| 1 | 质量事件闭环打底 | dislike/低分/治理事件统一入库、可查、可派发 | `2026-07-26-track-a-data-flywheel.md` |
| 2 | 证据缺口到自进化回流 | evidence_gap 聚类、草稿、审核、回流、撤回可追踪 | `2026-07-26-track-a-data-flywheel.md` |
| 3 | 主动运维建议质量评分 | 事件触发建议后给出证据完整性、可执行性、风险说明评分 | `2026-07-26-track-b-active-ops-ticket.md` |
| 4 | 主动运维转两票状态回写 | run 转票后追踪票据审核、签发、执行、归档状态 | `2026-07-26-track-b-active-ops-ticket.md` |
| 5 | 问答 trace 与反馈关联 | 问答结果、用户反馈、检索 debug、在线评测可通过 traceId 关联 | `2026-07-26-track-c-retrieval-trust.md` |
| 6 | 可信答案生产配置矩阵 | 明确生产推荐开关、灰度顺序和回滚点 | `2026-07-26-track-c-retrieval-trust.md` |
| 7 | 支撑矩阵验收 | 每个任务绑定文档、代码、测试、指标 | `docs/superpowers/research/2026-07-26-support-matrix.md` |

## 推荐批次

1. Batch A：质量事件查询接口 + Admin 事件视图 + dislike payload 增强。
2. Batch B：evidence_gap 端到端测试 + 自进化回流链路验收。
3. Batch C：主动运维建议质量评分 + 两票状态回写。
4. Batch D：traceId 串联问答、反馈、debug trace、online_eval。
5. Batch E：治理传播 dry-run 后开启真实清理。

## 边界

- 本轮只形成调研和方案文档，不改业务代码。
- 破坏性清理能力必须先 dry-run，再启用真实清理。
- 主动运维继续保持只读，转两票只生成草稿。
- AI 自进化内容继续降权或可撤回，避免生成内容污染主知识库。
