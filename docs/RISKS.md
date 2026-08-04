# 已知隐患与技术决策记录

> 本文档记录开发过程中发现的关键隐患、对应的缓解措施，以及已经做出的技术决策。
> **开发到对应阶段时，务必先阅读本文档对应章节。** 避免重蹈覆辙。

---

## 背景：为什么 collect/dig 改成了确定性管道

原始设计让模型用 **ReAct 文本循环**自主编排工具（search/fetch/save_file），并尝试升级为 **function calling**。实测（阿里云百炼 `qwen3.7-plus`）暴露了根本问题：

| 失败模式 | 现象 | 根因 |
|---------|------|------|
| P1 | `save_file` 大段内容被正则截断，反复重试至超时 | 内容塞进 JSON 参数、靠文本解析 |
| P2 | function calling 偶发 HTTP 400 | 模型不支持原生 `tool_calls`，随机走坏路径 |
| P3/P4 | 只调一次 search 就"宣称保存成功"（未真正调用） | 模型懒惰/幻觉，文本循环无法约束 |

**共同根因**：让模型用自由文本自主编排工具 + 把大内容塞进工具参数。无论换解析方式还是换 function calling，都没有跳出"让模型自主编排"这个陷阱。

**结论**：`collect` / `dig` 改为**确定性管道**（代码编排工具，模型只做一次生成），与 `read` / `note` 一致。确定性管道是线性流程的正确工程选择；真正的 agentic 编排留给 Stage 3（LangGraph）。

---

## 隐患 1：LLM 合成内容仍可能幻觉（质量风险，非崩溃）

- **现象**：管道不再崩，但模型可能编造链接/摘要、声称抓取不存在的资料，或输出大量「待补充」的次品报告。
- **缓解**：合成提示词（`COLLECT_COMPOSE_PROMPT` / `DIG_COMPOSE_PROMPT`）已写明"只基于我提供的资料、不要编造链接、没有的信息标注待补充"。可进一步：`temperature=0` + 对报告做一次链接有效性校验。
- **涉及阶段**：Sprint 1（当前）。

## 隐患 2：报告可能被包进 ``` 代码块围栏

- **现象**：模型有时输出 ```md ... ``` 包裹的整段报告，直接落盘会带围栏，影响渲染。
- **缓解**：保存前对 `report` 做一次 strip 代码围栏的清洗（`re.sub(r"^```[a-zA-Z]*\s*", "", ...)` 等）。
- **涉及阶段**：Sprint 1（当前）。

## 隐患 3：Stage 3（LangGraph）不能默认走 function calling

- **现象**：若 LangGraph 节点内部用 function calling，遇到 `qwen3.7-plus` 会再遇到 P2 的 400。
- **缓解**：**LangGraph 节点内部用确定性工具调用**（与现在管道一致，代码直接调工具），或届时换一个支持原生 function calling 的模型（gpt 系）。
- **涉及阶段**：Stage 3。

## 隐患 4：Agent 基线（ReAct）的文本解析仍脆弱

- **现象**：`Agent` 类的 ReAct 文本循环现已不被 CLI 使用（保留给 Stage 4 benchmark 当对比基线），但其 `_parse_action` 对"大段内容作为工具参数"仍脆弱。
- **缓解**：benchmark 选**不依赖打大段内容**的简单任务（如"搜索并返回 3 个链接"），避免基线重蹈 P1。
- **涉及阶段**：Stage 4（benchmark）。

## 隐患 5：function calling 的定位变化（能力选项，非修复路径）

- **现象**：`AGENT_USE_FUNCTION_CALLING` 默认 `false`。function calling 代码保留，但仅对支持原生 `tool_calls` 的模型生效。
- **含义**：面试讲故事时，function calling 是"能力选项"而非 collect/dig 的修复手段；实现亮点应强调"确定性管道 vs agentic loop 的取舍"。
- **涉及阶段**：全局（README / 面试叙事）。

---

## 隐患 6：Chroma 1.x 自定义 EmbeddingFunction 的 API 坑（Stage 2）

