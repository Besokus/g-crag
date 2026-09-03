"""Agent 工具集：把现有 service 包装成 Tool，返回 LLM 可读摘要。

从 diagnose_agent_service 迁移而来。后续 persona（S2/S3）只需在此新增 Tool 并注册。
"""
from app.config import settings
from app.services import domain_service, kg_service, retrieval_service, ticket_lifecycle_service
from app.services.agent_runtime import Tool, ToolRegistry

_TOPK = 5


# ---------- 工具实现（包装现有 service，返回 LLM 可读摘要）----------
async def _t_search_regulation(db, model_type, query, tenant=None):
    """检索运维规程/手册。"""
    kwargs = {"model_type": model_type}
    if tenant:
        kwargs["tenant"] = tenant
    ctx = await retrieval_service.mixed_search(db, query, _TOPK, **kwargs)
    return _fmt_chunks(ctx) or "未检索到相关规程"


async def _t_query_equipment_graph(db, model_type, entity, tenant=None):
    """查设备-故障-处置因果链（Neo4j 图谱）。"""
    if tenant:
        rows = await kg_service.graph_context(entity, 8, db=db, tenant=tenant)
    else:
        rows = await kg_service.graph_context(entity, 8)
    return "\n".join(rows) if rows else "图谱中无该设备相关因果链"


async def _t_search_similar_case(db, model_type, symptom, tenant=None):
    """查历史相似故障案例。"""
    kwargs = {"tenant": tenant} if tenant else {}
    res = await domain_service.similar_case(db, symptom, model_type, _TOPK, **kwargs)
    return _fmt_cases(res.get("cases", [])) or "未找到相似历史案例"


async def _t_draft_ticket(db, model_type, task, tenant=None):
    """生成处置操作票草案。"""
    kwargs = {"tenant": tenant} if tenant else {}
    res = await domain_service.generate_ticket(db, task, model_type, _TOPK, **kwargs)
    return _fmt_ticket(res.get("ticket", {})) or "生成操作票草案失败"


async def _t_create_ticket(db, model_type, task, device="", ticketType="操作票",
                           steps=None, safety=None, risks=None,
                           sourceRef="", tenant=None, creator=""):
    """把处置方案落库成两票草稿（source_ref 幂等，重复创建返回既有票）。"""
    task = (task or "").strip()
    if not task:
        return "创建失败：task 不能为空"
    kwargs = {"tenant": tenant} if tenant else {}
    t = await ticket_lifecycle_service.create_ticket(
        db, ticket_type=ticketType or "操作票", task=task, device=device or "",
        steps=steps or [], safety=safety or [], risks=risks or [],
        source_ref=sourceRef or None, creator=creator, **kwargs)
    return (f"已创建工单 {t['id']}（草稿，状态:{t['status']}），标题:{t['title']}。"
            f"可调用 submit_ticket 提交审核")


async def _t_submit_ticket(db, model_type, ticketId, tenant=None):
    """提交工单进审核（自动跑审核引擎，高分自动初审通过）。"""
    kwargs = {"tenant": tenant} if tenant else {}
    try:
        t = await ticket_lifecycle_service.submit_for_review(db, ticketId, **kwargs)
    except ValueError as e:
        return f"提交失败：{e}"
    return (f"工单 {t['id']} 已提交审核，状态:{t['status']}，"
            f"审核得分:{t.get('reviewScore', 0)}")


# ---------- 摘要格式化 ----------
def _fmt_chunks(ctx):
    if not ctx:
        return ""
    return "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('chunk') or '')[:200]}"
                     for i, c in enumerate(ctx[:_TOPK], 1))


def _fmt_cases(cases):
    if not cases:
        return ""
    return "\n".join(f"[{i}] {(c.get('docName') or '')}: {(c.get('text') or '')[:200]}"
                     for i, c in enumerate(cases[:_TOPK], 1))


def _fmt_ticket(ticket):
    if not ticket:
        return ""
    steps = ticket.get("steps") or []
    return (f"设备:{ticket.get('device') or '无'}\n"
            f"步骤:{';'.join(steps[:8]) if steps else '无'}\n"
            f"安措:{';'.join(ticket.get('safety') or []) or '无'}\n"
            f"风险:{';'.join(ticket.get('risks') or []) or '无'}")


