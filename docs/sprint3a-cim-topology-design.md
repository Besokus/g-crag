# Sprint 3a · CIM 标准设备拓扑数字孪生 — 设计稿（SDD）

> 状态：**设计完成，实现延后**。当前数字孪生 MVP 功能完整（demo 布局 + KG 故障链），CIM 升级是"锦上添花"非"补断点"。建议作为独立 sprint，待拿到真实 CIM/CIMD 数据再启动。

## 1. 现状（已全栈建成，非从零）

| 层 | 实现 | 文件 |
|---|---|---|
| 后端 | `twin_service`：站点布局/总览/设备详情/故障传播链/告警推送 | `backend/app/services/twin_service.py` |
| 路由 | 6 端点：layout/overview/device-detail/fault-chain/alert-push/ws | `backend/app/routers/twin.py` |
| 前端 | Three.js 3D 站点孪生 + 设备点击 + 故障链高亮 + 告警闪烁 | `frontend/src/views/DigitalTwin.vue` |
| 布局数据 | **手写 JSON 模板**（`TWIN_LAYOUT_PATH`，单个 `110kV-demo` 站） | `backend/data/...` |

**关键观察**：故障传播链 `get_fault_chain` **已图驱动**（`kg_service.get_paths` → Neo4j 多跳，按 `kgEntity`），不是硬编码。**唯一"demo 化"的是布局坐标与设备列表**——来自手写 JSON，非标准拓扑模型。

## 2. CIM 升级的真实断点

把"手写 demo 布局"升级为"从 **CIM（IEC 61970/61968）** 导入电气拓扑"：

- **现状**：positions/connections/devices 全手写，换站要手改 JSON；连通性是 `connections` 字段人工标注。
- **目标**：CIM RDF/CIMD 导入 → `ConductingEquipment`（断路器/线路/变压器/母线）+ `ConnectivityNode`/`Terminal` → **拓扑处理**（按开关开合合并节点 → 带电岛/供电区）→ 从图派生布局、连通、故障传播路径。

收益：任何站点只需丢 CIM 文件即可生成孪生；故障传播基于真实电气连通（开关状态感知）而非 KG 实体模糊匹配。

## 3. 方案草图

```
CIM RDF/CIMD ──parse──▶ 设备+节点图(内存/Neo4j 子图)
                              │
                              ▼
                     拓扑处理器(开关态合并→带电岛)
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        twin_service       故障传播          布局自动
        (读图替代JSON)     (按连通+带电)     (力导/规则)
```

- **CIM 解析**：`rdflib` 解析 CIMXML，按 mRID 抽 `Breaker`/`TransmissionLine`/`PowerTransformer`/`BusbarSection` 等。
- **图存储**：复用 Neo4j（kg_service 同库，独立 label 如 `CimEquipment`/`CimNode`），三元组镜像 MySQL（仿 `KgTriple` 范式）。
- **拓扑处理**：经典 CIM topology processor——闭合开关合并 ConnectivityNode，断开开关断开，输出带电岛。
- **twin_service 改造**：`_load_layout(station_id)` 从 JSON → 改读 CIM 图；`get_fault_chain` 从 `kg_entity` 模糊匹配 → 改按 CIM 连通图 BFS（开关态感知）。

## 4. 工作量与建议

- **规模**：大型（CIM 解析器 + 拓扑处理器 + 图 schema + twin_service 重构 + 前端布局适配）。估 1-2 周。
- **前置依赖**：**真实 CIM/CIMD 数据**（电网客户提供的 IEC 61970 导出文件）。无数据则只能用合成 CIM，价值打折。
- **建议**：
  1. 当前 MVP 已满足 demo/监管展示需求，**不阻塞**。
  2. 列入独立 sprint，**拿到真实 CIM 数据再启动**。
  3. 可先做最小切片：仅 CIM 解析 + 图存储（不动 twin_service），让 KG 多一类"电气连通"实体，故障链自然受益（增量、低风险）。

## 5. 与 2026 趋势对齐

- **CIM 数字孪生**：电网行业 2026 标准化数字孪生基线，CIM 是事实数据模型。
- **LazyGraphRAG**（2026 微软）：图优先检索——CIM 连通图可作 GraphRAG 的高质量结构层，与本项目 `KG_RAG_ENABLE` 复用。
- **Agentic Corrective RAG**：故障传播链基于真实拓扑后，CRAG 的"证据充分性"判断更准（连通可达 = 强证据）。
