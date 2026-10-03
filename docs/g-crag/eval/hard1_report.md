# G-CRAG 评测报告：hard1

- 题目：split=all，模式：none, gcrag, gcrag_path, gcrag_path_all
- 阈值：{'high': 0.3, 'low': 0.18, 'source': 'high=calibrated,low=calibrated'}；打分器：dashscope_rerank@gte-rerank-v2（replay）
- 生成模型：claude@claude-sonnet-4-6；判定模型：claude:claude-sonnet-4-6；提交：6cead3e

## 准确率（可答题）

| 模式 | 单文档 | 双文档 | 三文档 | 合计 |
|---|---|---|---|---|
| none | — | 6.5%（2/31） | — | 6.5%（2/31） |
| gcrag | — | 22.6%（7/31） | — | 22.6%（7/31） |
| gcrag_path | — | 12.9%（4/31） | — | 12.9%（4/31） |
| gcrag_path_all | — | 25.8%（8/31） | — | 25.8%（8/31） |

## 拒答

| 模式 | 不可答题拒答率 | 可答题误拒率 |
|---|---|---|
| none | — | 77.4%（24/31） |
| gcrag | — | 61.3%（19/31） |
| gcrag_path | — | 71.0%（22/31） |
| gcrag_path_all | — | 64.5%（20/31） |

## 挽回率与代价

| 模式 | 图谱定位证据挽回 | LLM 改写挽回 | 平均纠错轮次 | 延迟 P50 / P95（ms） | 降级记录 |
|---|---|---|---|---|---|
| none | — | — | 0 | 8719.6 / 30054.2 | 0 |
| gcrag | 6.2%（1/16） | 0.0%（0/1） | 0.548 | 7675.8 / 28911.9 | 0 |
| gcrag_path | 0.0%（0/16） | — | 0.516 | 9103.9 / 23431.2 | 0 |
| gcrag_path_all | 0.0%（0/16） | — | 1 | 8450.4 / 33227.5 | 0 |

## 纠错动作分布

- none：{'normal': 31}
- gcrag：{'normal': 15, 'graph_partial': 2, 'ambiguous_kept': 12, 'refused': 1, 'graph_recovered': 1}
- gcrag_path：{'normal': 15, 'graph_partial': 4, 'graph_supplemented': 9, 'ambiguous_kept': 3}
- gcrag_path_all：{'normal': 7, 'graph_partial': 4, 'graph_supplemented': 17, 'ambiguous_kept': 3}
