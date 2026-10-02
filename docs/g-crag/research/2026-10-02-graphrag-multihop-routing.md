# 调研：语义不相近的多跳检索、图谱路由与评测集构造（G-CRAG）

- 日期：2026-10-02
- 范围：回答 G-CRAG 课程项目的 5 个问题：多跳方案、何时走图谱、落地经验、评测集构造、改进建议
- 来源原则：只引用一手来源（arXiv 原文、官方博客/文档、官方仓库）。所有数字都注明论文、表号、基准和底座模型；原文里找不到的写"未核实"。论文报告的数字和工程经验分开写，工程经验标【经验】
- 本项目实测数据来自 `docs/g-crag/eval/test3_report.md` 和任务说明，不是外部来源

---

## TL;DR

1. **明确针对"桥接实体不在问题里、问题与证据语义不相近"的方案只有几类**：HippoRAG / HippoRAG 2（以问题实体为种子跑个性化 PageRank，一步完成多跳）、IRCoT / Self-Ask / 查询分解（先拿到第一跳答案，再用它检索第二跳）、HopRAG（用伪问题在段落之间建"逻辑边"）、KAG（逻辑形式规划）。LightRAG 和 Microsoft GraphRAG 的 global / DRIFT 面向"全局概括类"问题，不是为隐藏桥接设计的。
2. **增益有条件**：图谱方法在 2WikiMultiHopQA 上收益最大（HippoRAG R@5 68.2→89.1，HippoRAG 论文 Table 2，对比 ColBERTv2），在 MuSiQue 上中等（49.2→51.9），在 HotpotQA 上持平或略低（79.3→77.7）。换成强 dense 检索器（NV-Embed-v2）后，HippoRAG 2 在 HotpotQA 上只多 0.2 F1（HippoRAG 2 论文 Table 2）。
3. **多项独立比较发现图谱经常不如普通 RAG**：对单跳和细节类问题，普通 RAG 更好；只输出三元组的 KG-RAG 明显更差（例如 HotpotQA F1 25.0 对 60.0，*RAG vs. GraphRAG* Table 1，Llama-3.1-8B）；保留原文 chunk 是关键（*In-depth Analysis…* §7.2）。GraphRAG-Bench 也观察到，图谱在简单事实题上会引入冗余噪声（Obs.1、Obs.4）。
4. **G-CRAG 的 0 次分级提升有结构性原因**：触发条件（rerank top1）和采纳条件（用原问题重新 rerank 后分级要提升）衡量的都是"与问题的语义相似度"。隐藏桥接场景里的第二跳文档恰恰与问题不相似，所以这个判据在图谱本该起作用的场景里天然不成立。这是推断，依据是 HippoRAG 改用图上 PPR 打分而不是查询相似度来排序，HopRAG 把这类文档定义为"间接相关"。
5. **路由的一手证据**：Adaptive-RAG 的复杂度分类器准确率只有 54.5%（Table 4），但用理想分类器（oracle）时 F1 可从 50.9 升到 62.8（Table 1，GPT-3.5），说明瓶颈在路由信号质量。*RAG vs. GraphRAG* 发现"两路都跑再合并"比"二选一路由"效果好（MultiHop-RAG 上 +6.4% 对 +1.1%，Llama-3.1-70B），代价是成本翻倍。厂商文档（Neo4j ToolsRetriever、LlamaIndex Router）都采用 LLM 选工具，但都没有给出评测数字。
6. **落地案例里，图谱带来正收益时都依赖显式结构**：LinkedIn 用工单字段和工单间的显式链接建图，MRR 0.522→0.927，解决时长中位数 −28.6%；蚂蚁 KAG 依赖领域 schema 和专家规则；Writer、Lettria/AWS 的大幅提升是厂商自测。微软 GraphRAG 官方说明写明，vector RAG 擅长"答案与问题相似"的局部问题。
7. **评测集要按 MuSiQue 的思路"按构造去捷径"**：用冻结的生产检索器筛选"doc2 不在 top5"的题，同时加闭卷过滤、只给 doc1 不可答过滤、题面泄漏检查，并公开整个过滤漏斗。筛的是题目而不是检索器，这样可以避免削弱检索的嫌疑。
8. **截止前建议按投入/收益排序**：① 构造检索困难的隐藏桥接评测集（1～1.5 天）；② 修正触发和采纳判据的盲区，改成路径感知重打分加生成器自报缺口（0.5～1 天）；③ 加一个 Self-Ask 式分解检索作为非图谱强基线（0.5 天）。HippoRAG 2 全量重建、GraphRAG/LightRAG/KAG/ToG、训练路由分类器都放进"局限与展望"。

---

## 1. 针对"语义不相近的多跳"的方案

### 1.1 问题界定

HippoRAG 把多跳问题分成两类（HippoRAG 论文 Table 7、§5.2）：

- **path-following**：沿一条链走，例如"Vila de Xira 出生地的地区是哪里"。HippoRAG 指出，即使 Alhandra 没出现在 Vila de Xira 的段落里，图搜索也能借助"出生地"这条关联找到目标。
- **path-finding**：要在很多条路径里找出连接若干实体的那一条，例如"做阿尔茨海默症研究的斯坦福教授"。

本项目关心的是前一类里最难的情况：第二跳文档 doc2 不提问题实体 A，问题里也没有桥接实体 B，所以 doc2 与问题在语义上不相近。HopRAG 把这类文档称为 *indirectly relevant*，意思是与问题不相似，但可以从相似文档跳过去（HopRAG §1、Figure 1b）。

### 1.2 各方案逐项说明

**HippoRAG（Gutiérrez et al., arXiv 2405.14831）**
- 机制：用 LLM 做 OpenIE 抽取三元组，建无 schema 的 KG。用检索编码器在实体之间加同义边（余弦相似度 τ=0.8）。在线检索时，先从问题里抽命名实体并链接到 KG 节点作为种子，再跑个性化 PageRank（PPR，阻尼 0.5），按节点得分聚合出段落排序；节点权重做类似 IDF 的特异性加权（§3、§3.4）。
- 是否针对隐藏桥接：**是**。PPR 会把概率分布到种子节点的邻域，**不依赖 doc2 与问题的相似度**，可以一步完成多跳（Table 7 示例、§5.2）。
- 增益（Table 2，单步检索，对比 ColBERTv2，OpenIE 用 GPT-3.5-turbo-1106）：

  | 指标 | MuSiQue | 2Wiki | HotpotQA |
  |---|---|---|---|
  | R@2 ColBERTv2 → HippoRAG | 37.9 → 40.9 | 59.2 → 70.7 | 64.7 → 60.5 |
  | R@5 ColBERTv2 → HippoRAG | 49.2 → 51.9 | 68.2 → 89.1 | 79.3 → 77.7 |

  QA 结果（Table 4，reader 为 GPT-3.5-turbo，top-5 段落加 1-shot CoT）：MuSiQue EM/F1 从 15.5/26.4 升到 19.2/29.8，2Wiki 从 33.4/43.3 升到 46.6/59.5，HotpotQA 从 43.4/57.7 **降到** 41.8/55.0。论文把 HotpotQA 上的弱势归因于两点：该数据集对知识整合的要求低，以及"概念与上下文"之间的取舍（§4、Appendix B）。
- 成本：查询时每 1000 个 query 花费约 $0.1、3 分钟；IRCoT 为 $1～3、20～40 分钟（Table 17）。论文据此称 HippoRAG 比 IRCoT 便宜 10～30 倍、快 6～13 倍。索引比 IRCoT 慢约 10 倍，每 1 万段落多花约 $15（Table 18，GPT-3.5）。

