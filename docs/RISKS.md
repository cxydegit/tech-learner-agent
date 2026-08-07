# 已知隐患与技术决策记录

> 本文档只收录**对后续开发阶段有指导意义**的隐患与决策；已解决、且修复已固化在代码里的实现细节不再重复记录。
> **开发到对应阶段时，务必先阅读本文档对应章节。** 避免重蹈覆辙。

---

## 背景：线性流程为什么用确定性管道（而非 agentic）

原始设计让模型用 **ReAct 文本循环 + function calling** 自主编排工具（search/fetch/save_file）。
实测（阿里云百炼 `qwen3.7-plus`）暴露根本问题：把大段内容塞进工具参数靠文本解析会被截断，而原生
function calling 又偶发 400 / 幻觉"宣称已保存"。**共同根因是"让模型自主编排工具"这个陷阱**。

**结论**：`collect` / `dig` 改为**确定性管道**（代码编排工具，模型只做一次生成），与 `read` / `note` 一致。
线性流程用确定性管道；真正的 agentic 编排留给 Stage 3（LangGraph）。

---

## 隐患 1：LLM 合成内容仍可能幻觉（质量风险，非崩溃）

- **现象**：管道不再崩，但模型可能编造链接/摘要、声称抓取不存在的资料，或输出大量「待补充」的次品报告。
- **缓解**：合成提示词（`COLLECT_COMPOSE_PROMPT` / `DIG_COMPOSE_PROMPT`）已写明"只基于我提供的资料、不要编造链接、没有的信息标注待补充"。可进一步：`temperature=0` + 对报告做一次链接有效性校验。
- **涉及阶段**：Sprint 1（当前，仍开放）。

## 隐患 2：Stage 3（LangGraph）不能默认走 function calling

- **现象**：若 LangGraph 节点内部用 function calling，遇到 `qwen3.7-plus` 会偶发 HTTP 400（模型不支持原生 `tool_calls`）。
- **缓解**：**LangGraph 节点内部用确定性工具调用**（与现在管道一致，代码直接调工具），或届时换一个支持原生 function calling 的模型（gpt 系）。
- **涉及阶段**：Stage 3。

## 隐患 3：Agent 基线（ReAct）的文本解析仍脆弱

- **现象**：`Agent` 类的 ReAct 文本循环现已不被 CLI 使用（保留给 Stage 4 benchmark 当对比基线），但其 `_parse_action` 对"大段内容作为工具参数"仍脆弱。
- **缓解**：benchmark 选**不依赖打大段内容**的简单任务（如"搜索并返回 3 个链接"），避免基线重蹈覆辙。
- **涉及阶段**：Stage 4（benchmark）。

## 隐患 4：function calling 的定位变化（能力选项，非修复路径）

- **现象**：`AGENT_USE_FUNCTION_CALLING` 默认 `false`。function calling 代码保留，但仅对支持原生 `tool_calls` 的模型生效。
- **含义**：面试讲故事时，function calling 是"能力选项"而非 collect/dig 的修复手段；实现亮点应强调"确定性管道 vs agentic loop 的取舍"。
- **涉及阶段**：全局（README / 面试叙事）。

---

## 隐患 5：DashScope embedding 批量上限 10 + Windows 路径分隔符（Stage 2）

- **现象**：① 一次嵌入 20 条文本报 `batch size is invalid, it should not be larger than 10`；② 语义去重明明命中却合并不了。
- **根因**：① 百炼 embeddings 单请求上限 10（比 OpenAI 的 25 更严）；② Windows 下 `str(Path.relative_to(...))` 产生反斜杠，与 RAG 索引的 POSIX 斜杠不一致，`path` 比较恒为 False——**语义路径实际是死代码，所有合并都走了字符串兜底**。
- **缓解**：① `_BATCH_SIZE = 10`；② 所有对外路径统一 `.as_posix()`（`get_existing_notes`、`persist_note` 返回值、RAG 元数据）。
- **涉及阶段**：Stage 2（RAG）；后续任何 embedding 批量逻辑都要守 10 的上限。

## 隐患 6：语义去重 vs 纯字符串去重的取舍（Stage 2）

- **取舍**：若把"embedding 召回 top1 **且** 字符串 overlap"作为合并硬条件，"字面零重叠的同义改写"无法合并——语义层失去价值。
- **缓解**：`_find_dedup_match` 采用**语义为主、overlap 为辅、字符串兜底**：相似度 >= `RAG_DEDUP_THRESHOLD`(0.55) 即合并（抓住同义改写）；未达阈值但主题重叠也可合并；两者都不中回退纯字符串匹配。read 缓存语义阈值取 0.62（URL 路径片段作查询词噪声大，取高防误报）。

---

## 隐患 7：纯字符分块会撕碎 Markdown 表格 / 代码围栏（Stage 2 分块器 v2）

- **现象**：`chunk_text` 按空行分段落 + 800 字符截断，对 Markdown 有两个真实弱点。实测复现：`materials/spring-boot-materials.md` 的 `## 一` 大表（≈2000 字符）被切成 3 块，第 2 块以 `'Deblauwe) | https://...'` 这种**单元格中间的残片**开头，且完全丢失章节上下文。
- **根因**：表格行间无空行 → 整张表算一个"段落"，一旦超长就走字符硬切分支；分块与 Markdown 结构无关。
- **缓解（chunker v2）**：新增 `chunk_markdown`，单遍扫描成块——长表格 / 长代码围栏**原子成块**（绝不合并 / 切分，即使超 `chunk_size`），标题作为**章节前缀**（`# ... › ## ...`）进入每块，overlap 只用普通正文尾部续接（**不变量：overlap 永不始于原子块内部**）。`chunk_text` 保留作纯文本兜底与"before"基线。
- **索引版本机制**：`content_hash = sha1(f"{CHUNKER_VERSION}\n{content}")`，**改动分块逻辑时必须递增 `CHUNKER_VERSION`**——版本提升后既有 hash 全部失配，首次 `index` 自动全量重切，无需手动清库；`index --force` 作手动逃生舱（改 `chunk_size` 等参数时用）。
- **涉及阶段**：Stage 2（RAG）；后续分块逻辑演进复用同一机制。

## 决策速查

| 决策 | 结论 |
|------|------|
| collect/dig 用 agentic 循环还是确定性管道？ | **确定性管道**（与 read/note 一致） |
| 默认是否开 function calling？ | **否**（`AGENT_USE_FUNCTION_CALLING=false`），模型不支持 |
| Agent 类（ReAct）还保留吗？ | **保留**，作为 Stage 4 benchmark 对比基线 |
| Stage 3 LangGraph 节点怎么做工具调用？ | **确定性工具调用**，不赌 function calling |
| 向量库用什么？ | **Chroma PersistentClient**（本地 `.chroma/`，cosine 度量），自定义 DashScope EmbeddingFunction |
| Embedding 用什么模型？ | **阿里云百炼 text-embedding-v3**（走 openai 兼容端点，批量上限 10） |
| note 去重用语义还是纯字符串？ | **语义为主 + 字符串兜底**（见隐患 6） |
| RAG 索引怎么保持新鲜？ | persist_note 写后**增量更新**该文件分块 + `index` 命令全量重建（变更检测避免重复计费） |
| Markdown 文档怎么分块？ | **Markdown 感知切块（chunker v2）**：表格/代码围栏原子化 + 标题章节前缀；`CHUNKER_VERSION` 版本号变更自动全量重切（见隐患 7） |
