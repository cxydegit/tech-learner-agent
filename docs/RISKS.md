# 已知隐患与技术决策记录

> 本文档只收录**对后续开发阶段有指导意义**的隐患与决策；已解决、且修复已固化在代码里的实现细节不再重复记录。
> **开发到对应阶段时，务必先阅读本文档对应章节。** 避免重蹈覆辙。

---

## 背景：线性流程为什么用确定性管道（而非 agentic）

原始设计让模型用 **ReAct 文本循环 + function calling** 自主编排工具（search/fetch/save_file）。
实测（阿里云百炼 `qwen3.7-plus`）暴露根本问题：把大段内容塞进工具参数靠文本解析会被截断，而原生
function calling 又偶发 400 / 幻觉"宣称已保存"。**共同根因是"让模型自主编排工具"这个陷阱**。

**结论**：`collect` 改为**确定性管道**（代码编排工具，模型只做一次生成），与 `read` / `note` 一致。
线性流程用确定性管道；真正的 agentic 编排留给 Stage 3（LangGraph）。

---

## 隐患 1：LLM 合成内容仍可能幻觉（质量风险，非崩溃）

- **现象**：管道不再崩，但模型可能编造链接/摘要、声称抓取不存在的资料，或输出大量「待补充」的次品报告。
- **缓解**：合成提示词（`COLLECT_COMPOSE_PROMPT` / `DIG_COMPOSE_PROMPT`）已写明"只基于我提供的资料、不要编造链接、没有的信息标注待补充"。可进一步：`temperature=0` + 对报告做一次链接有效性校验。
- **涉及阶段**：Sprint 1（当前，仍开放）。

## 隐患 2：Agent 基线（ReAct）的文本解析仍脆弱

- **现象**：`Agent` 类的 ReAct 文本循环现已不被 CLI 使用（保留给 Stage 4 benchmark 当对比基线），但其 `_parse_action` 对"大段内容作为工具参数"仍脆弱。
- **缓解**：benchmark 选**不依赖打大段内容**的简单任务（如"搜索并返回 3 个链接"），避免基线重蹈覆辙。
- **涉及阶段**：Stage 4（benchmark）。

## 隐患 3：function calling 的定位变化（能力选项，非修复路径）

- **现象**：`AGENT_USE_FUNCTION_CALLING` 默认 `false`。function calling 代码保留，但仅对支持原生 `tool_calls` 的模型生效。
- **含义**：面试讲故事时，function calling 是"能力选项"而非 collect/dig 的修复手段；实现亮点应强调"确定性管道 vs agentic loop 的取舍"。
- **涉及阶段**：全局（README / 面试叙事）。

---

## 隐患 4：DashScope embedding 批量上限 10 + Windows 路径分隔符（Stage 2）

- **现象**：① 一次嵌入 20 条文本报 `batch size is invalid, it should not be larger than 10`；② 语义去重明明命中却合并不了。
- **根因**：① 百炼 embeddings 单请求上限 10（比 OpenAI 的 25 更严）；② Windows 下 `str(Path.relative_to(...))` 产生反斜杠，与 RAG 索引的 POSIX 斜杠不一致，`path` 比较恒为 False——**语义路径实际是死代码，所有合并都走了字符串兜底**。
- **缓解**：① `_BATCH_SIZE = 10`；② 所有对外路径统一 `.as_posix()`（`get_existing_notes`、`persist_note` 返回值、RAG 元数据）。
- **涉及阶段**：Stage 2（RAG）；后续任何 embedding 批量逻辑都要守 10 的上限。

## 隐患 5：语义去重 vs 纯字符串去重的取舍（Stage 2）

- **取舍**：若把"embedding 召回 top1 **且** 字符串 overlap"作为合并硬条件，"字面零重叠的同义改写"无法合并——语义层失去价值。
- **缓解**：`_find_dedup_match` 采用**语义为主、overlap 为辅、字符串兜底**：相似度 >= `RAG_DEDUP_THRESHOLD`(0.55) 即合并（抓住同义改写）；未达阈值但主题重叠也可合并；两者都不中回退纯字符串匹配。read 缓存语义阈值取 0.62（URL 路径片段作查询词噪声大，取高防误报）。

## 隐患 6：单文件臃肿 → 分层重构，如何防回归（Step 2）

- **现象**：`agent.py` 805 行 + `prompts.py` 389 行 + `rag.py` 533 行堆成一个"上帝文件"，pipeline / Agent / 工具 / 提示词全部耦合，改一处牵全身。
- **决策（Step 2 落地）**：move-and-re-export 分层——`domain/`（纯规则，零 I/O，可独立单测）、`adapters/`（外部 I/O）、`pipelines/`（确定性管道，prompts 就近）、`graph.py`（LangGraph 编排，节点不 print）、`cli.py`（接口/渲染/交互）、`baselines/react_agent.py`（ReAct 基线冻结，主流程不 import）。依赖方向 `cli → graph → pipelines → adapters → domain`，禁止向上 import。
- **回归防线 I1**：`import src.cli` 不得把 chromadb / langgraph 放入 `sys.modules`（保证接口层轻量、冷启动快）。靠五处 lazy import 维持：`store.py::_find_dedup_match`/`_update_rag_index`、`cli.py::index`/`_try_reuse_cached_report`/`learn`。**后续新增顶层 import 时务必守住这条**——重构时在 `adapters/vector.py`、`graph.py` 的 import 就曾险些把 chromadb/langgraph 带上顶层。
- **研究层隔离**：`baselines/` 只供 benchmark，任何主流程代码不得 import 它；ReAct 基线的 `_extract_json_object` 保持逐字副本，不与 `domain/extraction.py` 合并。
- **涉及阶段**：Step 2 之后的所有开发。


