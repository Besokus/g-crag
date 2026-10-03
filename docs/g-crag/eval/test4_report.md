# G-CRAG 评测报告：test4

- 题目：split=test，模式：none, legacy, gcrag, gcrag_v2, gcrag_path, gcrag_path_all, closed_book
- 阈值：{'high': 0.3, 'low': 0.18, 'source': 'high=calibrated,low=calibrated'}；打分器：dashscope_rerank@gte-rerank-v2（replay）
- 生成模型：claude@claude-sonnet-4-6；判定模型：claude:claude-sonnet-4-6；提交：2daea52

## 准确率（可答题）

| 模式 | 单文档 | 双文档 | 三文档 | 合计 |
|---|---|---|---|---|
| none | 95.0%（19/20） | 80.0%（16/20） | 85.0%（17/20） | 86.7%（52/60） |
| legacy | 95.0%（19/20） | 75.0%（15/20） | 80.0%（16/20） | 83.3%（50/60） |
| gcrag | 95.0%（19/20） | 75.0%（15/20） | 80.0%（16/20） | 83.3%（50/60） |
| gcrag_v2 | 95.0%（19/20） | 75.0%（15/20） | 75.0%（15/20） | 81.7%（49/60） |
| gcrag_path | 95.0%（19/20） | 70.0%（14/20） | 80.0%（16/20） | 81.7%（49/60） |
| gcrag_path_all | 95.0%（19/20） | 65.0%（13/20） | 80.0%（16/20） | 80.0%（48/60） |
| closed_book | 15.0%（3/20） | 5.0%（1/20） | 15.0%（3/20） | 11.7%（7/60） |

## 拒答

| 模式 | 不可答题拒答率 | 可答题误拒率 |
|---|---|---|
| none | 100.0%（10/10） | 0.0%（0/60） |
| legacy | 100.0%（10/10） | 5.0%（3/60） |
| gcrag | 100.0%（10/10） | 3.3%（2/60） |
| gcrag_v2 | 100.0%（10/10） | 3.3%（2/60） |
| gcrag_path | 100.0%（10/10） | 5.0%（3/60） |
| gcrag_path_all | 100.0%（10/10） | 3.3%（2/60） |
| closed_book | 70.0%（7/10） | 51.7%（31/60） |

## 挽回率与代价

| 模式 | 图谱定位证据挽回 | LLM 改写挽回 | 平均纠错轮次 | 延迟 P50 / P95（ms） | 降级记录 |
|---|---|---|---|---|---|
| none | — | — | 0 | 11449.2 / 17697.7 | 0 |
| legacy | — | 0.0%（0/3） | 0.157 | 11317.5 / 16793.7 | 0 |
| gcrag | 0.0%（0/5） | 0.0%（0/3） | 0.4 | 10446.9 / 15026.7 | 0 |
| gcrag_v2 | 0.0%（0/5） | 0.0%（0/3） | 0.4 | 10069.9 / 15855.6 | 0 |
| gcrag_path | 0.0%（0/5） | 0.0%（0/2） | 0.329 | 11551.3 / 35776.8 | 0 |
| gcrag_path_all | 0.0%（0/5） | 0.0%（0/2） | 1.086 | 13109.7 / 51113.9 | 0 |
| closed_book | — | — | 0 | 7654.6 / 18024.2 | 0 |

## 纠错动作分布

- none：{'normal': 70}
- legacy：{'normal': 53, 'refused': 10, 'ambiguous_kept': 6, 'rewrite_partial': 1}
- gcrag：{'normal': 53, 'ambiguous_kept': 6, 'refused': 10, 'rewrite_partial': 1}
- gcrag_v2：{'normal': 53, 'ambiguous_kept': 6, 'refused': 10, 'rewrite_partial': 1}
- gcrag_path：{'normal': 53, 'refused': 6, 'graph_supplemented': 3, 'graph_partial': 5, 'ambiguous_kept': 3}
- gcrag_path_all：{'normal': 32, 'graph_supplemented': 24, 'refused': 6, 'graph_partial': 5, 'ambiguous_kept': 3}
- closed_book：{'closed_book': 70}
