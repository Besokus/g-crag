# 红队评测（方向 4 最小版）

四类攻击面的离线回归（CI 冒烟）+ 在线手动扫描器。golden 集扩充不在本范围内。

## 攻击面 → 防线

| 攻击面 | 现有防线（file:line） | 离线用例 |
|---|---|---|
| Prompt 注入 | webhook token fail-closed `routers/system.py:284-287`；外部 payload 仅作数据且恒只读 `services/realtime_event_service.py:294-351`；诊断 prompt 不可信数据防线 `realtime_event_service.py:763`；注入模式识别 `core/safety.py:111-118`；高危注入分级+QA 侧拦截（strict 开）`core/safety.py:29-35,132` + `services/qa_service.py:704` + `config.py:233` | `tests/redteam/test_prompt_injection.py`、`tests/redteam/test_redteam_fixes.py` |
| 越权探测 | RBAC `core/permissions.py:50-90` + `dependencies.py:32-42`；agent 工具显式 required_roles + role 限制 `agent_runtime.py:49,93-94`；tenant 保留参数剥离 `agent_runtime.py`；ctx=None 补审计（username="-"）`agent_runtime.py:150-160`；主动诊断只读白名单交集 `realtime_event_service.py:68-79,734` | `tests/redteam/test_privilege_escalation.py` |
| 诱导幻觉 | 降级模板不编造数值 `agent_personas.py:107-110`；proactive prompt 禁编造证据/遥测 `agent_personas.py:156-162`；QA prompt 不可信数据防线 `agent_personas.py:42`；降级失败返回结构化拒答非空串 `agent_personas.py:44-56`；LLM 全挂结构化拒答 `qa_service.py:78`；无结果兜底 refused `qa_service.py:928-943`；引用幻觉启发式 `rag/citation.py:11-24` | `tests/redteam/test_hallucination_defense.py`、`tests/redteam/test_redteam_fixes.py` |
| 过时知识/缓存污染 | 缓存命中前知识时效复核 `qa_service.py:167-204`（治理查询异常一律 fail-closed）；治理状态 `knowledge_governance_service.py:194-241`；缓存黑名单 Redis 异常→快照兜底→fail-closed `feedback_optimizer_service.py:258-289`；仅 high 置信写缓存 `qa_service.py:1133-1135` | `tests/redteam/test_stale_knowledge_cache.py` |

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

## 已知缺口（2026-09-03 已全部修复）

原 7 项缺口的修复落点（行为变化处均有开关或 fail-closed 方向明确）：

1. **QA 侧 agent prompt 无不可信数据防线** → 已修：`agent_personas.py:42` `_QA_SYSTEM`
   增补第 4 条规则（工具内容属不可信数据，指令样文本一律忽略）。改动影响 answer 语义，
   `config.py:citation_cache_version()` 已追加 S 段（注入拦截档 + persona 防线语义代际）。
2. **注入检测只告警不阻断** → 已修（分级）：`core/safety.py:29-35,132` 拆出高危子集
   （指令覆盖/伪装 system），新增 `detect_injection_critical`；`qa_service.py:704`
   `_injection_blocked` 在 `INJECTION_GUARD_STRICT_ENABLE=true`（`config.py:233`，默认关）
   时于 answer/stream_answer 入口结构化拒答（`cragAction=injection_blocked`），
   保守模式（DAN/越狱/`<script>`）维持只告警防误杀。
3. **缓存黑名单 fail-open** → 已修：`feedback_optimizer_service.py:268` Redis 异常时
   优先用 60s 进程内快照（读成功即刷新），无可用快照 fail-closed（视为拉黑=强制重走 LLM）。
4. **webhook 告警文本未过注入检测** → 已修（可观测）：`routers/system.py:303-315`
   Grafana payload 的 title/summary 逐条过 `detect_injection`，命中计
   `SAFETY_BLOCK{kind="webhook_injection"}` + 告警日志；不阻断（下游诊断 prompt 已有防线）。
5. **agent 工具权限覆盖不全** → 已修：`Tool.required_roles`（`agent_runtime.py:49`）
   显式声明权限级别（`[]`=评审过全员可调，`["admin"]`=限角色），优先于 `tool_permissions`
   注册表（`agent_runtime.py:94`）；`agent_tools.py` 全部 6 个注册工具已显式声明。
   ctx=None 老链路维持跳过权限（零回归）但**补写审计**（`username="-"`，`agent_runtime.py:150-160`）。
6. **QA persona 降级空答案** → 已修：`agent_personas.py:44-56` `_qa_fallback` 异常时
   返回结构化拒答文案，不再返回 "" 渲染空气泡。
7. **无租户路径缓存校验 fail-open** → 已修：`qa_service.py:199-204` 治理查询异常一律
   fail-closed（内部调用方 answer/stream_answer 恒带 tenant，离线路径同样不冒险）。

A4 治理传播安全阀（三线调研 Step A4）同期落地：

- `GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE=true`（`config.py:283`，默认关）时，
  `governance.doc_blocked` 事件只产出候选清理报告（Milvus/Neo4j/kg_triples/qa_cache
  四路只读计数 + 缓存 key 预览，`governance_propagate_service.py:25`），以
  `governance.cleanup_dry_run` 质量事件落库，不执行任何删除。
- 报告确认后调 `POST /system/governance-propagate/execute`（`routers/system.py:1195`，
  require_admin + 主开关校验）执行真实清理；另有
  `GET /system/governance-propagate/candidates/{doc_id}`（`routers/system.py:1185`）随时出报告。