- **现象**：① 集合初始化时报 `'DashScopeEmbeddingFunction' object has no attribute 'name'`；② query 时报 `no attribute 'embed_query'`。
- **根因**：Chroma 1.x 的 `EmbeddingFunction` 协议要求实现 `name()`，且 `embed_query()/embed_documents()` 的默认实现定义在协议基类上——**必须继承 `chromadb.api.types.EmbeddingFunction`**（而不是只实现 `__call__`）。
- **缓解**：`DashScopeEmbeddingFunction` 继承 `EmbeddingFunction[Documents]`，实现 `__call__` + `name()`；query/文档共用同一编码（text-embedding-v3 不区分）。
- **涉及阶段**：Stage 2（RAG）。

## 隐患 7：Chroma 1.x 多条件 where 必须显式 `$and`

- **现象**：`collection.get(where={"source": "reports", "url": url})` 抛 `ValueError: Expected where to have exactly one operator`。
- **根因**：Chroma 1.x 的 where 只接受单操作符字典，多等值条件必须写成 `{"$and": [{k: v}, ...]}`。
- **缓解**：`rag._where_and()` 统一组装；单条件原样返回，多条件包 `$and`。
- **涉及阶段**：Stage 2（RAG）。

## 隐患 8：DashScope embedding 批量上限 10 + Windows 路径分隔符（Stage 2）

- **现象**：① 一次嵌入 20 条文本报 `batch size is invalid, it should not be larger than 10`；② 语义去重明明命中却合并不了。
- **根因**：① 百炼 embeddings 单请求上限 10（比 OpenAI 的 25 更严）；② Windows 下 `str(Path.relative_to(...))` 产生反斜杠，与 RAG 索引的 POSIX 斜杠不一致，`path` 比较恒为 False——**语义路径实际是死代码，所有合并都走了字符串兜底**。
- **缓解**：① `_BATCH_SIZE = 10`；② 所有对外路径统一 `.as_posix()`（`get_existing_notes`、`persist_note` 返回值、RAG 元数据）。
- **涉及阶段**：Stage 2（RAG）。

## 隐患 9：语义去重 vs 纯字符串去重的取舍（Stage 2）

- **现象**：若把"embedding 召回 top1 **且** 字符串 overlap"作为合并硬条件，则"落地时的坑与难点"（语义 0.629 命中"RAG 实施挑战"、字面零重叠）无法合并——语义层失去价值。
- **缓解**：`_find_dedup_match` 采用 **语义为主、overlap 为辅、字符串兜底**：相似度 >= `RAG_DEDUP_THRESHOLD`(0.55) 即合并（抓住同义改写）；未达阈值但主题重叠也可合并；两者都不中回退纯字符串匹配。read 缓存语义阈值取 0.62（URL 路径片段作查询词噪声大，取高防误报）。

---

## 决策速查

| 决策 | 结论 |
|------|------|
| collect/dig 用 agentic 循环还是确定性管道？ | **确定性管道**（与 read/note 一致） |
| 默认是否开 function calling？ | **否**（`AGENT_USE_FUNCTION_CALLING=false`），模型不支持 |
| Agent 类（ReAct）还保留吗？ | **保留**，作为 Stage 4 benchmark 对比基线 |
| Stage 3 LangGraph 节点怎么做工具调用？ | **确定性工具调用**，不赌 function calling |
| 报告保存前要不要清洗代码围栏？ | 要（见隐患 2） |
| 向量库用什么？ | **Chroma PersistentClient**（本地 `.chroma/`，cosine 度量），自定义 DashScope EmbeddingFunction |
| Embedding 用什么模型？ | **阿里云百炼 text-embedding-v3**（走 openai 兼容端点，批量上限 10） |
| note 去重用语义还是纯字符串？ | **语义为主 + 字符串兜底**（见隐患 9） |
| RAG 索引怎么保持新鲜？ | persist_note 写后**增量更新**该文件分块 + `index` 命令全量重建（变更检测避免重复计费） |