# ---------- schema ----------
_SCHEMA_QUERY = {"type": "object",
                 "properties": {"query": {"type": "string", "description": "检索关键词，如 '主变压器油温高 处置'"}},
                 "required": ["query"]}
_SCHEMA_ENTITY = {"type": "object",
                  "properties": {"entity": {"type": "string", "description": "设备名，如 '1号主变'"}},
                  "required": ["entity"]}
_SCHEMA_SYMPTOM = {"type": "object",
                   "properties": {"symptom": {"type": "string", "description": "故障症状描述"}},
                   "required": ["symptom"]}
_SCHEMA_TASK = {"type": "object",
                "properties": {"task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"}},
                "required": ["task"]}
_SCHEMA_CREATE_TICKET = {"type": "object",
                         "properties": {
                             "task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"},
                             "device": {"type": "string", "description": "设备名"},
                             "ticketType": {"type": "string", "enum": ["操作票", "工作票"], "description": "票据类型，默认操作票"},
                             "steps": {"type": "array", "items": {"type": "string"}, "description": "操作步骤列表"},
                             "safety": {"type": "array", "items": {"type": "string"}, "description": "安全措施列表"},
                             "risks": {"type": "array", "items": {"type": "string"}, "description": "风险/危险点列表"},
                             "sourceRef": {"type": "string", "description": "来源关联（如 qa:会话id），幂等键"}},
                         "required": ["task"]}
_SCHEMA_SUBMIT_TICKET = {"type": "object",
                         "properties": {"ticketId": {"type": "string", "description": "工单 id"}},
                         "required": ["ticketId"]}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("search_regulation",
                      "检索电网运维规程/手册/标准，获取故障处置的规程依据、限值、标准步骤。",
                      _SCHEMA_QUERY, _t_search_regulation, required_roles=[]))
    reg.register(Tool("query_equipment_graph",
                      "查知识图谱中设备的故障-处置因果链（设备→故障→处置 多跳）。",
                      _SCHEMA_ENTITY, _t_query_equipment_graph, required_roles=[]))
    reg.register(Tool("search_similar_case",
                      "查历史相似故障案例（故障案例库），看历史上类似故障怎么处理的。",
                      _SCHEMA_SYMPTOM, _t_search_similar_case, required_roles=[]))
    reg.register(Tool("draft_ticket",
                      "生成处置操作票草案（步骤/安措/风险）。诊断基本明确、需要处置步骤时调用。",
                      _SCHEMA_TASK, _t_draft_ticket, required_roles=["admin"]))
    if settings.TICKET_ACTION_LOOP_ENABLE:
        reg.register(Tool("create_ticket",
                          "把已确定的处置方案落库成两票草稿。仅当用户明确要求生成/创建工单时调用。",
                          _SCHEMA_CREATE_TICKET, _t_create_ticket,
                          required_roles=["admin", "editor"]))
        reg.register(Tool("submit_ticket",
                          "把工单提交审核（自动跑审核引擎）。create_ticket 成功后按用户要求调用。",
                          _SCHEMA_SUBMIT_TICKET, _t_submit_ticket,
                          required_roles=["admin", "editor"]))
    return reg


DEFAULT_REGISTRY = build_default_registry()


# ===== N2 MCP 工具动态注册 =====

async def register_mcp_tools(registry: ToolRegistry = None) -> int:
    """从 MCP registry 发现外部工具 → schema 转换 → 注册进 ToolRegistry。

    在 main.py lifespan 中 MCP registry 加载后调用。
    Returns: 注册的工具数量
    """
    reg = registry or DEFAULT_REGISTRY
    try:
        from app.mcp.registry import mcp_registry
        from app.mcp.client import mcp_client

        servers = mcp_registry.list_enabled()
        if not servers:
            return 0

        discovered = await mcp_client.discover(servers)
        count = mcp_client.register_tools(reg, discovered)

        # 更新 registry 中的 tools 列表
        for item in discovered:
            mcp_registry.update_tools(item["server"], item.get("tools", []))

        if count:
            print(f"[mcp] 已注册 {count} 个外部 MCP 工具")
        return count
    except Exception as e:
        from app.core.obs import degraded
        degraded("mcp_register_tools", e)
        return 0