**HippoRAG 2（Gutiérrez et al., arXiv 2502.14802）**
- 机制：在 HippoRAG 的基础上做三处改动。① 加入**段落节点**，用 "contains" 边把段落连到它抽出的短语，形成**实体–段落二部图**（§3.2）。② 用整个问题去匹配三元组（query-to-triple），取代只匹配实体（§3.3）。③ 用 LLM 过滤三元组，称为"recognition memory"（§3.4）。所有段落节点也作为 PPR 种子，按与问题的 embedding 相似度加权（Appendix G.1）。如果过滤后没有三元组，就**直接退回 dense 检索**（§3.5），这本身就是一个内建的按需触发。
- 是否针对隐藏桥接：**是**，同时还设计成"不损害简单题"，摘要里称之为修复了以往结构化 RAG 在事实类任务上的退化。
- 增益（Table 3，passage R@5，对比 NV-Embed-v2 7B；建图和过滤用 Llama-3.3-70B-Instruct）：MuSiQue 69.7→74.7，2Wiki 76.5→90.4，HotpotQA 94.5→96.3。QA F1（Table 2，reader 为 Llama-3.3-70B-Instruct）：MuSiQue 45.7→48.6，2Wiki 61.5→71.0，HotpotQA 75.3→75.5，NQ 61.9→63.3。
- 同一张 Table 2 里，复现的 GraphRAG 在 MuSiQue 上只有 38.5 F1，**低于** NV-Embed-v2 的 45.7。复现的 LightRAG 只有 1.6（数值异常低，应当作复现失败，不能当成 LightRAG 的真实上限）。
- 成本（Table 12，MuSiQue 语料 11,656 段落，Llama-3.3-70B）：索引输入/输出 token 为 9.2M/3.0M，GraphRAG 为 115.5M/36.1M，LightRAG 为 68.5M/18.3M。每题 QA 耗时 1.2 s，NV-Embed-v2 为 0.3 s，GraphRAG 为 10.7 s，LightRAG 为 13.3 s。

**LightRAG（Guo et al., arXiv 2410.05779）**
- 机制：用 LLM 抽取实体和关系，并生成键值描述。查询时由 LLM 同时抽出 low-level 关键词（用于匹配实体）和 high-level 关键词（用于匹配关系），再收集被命中节点或边的**一跳邻居**（§3.2）。
- 是否针对隐藏桥接：**否**。论文的定位是"具体问题与抽象问题"，评测用的是 UltraDomain 语料上的 LLM 两两胜率（Table 1，GPT-4o-mini 当评委），**原文没有 MuSiQue、2Wiki、HotpotQA 的数字**。
- 第三方数字：KG2RAG Table 1（LLaMA3-8B）中，LightRAG 在 HotpotQA distractor 设置下 F1 为 0.293，Semantic RAG 为 0.617。GraphRAG-Bench Table 2（GPT-4o-mini）中，LightRAG 在 Novel 数据集的复杂推理题上 ACC 为 49.07，带 rerank 的 RAG 为 42.93。可见 LightRAG 的表现非常依赖题型。
- 成本：论文 §4.5 称检索阶段只用不到 100 token 生成关键词、调用 1 次 API，而 GraphRAG 的 global 检索要遍历 610 个社区报告，约 61 万 token。但 GraphRAG-Bench 实测 LightRAG 平均 prompt 约 10 万 token（Figure 9），两者口径不同。

