# G-CRAG：基于知识图谱与纠错机制的检索增强智能问答系统

G-CRAG（Graph-Corrective RAG）是一个通用的知识图谱问答系统：上传文档后自动建图，三元组带可回溯的出处段落；问答时先给检索结果分级，证据不足时由**知识图谱定位证据**纠错，纠错用尽仍不足就**诚实拒答**，并把问题送进人工证据补全。

本项目由 [zhyese/grid-qa](https://github.com/zhyese/grid-qa)（电网运维 RAG 问答系统）改造而来，保留了原项目的完整提交历史，以 CC BY-NC 4.0 发布。改了什么、新增了什么见文末[与 grid-qa 的关系](#与-grid-qa-的关系)和 [NOTICE](NOTICE)。

---

## 目录

- [核心思路](#核心思路)
- [纠错流程](#纠错流程)
- [功能页面](#功能页面)
- [评测结论](#评测结论)
- [快速开始](#快速开始)
- [复现评测](#复现评测)
- [目录结构](#目录结构)
- [测试](#测试)
- [文档索引](#文档索引)
- [局限](#局限)
- [与 grid-qa 的关系](#与-grid-qa-的关系)

---

## 核心思路

RAG 系统最危险的不是答不出，而是**在证据不足时自信地答错**。G-CRAG 围绕"可信"做了四件事：

| 问题 | 做法 |
|---|---|
| 检索结果可不可信，系统自己不知道 | 用 rerank 最高分把检索分为 correct / ambiguous / incorrect；打分器调用失败时记为 **unscored**，不拿假分数做判断（修复了原系统 rerank 失败时仍按"已打分"分级的缺陷） |
| 多跳问题的第二跳证据和问题语义不相近，向量检索召回不到 | **图谱定位证据**：从问题实体出发沿图谱走 1～2 跳，取回路径上每条关系的出处段落补进上下文，再统一重打分 |
| 图谱关系来自 LLM 抽取，可能是编的 | 每条三元组记录**出处段落**并做校验：主体和客体都在原文段落中出现才算 verified，找不到的标为"无出处" |
| 纠错用尽仍没有证据 | **结构化拒答**，问题自动进入证据补全队列；管理员 AI 起草 + 人工确认后入库，图谱同步更新 |

---

## 纠错流程

```
检索 → 检索分级
 ├─ 打分失败 → unscored：不纠错，标注不确定 → 生成
 ├─ correct  → 生成
 ├─ ambiguous → [第 1 轮] 图谱定位证据 → 合并上下文 → 重打分 → 再分级 → 生成
 └─ incorrect → [第 1 轮] 图谱定位证据 → 合并 → 重打分 → 再分级
                 ├─ 可作答 → 生成
                 └─ 仍 incorrect → [第 2 轮] LLM 改写问题重检索 → 再分级
                                     ├─ 可作答 → 生成
                                     └─ 仍 incorrect → 拒答 → 证据补全队列
```

纠错模式可按请求切换（`/api/qa/answer` 的 `correctionMode` 参数），用于对照实验：

| 模式 | 说明 |
|---|---|
| `none` | 不纠错 |
| `legacy` | 原有纠错：incorrect 时 LLM 改写重检索，仍不行则拒答 |
| `gcrag` | 图谱定位证据优先，LLM 改写兜底（默认） |
| `gcrag_v2` | 同 gcrag，但图谱证据只追加、不挤掉原检索条目 |
| `gcrag_path` | 图谱候选按"问题 + 证据路径"重打分后追加 |
| `gcrag_path_all` | 同 gcrag_path，且初始分级为 correct 时也补充一次图谱证据 |

`gcrag_v2`、`gcrag_path`、`gcrag_path_all` 是看过前几轮评测结果之后做的迭代，评测报告中已如实标注。设计细节见[设计文档](docs/g-crag/设计文档.md) §5～§8。

---

## 功能页面

| 页面 | 用途 |
|---|---|
| 智能问答 | 流式问答，显示置信度、引用来源、链路耗时、相关追问；拒答时给出原因 |
| **纠错对照** | 同一问题并排跑三种纠错模式，展示分级变化、纠错轮次、来源（检索 / 图谱）、证据路径图和链路耗时；管理员可在页面上注入 rerank 故障（超时 / 报错），现场演示 unscored |
| 知识库 | 上传 → 解析 → 向量化；向量化后后台自动抽取三元组建图（单篇新闻约 15 秒） |
| 知识图谱 | 关系图谱、多跳关联、枢纽实体；三元组管理表可展开**出处原文**（主体 / 客体高亮）并显示校验状态 |
| 系统管理 → 证据补全 | 拒答和证据有限的问题自动收集；AI 起草、人工编辑、确认后入库（结论为"无法确认"的草稿不允许入库） |
| 检索调试 / 链路诊断 | 单次检索的全链路 trace、各环节耗时 |

---

## 评测结论

**数据**：[CRUD-RAG](https://github.com/IAAR-Shanghai/CRUD_RAG) 中文新闻问答，抽 70 道测试题（单 / 双 / 三文档各 20 道可答题，另有 10 道删掉全部标准文档的不可答题），加上干扰新闻共约 1000 篇入库。生成与判定都用 claude-sonnet-4-6，rerank 结果录制回放，判定结果经人工抽查 50 条，一致率 96%。

**主测试集**（test4，[完整报告](docs/g-crag/eval/test4_report.md)）：

| 模式 | 可答题准确率 | 不可答题拒答率 | 可答题误拒率 |
|---|---|---|---|
| 闭卷（不检索） | 11.7% | 70% | 51.7% |
| none | **86.7%** | 100% | 0% |
| legacy | 83.3% | 100% | 5.0% |
| gcrag | 83.3% | 100% | 3.3% |
| gcrag_path_all | 80.0% | 100% | 3.3% |

在这个测试集上，**图谱纠错没有带来净收益**：70 道题里 53 道首轮就被判为 correct，纠错只作用于剩下的 17 道；各模式之间 1～3 题的差异，与"上下文完全相同时判定翻转约 4%"的生成随机性同量级。原因是这些题的证据和问题语义相近，普通检索已经能召回。

**隐藏桥接子集**（hard1，31 道，[完整报告](docs/g-crag/eval/hard1_report.md)）：专门构造"第二跳文档不提问题里的实体、问题也不提第二跳文档主题"的多跳题，并经人工抽检。

| 模式 | 准确率 |
|---|---|
| none | 6.5%（2/31） |
| gcrag | 22.6%（7/31） |
| gcrag_path_all | **25.8%（8/31）** |

三种图谱模式答对的每一道题，最终上下文里都有第二跳文档，而且都是图谱定位证据补进来的。

**结论**：G-CRAG 的效果是**有条件的**——条件就是"检索召回不到桥接文档"。在语义可达的问题上图谱证据是冗余的；在语义不可达的多跳问题上，图谱定位证据能把准确率从 6.5% 提到 22.6%～25.8%。隐藏桥接子集是针对性构造的，只用于诊断，不和主测试集指标混算。另外，所有检索模式在不可答题上都 100% 拒答，没有编造。

逐题分析、阈值标定、消融实验见[验收记录](docs/g-crag/验收记录.md)。

---

## 快速开始

### 前置

- Docker Desktop（Compose v2）
- 一个 LLM 渠道：Claude（Anthropic 协议），也支持 DeepSeek / 通义千问 / 豆包等 OpenAI 兼容接口
- 阿里百炼 API Key：用于 embedding 与 rerank（gte-rerank-v2）

### 启动

```bash
cp .env.example .env
# 编辑 .env，至少填写：
#   LLM_PROVIDER=claude
#   CLAUDE_API_KEY / CLAUDE_BASE_URL / CLAUDE_PROTOCOL
#   DASHSCOPE_API_KEY
#   ADMIN_PASSWORD（默认 admin123，请修改）
# 本机已有其他 Neo4j 占用 7474/7687 时，设置 NEO4J_HTTP_PORT / NEO4J_BOLT_PORT

docker compose -p gcrag-general up -d
```

| 地址 | 说明 |
|---|---|
| http://localhost:5173 | 前端（管理员账号见 `.env` 的 `ADMIN_USERNAME` / `ADMIN_PASSWORD`） |
| http://localhost:8001/docs | 后端接口文档 |
| http://localhost:8001/health | 健康检查（含 rerank 打分器状态） |

`.env` 只保存在本地，已被 `.gitignore` 忽略，不要提交。

### 领域包

领域知识（实体类型、关系白名单、抽取与回答提示词、别名表）放在 `backend/domain_packs/<名称>/`，部署时用 `DOMAIN_PACK` 选择，启动时加载并校验：

- `general`：通用新闻领域（默认，评测用）
- `grid`：原电网运维领域（仅演示）

不同领域包建议用不同的 compose project（`-p gcrag-<包名>`）运行，数据卷天然隔离。

### 重建知识图谱

```bash
docker exec grid-backend python /app/scripts/rebuild_kg.py            # 新建或断点续跑
docker exec grid-backend python /app/scripts/rebuild_kg.py --status   # 查看进度
```

---

## 复现评测

CRUD-RAG 原始数据放在 `data/crud_rag/`（不随仓库分发），compose 将其只读挂载到容器内。

```bash
E="docker exec grid-backend python /app/scripts/gcrag_eval.py"
$E build                                    # 抽样、构造不可答题、写出语料
$E import                                   # 语料入库（上传 → 解析 → 向量化）
docker exec grid-backend python /app/scripts/rebuild_kg.py
$E run --split test --modes none,legacy,gcrag,closed_book --run-id myrun
$E judge --run-id myrun                     # 规则 + Claude 判定
$E report --run-id myrun                    # 生成 report.md 与 metrics.json
```

隐藏桥接子集由 `scripts/gcrag_multihop.py` 构造（`hard` → `review` 人工抽检 → `hardset`）。

---

## 目录结构

```
backend/
  app/rag/
    scorer.py            打分器接口（rerank 成功 / 超时 / 无 key / 跳过）与录制回放
    grading.py           检索分级（含 unscored）
    gcrag.py             纠错编排（六种纠错模式）
    entity_linking.py    实体链接（词典最长匹配 + 实体向量召回）
    graph_evidence.py    图谱定位证据（遍历 → 出处段落 → 候选）
  app/services/
    kg_service.py        三元组抽取、出处校验、写 MySQL + Neo4j
    qa_service.py        问答主链路（缓存、检索、纠错、生成、拒答）
    evidence_gap_service.py  证据补全（收集、AI 起草、人工确认、入库）
  domain_packs/          领域包（general、grid）
frontend/src/views/
  GcragCompare.vue       纠错对照页
  KgGraph.vue            知识图谱与出处
scripts/
  gcrag_eval.py          评测：build / import / run / judge / calibrate / report
  gcrag_multihop.py      隐藏桥接多跳子集构造
  rebuild_kg.py          全量重建图谱（断点续跑）
  test_in_container.sh   在容器内跑测试
docs/
  adr/                   架构决策记录
  g-crag/                设计文档、开发方案、验收记录、评测报告、调研
```

---

## 测试

测试在后端容器里运行，连接隔离的测试库（MySQL `grid_qa_test`、Redis 15 号库），不碰在线数据：

```bash
scripts/test_in_container.sh                                  # 全量
scripts/test_in_container.sh tests/test_kg_provenance.py -q   # 单个文件
```

不要直接用容器默认环境跑测试：部分集成测试会写入证据缺口，在线环境的证据补全会把它们当成真实问题入库。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [CONTEXT.md](CONTEXT.md) | 术语表（检索分级、图谱定位证据、出处 chunk、挽回率……） |
| [docs/adr/](docs/adr/) | 架构决策记录：fork 底座、领域包、裁剪范围、出处 chunk、只用云端模型、CRUD-RAG 主评测、点赞续期缓存 |
| [设计文档](docs/g-crag/设计文档.md) | 架构、纠错流程、实体链接、图谱遍历、评测框架、接口契约 |
| [开发方案文档](docs/g-crag/开发方案文档.md) | 定位、范围、里程碑 |
| [验收记录](docs/g-crag/验收记录.md) | 各里程碑的实测数据与逐题分析 |
| [评测报告](docs/g-crag/eval/) | test2～test4、hard1 |
| [调研](docs/g-crag/research/) | GraphRAG 隐藏桥接多跳与图谱路由的业内方案 |

---

## 局限

- 图谱纠错只在"多跳且语义不可达"的问题上有效；在普通问题上没有收益，还会增加少量延迟。
- 实体链接依赖词典和实体向量，问题里的实体链接不上时无法使用图谱证据。
- 同名不同实体（如两个不同的"县政协"）会被合并为一个节点，这是实体消歧的真实局限。
- 分级完全依赖 rerank 分数，打分器的偏差会传导到纠错决策；阈值与打分器绑定，换打分器需重新标定。
- 评测规模小（70 + 31 题），模式间 1～3 题的差异不足以排序。

---

## 与 grid-qa 的关系

本项目基于 [zhyese/grid-qa](https://github.com/zhyese/grid-qa) 做增量开发，原项目的检索（BM25 + 向量 + rerank）、缓存、权限、文档管理、前端框架等大部分基础设施沿用。G-CRAG 的工作集中在：

| 类别 | 内容 |
|---|---|
| 新增 | 图谱定位证据纠错与六种纠错模式；实体链接；三元组出处段落与出处校验；领域包机制；打分器接口、录制回放与运行时故障注入；纠错对照页；CRUD-RAG 评测框架与隐藏桥接多跳子集 |
| 修复 | 分级信号缺陷（rerank 失败时仍按已打分分级）；证据补全回流的缓存键与问答读取不一致；非答案可被确认入库；点赞写入永久缓存层导致拒答被高置信复用 |
| 改造 | 由电网专用改为通用领域；LLM 统一走 Claude，阿里百炼只用于 embedding / rerank |
| 裁剪 | 电网业务功能、Agent 与 MCP、知识治理等与主链路无关的模块（先断开，代码暂留） |

完整修改说明见 [NOTICE](NOTICE)。

## 许可证

[CC BY-NC 4.0](LICENSE)，禁止商业使用。原作品署名见 NOTICE 与 git 提交历史。
