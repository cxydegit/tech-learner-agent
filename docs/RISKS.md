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

## 隐患 5：语义去重 vs 字符串去重的取舍——确定性信号的天花板（Stage 2 → P0 重构）

- **取舍**：若把"embedding 召回 top1 **且** 字符串 overlap"作为合并硬条件，"字面零重叠的同义改写"无法合并——语义层失去价值。
- **P0 重构（教训）**：第一版确认层（标题/具体标签/内容判别性概念 overlap）在**手写黄金集**上 100% 正确，但 LLM 合成压力测试（`scripts/eval_dedup_synth.json`，生成者 ≠ 标注者）揭穿：**确定性表面特征无法识别真正措辞不同的同义改写**——即使源笔记就是候选，三信号确认率仅 9%；内容 containment 信号 0/47（手写黄金集"有效"是因为我写的用例复述了源笔记措辞，是假阳性）；标签信号还会撞到错误候选造成错合并。手写黄金集不能当真实准确率。
- **缓解（现行方案）**：候选召回（语义 top2，相似度 < `RAG_DEDUP_JUDGE_SIM_MIN`(0.4) 不送判定）→ 标题 fast-path（`domain/dedup._title_fast_match`：去停用词后词元集合**完全相等**，只认"标题基本同一句"，只省 LLM 调用、不决定合并）→ **LLM 判定**（`adapters/llm.judge_same_knowledge_point`，输出 same/diff + 理由）。判定 same 仍走 `merge_candidates` 用户确认（用户是最终闸门）；LLM 判定失败降级为不合并（安全侧）。教训：**确定性规则做不了语义等价判断，灰色区必须上 LLM + 用户确认兜底**。
- **注意**：去重维度评估（`--dedup` / `--dedup-synth`）现在会打真实 LLM 判定调用（~百次），跑评估要算配额。
- read 缓存语义阈值取 0.62（URL 路径片段作查询词噪声大，取高防误报）。

## 隐患 6：单文件臃肿 → 分层重构，如何防回归（Step 2）

- **现象**：`agent.py` 805 行 + `prompts.py` 389 行 + `rag.py` 533 行堆成一个"上帝文件"，pipeline / Agent / 工具 / 提示词全部耦合，改一处牵全身。
- **决策（Step 2 落地）**：move-and-re-export 分层——`domain/`（纯规则，零 I/O，可独立单测）、`adapters/`（外部 I/O）、`pipelines/`（确定性管道，prompts 就近）、`graph.py`（LangGraph 编排，节点不 print）、`cli.py`（接口/渲染/交互）、`baselines/react_agent.py`（ReAct 基线冻结，主流程不 import）。依赖方向 `cli → graph → pipelines → adapters → domain`，禁止向上 import。
- **回归防线 I1**：`import src.cli` 不得把 chromadb / langgraph 放入 `sys.modules`（保证接口层轻量、冷启动快）。靠五处 lazy import 维持：`store.py::_find_dedup_match`/`_update_rag_index`、`cli.py::index`/`_try_reuse_cached_report`/`learn`。**后续新增顶层 import 时务必守住这条**——重构时在 `adapters/vector.py`、`graph.py` 的 import 就曾险些把 chromadb/langgraph 带上顶层。
- **研究层隔离**：`baselines/` 只供 benchmark，任何主流程代码不得 import 它；ReAct 基线的 `_extract_json_object` 保持逐字副本，不与 `domain/extraction.py` 合并。
- **涉及阶段**：Step 2 之后的所有开发。

## 隐患 7：超大原子块可能超出 embedding 输入上限（分块器硬上限补丁）

- **现象**：分块器保证"表格/代码块整块保留，即使超 chunk_size"。若索引到病态超大块（超长表格 / 整段日志代码），单块可能超出 embedding 输入上限，导致**整个文件索引失败**。
- **缓解（硬上限 + 逻辑二次切分）**：`chunk_markdown` 增加 `hard_cap`（默认 `RAG_CHUNK_HARD_CAP`=8192 字符）。超过它的原子块不再整块保留，按逻辑结构二次切分——**表格按行分组、每块重复表头**；**代码按空行（逻辑段落）分组、每块闭合为完整围栏**；单行/单段落仍超限时**截断保底**（损失尾部，但保证不产超限块、不崩）。正常块（当前语料最大 ~2300 字符）不受影响，仍整块保留。
- **涉及阶段**：Stage 2（RAG）；后续索引超大文档时注意。改分块逻辑仍需递增 `domain/chunking.py` 的 `CHUNKER_VERSION`（当前 v3 已含此补丁）。