**Microsoft GraphRAG（Edge et al., arXiv 2404.16130；官方文档与博客）**
- 机制：LLM 抽取实体和关系，用 Leiden 算法做层次社区划分，为每个社区生成报告。查询有三种：
  - **local**：先把问题与实体描述做 embedding 匹配，再扩展到相连的实体、关系、社区报告和原文 text unit（[Local Search 文档](https://microsoft.github.io/graphrag/query/local_search/)）。
  - **global**：对社区报告做 map-reduce。
  - **DRIFT**：先用社区报告做 HyDE 式 primer，生成后续问题，再用 local search 细化，默认迭代 2 轮（[DRIFT 博客](https://www.microsoft.com/en-us/research/blog/introducing-drift-search-combining-global-and-local-search-methods-to-improve-quality-and-efficiency/)）。
- 是否针对隐藏桥接：**否**。论文明确的目标是"全局 sensemaking"问题，并写道 vector RAG"works well for queries that can be answered with information localized within a small set of records"（§1）。
- 增益：论文在约 1M token 的播客语料和约 1.7M token 的新闻语料上，用 GPT-4 当评委做两两比较。对比 vector RAG（SS），global 方法的 comprehensiveness 胜率为 72～83%（播客）和 72～80%（新闻），diversity 胜率为 75～82% 和 62～71%；但 **directness 上 vector RAG 最好**（§5、Figure 2）。DRIFT 对 local search 的 comprehensiveness 胜率为 78%，diversity 为 81%（5,000+ 篇 AP 新闻，50 题，博客）。**以上都没有 MuSiQue、2Wiki、HotpotQA 的数字**。第三方数字：HippoRAG 2 Table 2 中 GraphRAG 在 MuSiQue 上 F1 38.5，NV-Embed-v2 为 45.7；HopRAG Table 2（GPT-4o）中 GraphRAG 在 MuSiQue 上 EM 12.10，BGE 为 20.80。
- 成本：官方 README 警告"GraphRAG indexing can be an expensive operation… start small"（[README](https://github.com/microsoft/graphrag)）。LazyGraphRAG 博客称其索引成本"与 vector RAG 相同，是完整 GraphRAG 的 0.1%"，查询成本是 GraphRAG global search 的 4%（[LazyGraphRAG 博客](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)）。

**KAG（蚂蚁集团，arXiv 2409.13731）**
- 机制：包括 LLMFriSPG 知识表示、KG 与原文 chunk 的**互索引**（mutual-indexing）、逻辑形式引导的混合推理（把问题规划成检索、图推理、数学运算等步骤），以及基于同义和上下位关系的知识对齐（摘要、§2）。
- 是否针对隐藏桥接：**是**。摘要把动机写成"the gap between vector similarity and the relevance of knowledge reasoning"。
- 增益（Table 8，端到端 EM/F1）：对比 IRCoT+HippoRAG（底座 DeepSeek-V2），KAG（LFSH_ref3，底座 DeepSeek-V2）在 HotpotQA 上从 51.0/63.7 升到 62.5/76.2，2Wiki 从 48.0/57.1 升到 67.8/76.2，MuSiQue 从 26.2/36.5 升到 36.7/48.7。注意：表中 NativeRAG 基线用的是 ChatGPT-3.5，**与 KAG 的底座不同**，只有对比 IRCoT+HippoRAG（DeepSeek-V2）这一行是同底座。检索（Table 9，多步）：KAG 的 R@5 在 HotpotQA、2Wiki、MuSiQue 上为 88.8、91.9、65.7，NativeRAG（数值与 HippoRAG 论文中的 ColBERTv2 相同）为 79.3、68.2、49.2。
- 成本：论文没有给出索引和查询的 token 或费用。§3.3 提到 LFS_ref3 比 LFSH_ref3 节省 12～22% 计算时间。**成本未核实**。

**Think-on-Graph（ToG，arXiv 2307.07697）/ ToG-2（arXiv 2407.10805）**
- 机制：ToG 让 LLM 当 agent，在 KG（Freebase、Wikidata）上做 beam search，每一步由 LLM 剪枝关系和实体。ToG-2 用 KG 通过实体链接文档，再把文档当实体上下文做紧耦合的迭代检索（ToG-2 摘要）。
- 是否针对隐藏桥接：部分是，靠逐跳显式推理来补桥接。但 ToG 依赖现成的大型 KG，原论文评测的是 KBQA（CWQ、WebQSP 等），不是从语料自建的图。
- 增益：ToG Table 1 中，CWQ 上 ChatGPT CoT 为 38.8，ToG（ChatGPT）为 57.1，ToG（GPT-4）为 67.6（KBQA，没有 dense RAG 基线）。ToG-2 Table 1（GPT-3.5-turbo）中，AdvHotpotQA EM：Vanilla RAG 23.7，ToG 26.3，ToG-2 42.9。**没有 MuSiQue、2Wiki 的数字**。
- 成本：ToG 平均每题调用 LLM 7.6～14.3 次，CWQ 上为 14.3（ToG Table 7/8）。ToG-2 平均每题 5.4 次、27.3 s，NaiveRAG 为 1 次、10.2 s（ToG-2 Table 9，HotpotQA）。

**IRCoT（Trivedi et al., arXiv 2212.10509）**
- 机制：思维链（CoT）与检索交替进行，每生成一句 CoT 就用它作为查询再检索（Figure 2）。
- 是否针对隐藏桥接：**是**。摘要写道"what to retrieve depends on what has already been derived"。
- 增益（Figure 3/4，基线是单步 BM25 检索 OneR，**不是 dense**；CoT 生成用 GPT3 code-davinci-002）：召回在 HotpotQA、2Wiki、MuSiQue 上分别 +11.3、+22.6、+12.5，QA F1 分别 +7.1、+13.2、+7.1。HippoRAG Table 3 中，IRCoT+ColBERTv2 的 R@5 在 MuSiQue、2Wiki、HotpotQA 上为 53.7、74.4、82.0，单步 ColBERTv2 为 49.2、68.2、79.3。
- 成本：每题要多轮 LLM 加检索。HippoRAG 设置里最多 2～4 轮，每轮取 10 段（HippoRAG Appendix H）；1000 题 API 费用 $1～3（GPT-3.5，HippoRAG Table 17）。

**Self-Ask（Press et al., arXiv 2210.03350）与查询分解**
- 机制：模型显式写出"Follow up:"子问题，可以把子问题交给搜索引擎，拿到答案后再提下一个子问题（§3.3、Figure 3）。
- 是否针对隐藏桥接：**是**。第一跳答出 B 之后，第二个子问题就会显式包含 B，相似度检索因此能命中 doc2。
- 增益（Table 1，davinci-002，EM）：2Wiki 上 CoT 29.8，Self-ask 30.0，Self-ask+Search 40.1；MuSiQue 上分别为 12.6、13.8、15.2。基线是 prompting 和 Google 搜索，**不是 dense RAG**。HopRAG Table 1（GPT-3.5-turbo，top-20）给出了 dense 加分解的数字：BGE 对比 BGE+Query Decomposition，MuSiQue EM 11.8→21.5，2Wiki 27.9→43.9，HotpotQA 38.4→43.6。
- 成本：每个子问题多 1 次 LLM 调用和 1 次检索。

**HopRAG（arXiv 2502.12442，补充）**
- 机制：索引时让 LLM 为每个段落生成"出向伪问题"（本段引出但答不了的问题）和"入向伪问题"（本段能回答的问题），按关键词 Jaccard 加 embedding 相似度把它们配对，在段落之间建有向边。检索时从相似段落出发，由 LLM 选边做多跳 BFS，最后剪枝（§3）。
- 是否针对隐藏桥接：**是，而且是最直接的**。摘要写道"traditional retrievers focus on lexical or semantic similarity rather than logical relevance"。
- 增益（Table 1，GPT-3.5-turbo，top-20）：MuSiQue EM/F1 从 BGE 的 11.8/18.6 升到 39.1/53.0；HippoRAG 为 32.6/43.8。但在 2Wiki 上 HippoRAG（66.4/74.0）**优于** HopRAG（61.6/68.9）。
- 成本：检索时每题调用 LLM 约 20～40 次（Table 4，n_hop 为 1～4）；索引时每个段落要生成伪问题。

**实体–chunk 二部图扩展（KG2RAG、Bedrock GraphRAG、HippoRAG 2 段落节点）**
- 机制：先做语义检索得到种子 chunk，再沿 KG 扩展 m 跳实体，取回这些实体的出处 chunk 并组织成上下文。KG2RAG 先扩展 1 跳（m=1），再用 cross-encoder 按三元组表示对最小生成树排序（KG2RAG §2、§3）。Bedrock 文档的描述是"After performing an initial vector search… retrieves related graph nodes or chunk identifiers that are linked to the retrieved document chunks. Expands on these related chunks by traversing the graph"（[Bedrock 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html)）。**G-CRAG 的"图谱定位证据"属于这一类**。
- 是否针对隐藏桥接：部分是，前提是种子 chunk 里有 A，扩展才能到 B 再到 doc2。
- 增益（KG2RAG Table 1/7，LLaMA3-8B，mxbai-embed-large，bge-reranker-large）：HotpotQA distractor 设置下 response F1 从 Semantic+Rerank 的 0.652 升到 0.663；fullwiki 设置下从 0.587 升到 0.631；MuSiQue 从 0.380 升到 0.419。**增益温和**。
- 成本：建图时每个 chunk 平均 561 个输入 token、22 个输出 token、1 次调用；LightRAG 为 1,269/381、1 次，GraphRAG 为 2,791/629、5 次（Table 9）。检索 25 ms，Semantic RAG 为 21 ms（Table 10）。

### 1.3 汇总对比表

| 方案 | 核心机制 | 是否明确针对隐藏桥接 | 相对普通 dense RAG 的多跳增益（表号 / 底座） | 索引成本 | 查询成本 |
|---|---|---|---|---|---|
| HippoRAG | OpenIE KG + 同义边 + PPR | 是 | R@5 vs ColBERTv2：MuSiQue +2.7，2Wiki +20.9，HotpotQA −1.6（T2，GPT-3.5 OpenIE） | 比 IRCoT 慢约 10 倍，每 1 万段约 +$15（T18） | 1 次 NER 调用；每千题约 $0.1（T17） |
| HippoRAG 2 | + 段落节点二部图 + query-to-triple + LLM 过滤 | 是，且兼顾简单题 | F1 vs NV-Embed-v2：MuSiQue +2.9，2Wiki +9.5，HotpotQA +0.2（T2，Llama-3.3-70B） | 9.2M 输入 / 3.0M 输出 token（11.6k 段，T12） | 每题 1.2 s，dense 为 0.3 s（T12） |
| LightRAG | 双层关键词 → 实体 / 关系 + 1 跳邻居 | 否 | 原文无；第三方结果多数低于 dense（KG2RAG T1） | 68.5M 输入 token（HippoRAG 2 T12） | 每题 13.3 s（同上）；prompt 约 10 万 token（GraphRAG-Bench F9） |
| MS GraphRAG local / global / DRIFT | 社区层次 + 报告；local 走实体扩展 | 否（面向全局概括） | 原文无；MuSiQue F1 38.5 < NV-Embed 45.7（HippoRAG 2 T2） | 115.5M 输入 token（同上） | 每题 10.7 s；global prompt 达 10^5 量级（GraphRAG-Bench F8） |
| KAG | 互索引 + 逻辑形式规划 + 知识对齐 | 是 | vs IRCoT+HippoRAG（同为 DeepSeek-V2）：F1 +12.5 / +19.1 / +12.2（T8，HotpotQA / 2Wiki / MuSiQue） | 未披露 | 多步，未披露 |
| ToG / ToG-2 | LLM 在 KG 上 beam search / KG 与文档迭代检索 | 部分是 | AdvHotpotQA EM 23.7→42.9（ToG-2 T1，GPT-3.5） | 依赖现成 KG | ToG 每题 7.6～14.3 次调用；ToG-2 5.4 次 |
| IRCoT | CoT 与检索交替 | 是 | 对 BM25 单步：召回 +11～23（Fig.3，code-davinci-002） | 无 | 2～4 轮 LLM + 检索 |
| Self-Ask / 查询分解 | 显式子问题 | 是 | BGE→BGE+分解：MuSiQue EM +9.7（HopRAG T1，GPT-3.5） | 无 | 每个子问题 +1 次 LLM 和 +1 次检索 |
| HopRAG | 伪问题逻辑边 + LLM 选边 | 是（最直接） | MuSiQue EM 11.8→39.1（T1，GPT-3.5） | 每段生成伪问题 | 每题 20～40 次 LLM（T4） |
| 实体–chunk 扩展（KG2RAG 等） | 种子 chunk → KG m 跳 → 出处 chunk | 部分是 | HotpotQA-Full F1 0.587→0.631（T1，LLaMA3-8B） | 每 chunk 1 次抽取调用 | 接近 dense |

> 注：上表数字分别来自不同论文的不同设置，**不能跨行横向比较**。同一行的"增益"只与该论文自己的基线相比。

---

## 2. 何时走图谱：自适应路由的落地做法

### 2.1 学术方案

| 方案 | 路由信号 | 一手结论 |
|---|---|---|
| Adaptive-RAG（Jeong et al., arXiv 2403.14403） | 用 T5 小模型做三分类：A 不检索 / B 单步 / C 多步（IRCoT）。训练标签来自"哪种策略先答对"，没有标签的题按数据集出身打 B 或 C（§3.2） | Table 1（GPT-3.5）：Adaptive-RAG F1 50.91、相对耗时 1.46；多步 F1 50.87、耗时 3.33；**理想分类器 F1 62.80**。分类器准确率只有 54.52%（Table 4，FLAN-T5-XL 设置），C 被误判成 B 的约 31%（§5 混淆矩阵） |
| CRAG（Yan et al., arXiv 2401.15884） | 微调 T5-large 检索评估器打置信度，分成 Correct / Ambiguous / Incorrect 三档并触发不同动作（§4.2–4.3） | G-CRAG 的分级就来自这里。注意：评估器打的是**检索结果与问题的相关度**，判断不了"缺了一跳" |
| Sufficient Context（Joren et al., arXiv 2411.06037） | 由 LLM 自动评判"上下文是否足以回答"（sufficient context autorater） | Gemini 1.5 Pro 1-shot 在 115 条人工标注上准确率 93.5%（Table 1）。另发现大模型在上下文不足时常常不弃权，直接给出错误答案（摘要） |
| FLARE（Jiang et al., arXiv 2305.06983） | 生成中出现低置信 token 时才触发检索（摘要） | 属于"生成器自报缺口"一类信号 |
| PopQA Adaptive Retrieval（Mallen et al., ACL 2023，arXiv 2212.10511） | 按问题实体的流行度阈值决定是否检索（§6） | 是基于实体属性触发的一手先例。本项目可以类比为"问题实体在图里是否有跨文档 2 跳邻居" |
| *RAG vs. GraphRAG*（Han et al., arXiv 2502.11371v3） | ① Selection：LLM 把问题分成事实型（走 RAG）或推理型（走 GraphRAG）；② Integration：两路都跑，上下文拼接（§4.5） | MultiHop-RAG（Llama-3.1-70B）上 Selection +1.1%，Integration +6.4%；Integration 普遍更好，但每题两路都要跑。两者互补：13.6% 的题只有 GraphRAG 答对，11.6% 只有 RAG 答对（Figure 2，8B） |

### 2.2 厂商和官方文档的做法

- **Microsoft GraphRAG**：官方文档按问题类型列出各方法的用途：local 适合"specific entities"，global 适合"the dataset as a whole"，DRIFT 在 local 基础上补充社区信息，basic 是 vector RAG 基线（[Query Overview](https://microsoft.github.io/graphrag/query/overview/)）。**没有自动路由**，由使用者自己选。LazyGraphRAG 博客写道"Vector RAG excels for local queries where the answer resembles the query"。BenchmarkQED 博客指出 data-local 问题"tend to benefit most from Vector RAG's ranking of directly relevant chunks"（[BenchmarkQED](https://www.microsoft.com/en-us/research/blog/benchmarkqed-automated-benchmarking-of-rag-systems/)）。
- **Neo4j**：`ToolsRetriever` 把 VectorRetriever、Text2CypherRetriever 等包装成工具，由 LLM 按工具描述选择（2025-09-18，[博客](https://neo4j.com/blog/developer/introducing-toolsretriever-graphrag-python-package/)）。博客原话是"VectorRetriever might excel at semantic searches, while a Text2CypherRetriever is better suited for precise, structured queries"。**没有评测数字**。
- **AWS Bedrock Knowledge Bases GraphRAG**：选用 Neptune Analytics 作为存储后，**每次查询都是"先向量检索，再沿图扩展 chunk"**，属于默认开启的设计，图的构建不可定制（[文档](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html)）。
- **LlamaIndex**：Router 通过 LLM selector 或 Pydantic selector 选择单个或多个数据源，适用场景包括"选数据源"和"选摘要还是语义检索"（[Routers 文档](https://developers.llamaindex.ai/python/framework/module_guides/querying/router/)）。**没有专门给出 KG 与向量之间的路由评测**。

### 2.3 "图谱默认开启"和"按需触发"的取舍

| 维度 | 默认开启（Bedrock 式、Integration） | 按需触发（Adaptive-RAG、CRAG、Selection） |
|---|---|---|
| 质量 | 互补性大时最好（*RAG vs. GraphRAG* Integration +6.4%）。但简单题可能被噪声拖累（GraphRAG-Bench Obs.1、Obs.4） | 上限取决于路由信号。Adaptive-RAG 的真实分类器和理想分类器之间差 11.9 F1 |
| 成本 | 每题都多一路；GraphRAG 类 prompt 可能大一到两个数量级（GraphRAG-Bench Figure 8/9） | 只在部分题上付费 |
| 可解释性 / 课程展示 | 难以说明"图谱何时起作用" | 触发率、挽回率可以直接作为指标（G-CRAG 现有设计） |
| 关键风险 | 拖累简单题 | **信号盲区**：触发信号与图谱要解决的问题正交时，永远不会触发或采纳（见 §5 P2） |

【经验】对 G-CRAG 来说，"按需触发"在课程叙事上更合适，但触发和采纳必须用能看到"缺一跳"的信号，例如充分性判断或"问题实体的 2 跳出处文档不在 top-k"。不能只用"与问题的相关度"。

---

## 3. 真实落地案例与经验教训

### 3.1 正收益案例

| 案例 | 来源 | 报告的数字 | 正收益来自哪里 | 可信度备注 |
|---|---|---|---|---|
| LinkedIn 客服 KG-RAG | arXiv 2404.17723（SIGIR 2024） | MRR 0.522→0.927，Recall@3 0.640→1.000（Table 1）；BLEU 0.057→0.377（Table 2）；GPT-4 + E5。上线约 6 个月，随机分组后解决时长中位数 7 h→5 h（−28.6%），均值 40 h→15 h（Table 3） | 图是**工单内部的树结构**（摘要、描述、复现步骤等字段）加**工单间的显式链接**（clone、related），不是 OpenIE；查询用 LLM 解析字段后生成 Cypher；查询出错时回退到文本检索（§3） | 黄金评测集的规模**原文未披露**（未核实） |
| 蚂蚁 KAG 政务问答 | arXiv 2409.13731 §4.1 | 11,000 篇政务文档，492 条样本，Precision 66.5→91.6，Recall 52.6→71.8（Table 12，NaiveRAG→KAG） | 事项、地区、材料等**半结构化字段**，加上人工补充的同义和上下位关系 | 底座未注明；样本内部构成未披露 |
| 蚂蚁 KAG 医疗问答 | 同上 §4.2 | 召回 60.67%、精度 81.32%；医保政策准确率 77.2% | 专家构建的 180 万实体、500 万关系，强 schema，700+ DSL 规则 | 没有 NaiveRAG 对照数字 |
| Lettria on AWS | [AWS 博客](https://aws.amazon.com/blogs/machine-learning/improving-retrieval-augmented-generation-accuracy-with-graphrag/)（2024-12-23） | 正确率 50.83%→80%；算上可接受答案 67.5%→约 90% | 依据领域定义抽取实体关系，把查询意图转成图查询 | 厂商自测；题量博客未披露（未核实） |
| Writer Knowledge Graph | [Writer 博客](https://writer.com/engineering/rag-benchmark/) | RobustQA 86.31%，对手 32.74～75.89% | 专用 LLM 建图并做压缩 | **Writer 自测，非独立评测** |
| Neo4j "GraphRAG Manifesto" | [Neo4j 博客](https://neo4j.com/blog/genai/graphrag-manifesto/) | 引用 Data.world 研究"accuracy 3x" | — | 原研究（arXiv 2311.07509）做的是**企业 SQL 库上的 Text-to-SQL 与 KG 上 SPARQL 的对比**（GPT-4 zero-shot，16%→54%），**不是文本 RAG**，不能拿来支撑文本多跳 |
| 微软 GraphRAG | 论文与博客 | 见 §1.2 | 全局概括类问题 | 依赖 LLM 评委；*RAG vs. GraphRAG* §5.3 发现这类评测有明显**位置偏差**，调换顺序后结论可能反转 |

### 3.2 冗余或有害的一手报告

1. ***RAG vs. GraphRAG: A Systematic Evaluation and Key Insights*（arXiv 2502.11371v3，2026-03 修订）**。统一设置：建图用 GPT-4o-mini，chunk 256 token，ada-002 embedding，top-10，生成用 Llama-3.1-8B/70B。
   - Table 1（8B）：NQ 上 RAG F1 64.78，优于 HippoRAG2 的 61.03 和 Community-Local 的 63.01。HotpotQA 上 HippoRAG2 63.01、Local 61.66 优于 RAG 60.04。**KG-GraphRAG（LlamaIndex 实现：KG 遍历后返回三元组）在 HotpotQA 上只有 25.02（仅三元组）和 42.60（三元组加原文）**。
   - Table 2（MultiHop-RAG）：RAG overall 67.02，HippoRAG2 70.27；Global 在 Null 题上只有 19.27，幻觉风险高。
   - 原因分析（§4.2(4)）：构建出的 KG 只覆盖约 65.8%（HotpotQA）和 65.5%（NQ）的答案实体。
   - 成本（Table 4）：MultiHop-RAG 上建图时间 RAG 135 s，KG-GraphRAG 7,702 s，Community 5,560 s。
   - **与本项目最相关**：G-CRAG 和原型 grid-qa"一跳三元组注入 prompt"的做法最接近 KG-GraphRAG 这一类，而这一类在该研究中表现最差。
2. ***When to use Graphs in RAG*（GraphRAG-Bench，arXiv 2506.05690v3）**。语料是 NCCN 医疗指南和 20 世纪前的小说，模型 GPT-4o-mini。
   - Obs.1：简单事实检索上，basic RAG 与 GraphRAG 相当或更好，"GraphRAG's extra graph-based processing may introduce redundant or noisy information"。
   - Table 2，Novel 数据集事实题 ACC：RAG（带 rerank）60.92，MS-GraphRAG 49.29。复杂推理题：HippoRAG2 53.38，RAG（带 rerank）42.93。
   - Figure 7：平均度数方面，HippoRAG2 为 8.75（Novel），MS-GraphRAG 为 1.48，**图的稠密度与检索表现相关**。
   - 文中有一处内部不一致：Obs.8 说 global 的 prompt"达到 4×10^4"，Figure 8 的表格却是 331,375。
   - 另有一篇同名但不同的 GraphRAG-Bench（arXiv 2506.02404，领域推理题）**本次没有读**。
3. ***In-depth Analysis of Graph-based RAG in a Unified Framework*（arXiv 2503.04338）**。统一用 BGE-M3，k=4，Llama-3-8B。
   - Table 5，MuSiQue accuracy：VanillaRAG 17.23，HippoRAG 17.00，ToG 9.37，G-retriever 2.73。HotpotQA：VanillaRAG 50.78，HippoRAG 50.32。
   - §7.2(2) 的结论："retaining the original text chunks is crucial"。只依赖图结构的方法（G-retriever、ToG、DALK）在多数数据集上表现差。
4. **HippoRAG 2 论文本身的动机**（摘要）："their performance on more basic factual memory tasks drops considerably below standard RAG"。也就是说，作者承认上一代结构化 RAG 在简单任务上会退化。
5. **本项目实测**（`docs/g-crag/eval/test3_report.md`）：gcrag 与 legacy 的动作分布完全相同，图谱定位证据挽回 0/5，可答题准确率 none 52/60，gcrag 50/60，gcrag 的可答题误拒为 2/60。**图谱条目只在 t20 一题进入最终上下文（各模式均答对），none→gcrag 的 2 题差距不能归因于图谱**；验收记录的逐题分析已确认差距来自 d13、t18 两道可答题被阈值拒答（top1 0.155 / 0.168 < low 0.18），其余差异为生成随机性（见 §5 P0）。

### 3.3 归纳：图谱什么时候带来正收益（基于上述来源的推断）

- **有正收益**：① 关系是显式的、结构化的（LinkedIn 的字段和链接、KAG 的事项和材料）；② 第二跳证据与问题不相似，但能通过实体到达（2Wiki 这类以实体为中心的数据集，HippoRAG 在 2Wiki 上增益最大）；③ 全局概括类问题（GraphRAG 的 global）。
- **冗余**：问题与证据语义相近，dense 加 rerank 已经召回（HotpotQA 上的 HippoRAG，NQ 上的 HippoRAG2）。本项目 19 道 2 跳候选题全部被 top5 直接召回，属于这种情况。
- **有害**：① 只给三元组、不给原文；② KG 覆盖率低，例如答案实体只有约 65% 进了图，本项目 72% 的实体只有一条边；③ 图谱候选挤掉有用的检索条目，本项目 test2 t30 已观察到，gcrag_v2 就是为此做的修正。

---

## 4. 怎样构造能体现图谱价值的评测集

### 4.1 公开基准的去捷径方法

**MuSiQue（Trivedi et al., arXiv 2108.00573）**：自底向上，把单跳问题组合成多跳问题。
- S2 可组合对：q1 的答案 a1 是一个出现在 q2 中的命名实体；a2 不出现在 q1 中；p1 ≠ p2；a1 与 q2 中的提及要通过三重校验（spaCy 实体类型一致、Wikipedia 搜索第一条结果相同、Wikification 结果相同）（§5 S2）。
- **S3 断连过滤（Disconnection Filter）**（§4.3 的条件 (2)、§5 S3）：
  - head 节点：模型在**不给上下文**时答不出 q1，平均答案 F1 < 0.5，用 5 折交叉的 Longformer 判定。
  - tail 节点：把 q2 中的 a1 遮掉后，配上 gold 段落和 9 个 BM25 干扰段落，模型仍答不出，要求平均答案 F1 ≤ 0.25，或者答案 F1 ≤ 0.75 且 support F1 < 1。
  - 效果：tail 问题只保留了 26.5%（§6）。
- S5 尽量减少训练集和测试集的泄漏；S6 用"去掉中间答案后的子问题拼接"检索干扰段落，让干扰项更难；S8 构造只差一段证据的不可答对照题。
- 效果（Table 4）：DiRe（断连推理可达的得分）的答案分，HotpotQA 68.8、2Wiki 63.4、MuSiQue 37.8；只看单段的 1-Para 模型分别为 64.8、60.1、32.0。

**HotpotQA（Yang et al., arXiv 1809.09600）**：
- bridge 题：沿 Wikipedia 首段超链接图的边 (a, b) 采样，b 是桥接实体，限定在人工整理过的页面集合内（§2）。
- comparison 题：比较两个实体的共同属性。
- 去捷径：抽样标注者，把主要只需单段的题划入 train-easy（18,089 条）；把单跳基线能答对的题划为 train-medium（56,814 条，占多跳样本的 60%）；只有剩下的 hard 题进入 dev 和 test（§3）。
- HippoRAG Appendix B 指出，HotpotQA 的干扰段与支持段的相似度分布差距大，干扰项"not very effective"。

**2WikiMultiHopQA（Ho et al., arXiv 2011.01060）**：
- 用 Wikidata 和模板生成四类题：comparison、inference（逻辑规则，例如 father∧father⇒grandfather）、compositional、bridge-comparison（§2）。
- 去掉 Min et al. 判定为单跳或依赖上下文的模板。
- 用单跳 BERT 做 5 折交叉，能答对的题划为 train-medium（§3）。

**MultiHop-RAG（Tang & Yang, arXiv 2401.15391，英文新闻）**：
- 先抽取 claim，再生成 bridge-entity 和 bridge-topic，然后由 GPT-4 生成 inference、comparison、temporal、null 四类问题（§3）。
- 最好的 embedding 加 reranker 的 Hits@10 只有 0.7467（Table 5）。
- 注意：它的问题**会显式提到桥接实体或主题**，考察的是"多文档聚合"，不是"隐藏桥接"。

### 4.2 适合本项目的构造方案（1～2 天，全自动判定，适用于中文新闻）

**目标子集**：隐藏桥接的 2 跳题。从实体 A 出发，经桥接 B 到达答案 C；问题只提 A；doc2 是答案所在的文档，**不提 A**。

**步骤**：
1. **候选路径枚举**（不调用模型，Neo4j 查询）：
   - 找满足以下条件的路径 A–r1–B–r2–C：(A,B) 边的出处 chunk 属于 doc1，(B,C) 边的出处属于 doc2，doc1 ≠ doc2。
   - B 不是枢纽实体（用现有的 `hub_degree`）。
   - **doc2 全文不包含 A 及其别名**（字符串匹配加实体词典）。这一条对应 MuSiQue 的 tail 条件。
   - doc1 不包含 C。
   - 为保证多样性，每个 B 最多用 3 次（参照 MuSiQue S4 的复用上限）。
   - 目标是 150～300 条候选。上次 30 条候选里 0 条是检索困难的，所以候选池要放大 5～10 倍。
2. **出题**（Claude，每条 1 次调用）：
   - 只给 Claude 两个三元组和 doc1 的相关片段，**不给 doc2 全文**，以减少词面泄漏。
   - 要求用"A 的 r1 对象"这种指代方式描述 B，问 B 的 r2，答案为 C。
   - 题面不能出现 B、C 及其别名。
3. **自动过滤**（每条最多 4 次 Claude 调用加 1 次检索，全部记录日志）：
   - F1 可答：把 doc1 和 doc2 一起给 Claude，能答对（用 LLM 判定）。这一步同时得到 oracle 上界。
   - F2 doc2 必要：只给 doc1 时答不出（对应 MuSiQue 的断连条件）。
   - F3 闭卷：不给上下文时答不出（对应 MuSiQue 的 M(q; φ) ≠ a）。
   - F4 **检索困难（自动判定"top5 召回不到桥接文档"）**：
     - 用**冻结的生产检索器**（同一 commit，BM25 + dense + gte-rerank-v2，配置和阈值都不变）对题面检索。
     - 判定条件：top5 中**没有任何 chunk 同时包含 B 和 C**。用"包含 B 和 C"而不是"等于 doc2"，是为了防止新闻语料里多篇报道同一事件造成误判。
     - 同时记录 doc2 在 top-20 和 top-50 中的名次，以及在 BM25-only、dense-only 下的名次。
   - F5 泄漏检查：题面与 doc2 的字符 3-gram Jaccard 低于阈值，阈值在候选集上取 P90 并写进文档。
4. **人工抽检**：随机抽 20 道题，检查题面是否自然、答案是否唯一，记录不合格率。

**产出的数据集**：
- 全体候选（通过 F1、F2、F3）
- 其中的"检索困难子集"（再通过 F4、F5）

两者都要报告。**检索困难子集的自然出现率本身就是一个发现**：如果在 CRUD-RAG 新闻语料里这个比例很低，就说明这个语料上图谱的价值空间本来就小。

**调用量估算（按 200 条候选）**：
- Claude：出题 200 次 + F1/F2/F3 共 600 次 + 判定约 200 次，合计约 1,000 次。
- 百炼：每题 1 次 query embedding 和 1 次 rerank，共约 400 次。如果走现有的录制回放机制，后续各模式评测会复用这些结果。

### 4.3 怎样避免"人为削弱检索"的嫌疑

1. **冻结检索器，只筛题目**：检索配置、阈值、模型在构造前就冻结，记录 commit 哈希。构造规则先写进文档（预注册），再跑 G-CRAG。
2. **公开漏斗**：报告候选数，以及每道过滤后剩下的数量；报告检索困难子集的自然比例。
3. **两套结果都报告**：原 70 题（含不可答题，用于检验无害性）和检索困难子集（用于检验价值）。另外报告未经 F4 过滤的全体 2 跳候选。
4. **加强基线**：在同一子集上对比 none、legacy（LLM 改写）和 Self-Ask 式分解检索（§5 P3）。图谱要与"非图谱的多步检索"比，不能只与单步检索比，这也是 HippoRAG 论文选择 IRCoT 作对照的做法。
5. **跨检索器稳健性**：只针对一个检索器做对抗筛选会产生选择偏差。所以要报告 doc2 在 BM25-only 和 dense-only 下的排名分布，并给出"多个检索器都召回不到"的子集结果。
6. **说明先例**：MuSiQue、HotpotQA、2Wiki 都用模型把能走捷径的题过滤掉或单独分出子集。在报告里明确写"检索困难子集"是**按构造得到的诊断集**，不代表线上分布。
7. **统计检验**：子集只有 30～40 题时，用 McNemar 检验或对不一致题对做精确二项检验，不要只报百分比。

---

## 5. 对本项目的改进建议（按投入/收益排序）

> 约束：剩约 8 天，至少留 3 天给报告、PPT 和演示；百炼余额有限；不削弱检索。

### P0 逐题归因 none 与 gcrag 的差距（约 2 小时，0 次外部调用）
- **改什么**：不改代码，只用 test3 的轨迹逐题列出 none 答对而 gcrag 答错的题，标注原因（误拒、改写、图谱）。
- **能证明什么**：把"G-CRAG 比不纠错低 3.4 个点"准确归因。从 test3 来看，图谱采纳了 0 次，gcrag 与 legacy 的动作分布完全相同，所以差距多半来自分级和拒答链路（可答题误拒 2/60），而不是图谱。这直接影响报告结论的措辞。
- **风险**：几乎没有。

### P1 构造检索困难的隐藏桥接 2 跳评测集（1～1.5 天）
- **改什么**：按 §4.2 和 §4.3 新增构造脚本和数据文件，不改检索代码。
- **调用量**：Claude 约 1,000 次，百炼约 400 次（200 条候选时）。
- **能证明什么**：图谱的价值是否存在于"问题与证据语义不相近"的场景。即使结论是"这个语料里这类题很少"，也是一个诚实、可写进报告的发现。
- **风险**：检索困难题可能太少，比如少于 20 道。应对办法是扩大候选池（多枚举 B），或者放宽到"doc2 不在 top5 但在 top20"，并分层报告。题面质量依赖 Claude，需要人工抽检。

### P2 修正触发和采纳判据的盲区（0.5～1 天，与 P1 并行）
- **问题诊断（推断）**：
  - 采纳要求"原上下文加图谱候选，用**原问题**重新 rerank 后分级提升"（设计文档 §6、`gcrag.correct`）。隐藏桥接场景里 doc2 与原问题不相似，所以 rerank 分数天然低，几乎不可能把 top1 拉高。
  - 触发条件是 rerank top1。doc1 与问题高度相关，于是被判为 correct，图谱根本不会被触发。
  - 两道关卡用的都是图谱要绕开的那种信号。HippoRAG 用图上的 PPR 分数排序，而不是查询相似度；HopRAG 把这类文档定义为"间接相关"。
- **改什么**（保留旧模式，新增模式，例如 gcrag_v3，以便对照）：
  1. **路径感知重打分**：给图谱候选 chunk 打分时，查询改为"原问题 + 路径文本（A —r1→ B —r2→ C）"，或改为由路径生成的子问题"B 的 r2 是什么"。不再用原问题打分。
  2. **采纳不再看分级是否提升**：沿用 gcrag_v2 的追加方式（原检索条目不动，图谱条目追加在后面），只按最终答案正确率评估。
  3. **触发加一个"缺口"信号**：在生成 prompt 里要求结构化输出 `sufficient | missing: <缺的信息>`。只有 missing 时才走图谱，并把 missing 文本作为图谱和子问题的查询。这类似 Self-Ask 和 FLARE 的思路，**上下文充分时不增加调用**。另一个零 LLM 调用的备选信号是"问题实体在图里有跨文档 2 跳邻居，且这些邻居的出处文档不在 top-k"，可以类比 PopQA 按实体属性触发的做法。
- **调用量**：百炼每次图谱轮约多 1 次 rerank（现有流程本来就有）；Claude 只在 missing 时多 1 次生成。
- **能证明什么**：在 P1 子集上，图谱采纳次数大于 0，且答案正确率提升；在原 70 题上不下降（无害）。
- **风险**：
  - Sufficient Context 论文发现，大模型在上下文不足时往往直接作答而不弃权，生成器可能很少报 missing。备选方案是单独做一次充分性判断（每题多 1 次 Claude 调用）。
  - 图谱抽取错误会经由路径文本引入噪声。
  - 这是在看过数据之后做的迭代，报告里要像 gcrag_v2 一样如实标注。

### P3 加一个 Self-Ask 式分解检索作为非图谱强基线（0.5 天）
- **改什么**：新增一个对照模式。Claude 先根据 top-k 判断第一跳答案并提出 1 个后续子问题，再用**同一个检索器**检索一次，合并后生成答案。最多 1 轮。
- **调用量**：每题 Claude +1～2 次，百炼 +1 次 embedding 和 +1 次 rerank。只在 P1 子集和原 70 题上跑。
- **能证明什么**：图谱的增量价值究竟是"能做多跳"，还是"比 LLM 多步检索更便宜、更稳"。HippoRAG 的卖点正是与 IRCoT 效果相当，但便宜 10～30 倍（Table 17）。如果 G-CRAG 与分解基线持平但调用更少，就是一个成立的结论；如果图谱更差，也应如实写进报告。
- **风险**：这个基线可能比图谱更好。这不算风险，而是诚实的结论；但需要预留时间改写报告叙事。

### P4（可选，P1～P3 完成后再做）在现有 Neo4j 图上做 PPR-lite 排序（1～1.5 天）
- **改什么**：把实体和出处 chunk 导出成二部图（参照 HippoRAG 2 的段落节点），在内存里用 networkx 跑 PPR。种子取问题链接到的实体，以及 top-k chunk 里的实体。按 PPR 分数取图谱候选，替代"2 跳遍历后再用原问题 rerank"。
- **调用量**：查询时不新增 LLM 或百炼调用。
- **能证明什么**：图上的排序信号是否比相似度更适合找第二跳文档。
- **风险**：72% 的实体只有一条边，图很稀疏，PPR 的作用有限。HippoRAG 用同义边加密图，那需要为全部实体做 embedding，会消耗百炼额度。时间紧时直接放进展望。

### 不建议在截止前做的事（写进"局限与展望"）

| 方案 | 不做的原因 |
|---|---|
| HippoRAG 2 全量重建（OpenIE + 三元组 embedding + LLM 过滤） | 要对全部 chunk 重新抽取，而且要为三元组和实体做 embedding，百炼额度不够；参考 HippoRAG 2 Table 12，11.6k 段需要 9.2M 输入 token |
| MS GraphRAG、LightRAG 索引 | 索引 token 是 HippoRAG 2 的 7～12 倍（HippoRAG 2 Table 12）；而且它们面向全局问题，不对口 |
| KAG | 需要领域 schema、互索引和逻辑形式求解器，工程量以周计 |
| ToG / ToG-2、HopRAG | 每题 5～40 次 LLM 调用；HopRAG 还要为每个段落生成伪问题 |
| 训练 Adaptive-RAG 式复杂度分类器 | 需要大量带标签的问题；原论文的分类器准确率也只有 54.5% |
| 在英文 MuSiQue / 2Wiki 上复现 | 语料要用百炼重新做 embedding，中文链路也要适配；可以写成"未来在标准基准上验证" |
| 图谱加密（同义边、实体消歧） | 需要标定和额度，但它可能是 72% 单边实体问题的根本解法，列为首要展望 |

### 建议的 8 天排期（参考）
- **D1**：P0；P1 路径枚举和出题。
- **D2**：P1 过滤和人工抽检，冻结数据集；并行开发 P2。
- **D3**：P2 和 P3 实现。
- **D4**：在原 70 题和 P1 子集上跑 none、legacy、gcrag、gcrag_v2、gcrag_v3、分解基线。
- **D5**：分析和统计检验。
- **D6～D8**：报告、PPT、演示。P4 只在 D3 有余量时做。

---

## 参考文献（标题 + 链接 + 实际读过的部分）

1. Gutiérrez et al. *HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models*. https://arxiv.org/abs/2405.14831 。读了 HTML v3 的 §3–§5、Table 1–7、Appendix B、G（Table 17/18）、H。
2. Gutiérrez et al. *From RAG to Memory: Non-Parametric Continual Learning for Large Language Models*（HippoRAG 2）. https://arxiv.org/abs/2502.14802 。读了摘要、§3、§4、Table 1–5、Appendix F（Table 12）、G.1。
3. Guo et al. *LightRAG: Simple and Fast Retrieval-Augmented Generation*. https://arxiv.org/abs/2410.05779 。读了 §3.2–3.4、§4 的数据集说明、Table 1/2 标题、§4.5 成本。
4. Edge et al. *From Local to Global: A GraphRAG Approach to Query-Focused Summarization*. https://arxiv.org/abs/2404.16130 。读了 §1、§4 的实验设置、§5 结果、Table 2、§6.1。
5. Microsoft GraphRAG 文档：Query Overview https://microsoft.github.io/graphrag/query/overview/ ；Local Search https://microsoft.github.io/graphrag/query/local_search/ 。读了全文。
6. Microsoft Research Blog：*Introducing DRIFT Search* https://www.microsoft.com/en-us/research/blog/introducing-drift-search-combining-global-and-local-search-methods-to-improve-quality-and-efficiency/ ；*LazyGraphRAG* https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/ ；*BenchmarkQED* https://www.microsoft.com/en-us/research/blog/benchmarkqed-automated-benchmarking-of-rag-systems/ 。读了方法和结果段落。
7. microsoft/graphrag 仓库 README 和 RAI_TRANSPARENCY.md：https://github.com/microsoft/graphrag 。读了 README 的警告、Transparency 的用途、限制和适用数据说明。
8. Liang et al.（蚂蚁）*KAG: Boosting LLMs in Professional Domains via Knowledge Augmented Generation*. https://arxiv.org/abs/2409.13731 。读了摘要、§1、§3.2–3.3、Table 8/9、§4 应用（Table 12）。
9. Sun et al. *Think-on-Graph*. https://arxiv.org/abs/2307.07697 。读了摘要、Table 1、附录中的 LLM 调用复杂度和 Table 7/8。
10. Ma et al. *Think-on-Graph 2.0*. https://arxiv.org/abs/2407.10805 。读了摘要、Table 1、Table 9 和成本说明。
11. Trivedi et al. *Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions*（IRCoT）. https://arxiv.org/abs/2212.10509 。读了摘要、§3、§5 结果（Fig.3/4 的文字描述）、局限部分。
12. Press et al. *Measuring and Narrowing the Compositionality Gap in Language Models*（Self-Ask）. https://arxiv.org/abs/2210.03350 。读了 §3.3–3.4、Table 1。
13. Liu et al. *HopRAG: Multi-Hop Reasoning for Logic-Aware Retrieval-Augmented Generation*. https://arxiv.org/abs/2502.12442 。读了摘要、§1、§3、Table 1/2/4。
14. Zhu et al. *Knowledge Graph-Guided Retrieval Augmented Generation*（KG2RAG）. https://arxiv.org/abs/2502.06864 。读了摘要、§2 方法、Table 1/2/7、Appendix A.2（Table 9/10）。
15. Jeong et al. *Adaptive-RAG*. https://arxiv.org/abs/2403.14403 。读了摘要、§3、Table 1、Table 3/4、分类器分析段落。
16. Yan et al. *Corrective Retrieval Augmented Generation*. https://arxiv.org/abs/2401.15884 。读了 §4.1–4.2（评估器与三档动作）。
17. Joren et al. *Sufficient Context: A New Lens on Retrieval Augmented Generation Systems*. https://arxiv.org/abs/2411.06037 。读了摘要和 §3.2 的 Table 1。
18. Jiang et al. *Active Retrieval Augmented Generation*（FLARE）. https://arxiv.org/abs/2305.06983 。只读了摘要。
19. Mallen et al. *When Not to Trust Language Models*（PopQA）. https://arxiv.org/abs/2212.10511 。读了 §6 Adaptive Retrieval 的标题和要点句。
20. Xu et al.（LinkedIn）*Retrieval-Augmented Generation with Knowledge Graphs for Customer Service Question Answering*. https://arxiv.org/abs/2404.17723 。读了全文（篇幅短）。
21. Han et al. *RAG vs. GraphRAG: A Systematic Evaluation and Key Insights*. https://arxiv.org/abs/2502.11371 （v3，2026-03-04）。读了 §3、§4.1–4.6、Table 1/2/4、§5.3、§6。
22. Xiang et al. *When to use Graphs in RAG: A Comprehensive Analysis for Graph Retrieval-Augmented Generation*（GraphRAG-Bench）. https://arxiv.org/abs/2506.05690 （v3）。读了摘要、§2–§4、Table 2/3、Figure 7–9 的表格、Obs.1–9。
23. Zhou et al. *In-depth Analysis of Graph-based RAG in a Unified Framework*. https://arxiv.org/abs/2503.04338 。读了 Table 5 和 §7.2 的观察 (1)–(3)。
24. Trivedi et al. *MuSiQue: Multihop Questions via Single-hop Question Composition*. https://arxiv.org/abs/2108.00573 。读了 §4.3、§5（S1–S8）、§6、§8.1 的 Table 4。
25. Yang et al. *HotpotQA*. https://arxiv.org/abs/1809.09600 。读了 §2（bridge 和 comparison 的构造）、§3（train-easy、train-medium 的划分）。
26. Ho et al. *Constructing A Multi-hop QA Dataset for Comprehensive Evaluation of Reasoning Steps*（2WikiMultiHopQA）. https://arxiv.org/abs/2011.01060 。读了 §2 的题型与模板过滤、§3 的单跳模型划分。
27. Tang & Yang. *MultiHop-RAG*. https://arxiv.org/abs/2401.15391 。读了 §3 的构造步骤、Table 1/3/5。
28. AWS 文档：*Build a knowledge base with Amazon Neptune Analytics graphs*. https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html 。读了全文。
29. AWS ML Blog：*Improving Retrieval Augmented Generation accuracy with GraphRAG*（Lettria，2024-12-23）. https://aws.amazon.com/blogs/machine-learning/improving-retrieval-augmented-generation-accuracy-with-graphrag/ 。读了基准和结果段落。
30. Neo4j Blog：*Introducing ToolsRetriever in the Neo4j GraphRAG Python package*（2025-09-18）. https://neo4j.com/blog/developer/introducing-toolsretriever-graphrag-python-package/ ；*The GraphRAG Manifesto* https://neo4j.com/blog/genai/graphrag-manifesto/ 。读了动机、用法和数字引用段落。
31. LlamaIndex 文档：Routers. https://developers.llamaindex.ai/python/framework/module_guides/querying/router/ 。读了全文。
32. Writer：*RAG benchmarking: Writer Knowledge Graph ranks #1*. https://writer.com/engineering/rag-benchmark/ 。读了结果段落（厂商自测）。
33. Sequeda et al. *A Benchmark to Understand the Role of Knowledge Graphs on LLM's Accuracy for Question Answering on Enterprise SQL Databases*. https://arxiv.org/abs/2311.07509 。只读了摘要，用于核实 Neo4j 引用的"3x"。

### 未核实或需注意的事项
- LinkedIn 黄金评测集的规模、Lettria/AWS 基准的题量：原文和博客都没有披露。
- KAG 的索引和查询成本：原文没有给出。
- LightRAG 在 HippoRAG 2 Table 2 中的复现数值异常低（MuSiQue F1 1.6），只能说明"按该复现设置失败"，不代表 LightRAG 的上限。
- IRCoT 的增益数字来自 Figure 3/4 的正文描述，不是表格；基线是 BM25 单步检索，不是 dense。
- GraphRAG-Bench（2506.05690）内部有一处不一致：Obs.8 文字说 global prompt 达到 4×10^4，Figure 8 表格为 331,375。同名的另一篇 GraphRAG-Bench（arXiv 2506.02404）本次没有读。
- §5 P2 关于"触发和采纳判据结构性失效"的诊断是基于设计文档和机制的推断，需要用 P1 子集的轨迹验证，例如 doc2 的 rerank 分数分布。
