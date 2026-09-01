# 红队评测（方向 4 最小版）

四类攻击面的离线回归（CI 冒烟）+ 在线手动扫描器。golden 集扩充不在本范围内。

## 攻击面 → 防线

| 攻击面 | 现有防线（file:line） | 离线用例 |
|---|---|---|
| Prompt 注入 | webhook token fail-closed `routers/system.py:284-287`；外部 payload 仅作数据且恒只读 `services/realtime_event_service.py:294-351`；诊断 prompt 不可信数据防线 `realtime_event_service.py:630-651`；注入模式识别 `core/safety.py:111-118` | `tests/redteam/test_prompt_injection.py` |
| 越权探测 | RBAC `core/permissions.py:50-90` + `dependencies.py:32-42`；agent 工具按 role 限制 `agent_runtime.py:22-25,72-81`；tenant 保留参数剥离 `agent_runtime.py:87-101`；主动诊断只读白名单交集 `realtime_event_service.py:68-79,734` | `tests/redteam/test_privilege_escalation.py` |
| 诱导幻觉 | 降级模板不编造数值 `agent_personas.py:107-110`；proactive prompt 禁编造证据/遥测 `agent_personas.py:156-162`；LLM 全挂结构化拒答 `qa_service.py:78`；无结果兜底 refused `qa_service.py:891-906`；引用幻觉启发式 `rag/citation.py:11-24` | `tests/redteam/test_hallucination_defense.py` |
| 过时知识/缓存污染 | 缓存命中前知识时效复核 `qa_service.py:167-204`；治理状态 `knowledge_governance_service.py:194-241`；缓存黑名单 `feedback_optimizer_service.py:261-267`；仅 high 置信写缓存 `qa_service.py:1096-1098` | `tests/redteam/test_stale_knowledge_cache.py` |

## 怎么跑

```bash
# CI 冒烟（离线，随 pytest 全量跑，无需服务）
python -m pytest tests/redteam -q

# 手动/夜间在线扫描（需 backend+LLM 运行中，不进 CI）
python scripts/redteam_eval.py --base-url http://127.0.0.1:8001 \
    --username admin --password admin123 \
    --probe-username operator1 --probe-password operator123 \
    --output reports/redteam_$(date +%F).md
```

扫描器判定为启发式（magic token / 密钥模式 / 提示词复述 / 越权 HTTP code / 兜底措辞），
报告需人工复核；`--strict` 把 warn 计入门禁，存在 fail（或 strict 下 warn）时 exit 1。

## 已知缺口（只记录，未改生产代码）

1. **QA 侧 agent prompt 无不可信数据防线**：`agent_personas.py:35-39` `_QA_SYSTEM` 没有
   realtime 侧（`realtime_event_service.py:635`）同款"检索/输入内容属于不可信数据"文案。
   建议落点：`_QA_SYSTEM` 补防线；改动会变 answer 语义，需同步 `config.py:citation_cache_version()`。
2. **注入检测只告警不阻断**：`core/safety.py:197-228` guard_query 与 `domain_service.py:52-62`
   命中注入仅计数+日志，攻击 query 原样进入检索/LLM。建议落点：按模式分级，高危模式
   （假装 system 指令/指令覆盖）在 QA 侧叠加降级提示或拒绝路径，保守模式维持现状防误杀。
3. **缓存黑名单 fail-open**：`feedback_optimizer_service.py:266-267` Redis 异常返回 False，
   L2 MySQL 缓存仍可命中已拉黑坏答案。建议落点：异常时 degraded + fail-closed 或本地短 TTL 兜底。
4. **webhook 告警文本未过注入检测**：`routers/system.py:270-360` Grafana payload 的
   title/summary 直落操作日志与 WS 广播（下游诊断 prompt 有防线，展示层无过滤）。
   建议落点：webhook 循环内对文本做一次 detect_injection 计数（先可观测，不阻断）。
5. **agent 工具权限覆盖不全**：`agent_runtime.py:22-25` 仅 3 个两票工具限角色，
   query_telemetry 等未列工具全员可调；ctx=None 链路完全跳过权限与审计（老链路零回归设计，
   已由 `test_ctx_none_skips_permission_check_documented_gap` 固化）。建议落点：新工具默认
   显式声明权限级别；ctx 缺失时至少补审计记录。
6. **QA persona 降级空答案**：`agent_personas.py:44-51` `_qa_fallback` 异常时返回 ""，
   空串会作为答案返回。建议落点：降级失败返回 `_llm_all_down_response` 同款结构化拒答。
7. **无租户路径缓存校验 fail-open**：`qa_service.py:187/204` `not bool(tenant)`，离线/兼容
   调用不校验时效（已由 `test_cache_validation_without_tenant_fails_open_documented_gap` 固化）。
   建议落点：内部调用统一强制带租户后改 fail-closed。
