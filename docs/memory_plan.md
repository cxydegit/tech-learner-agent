# 记忆系统增强计划（memory_plan）

## 背景与目标

route（定制化学习路线）模块已有记忆部件：工作记忆（最近 10 轮）、情境记忆（coach_summary 摘要）、用户记忆（profile.json）、任务状态（roadmap + checkpointer）、语义记忆（知识库 + note/ask 工具）。当前缺口：**语义记忆的写入与读取依赖 agent 自觉**，且情境记忆、用户记忆存在"只增不减 / 只写不读"问题。

本计划分五步补齐，形成一个读写确定、分类齐全的记忆系统：

1. **Step 1 确定性写触发**——学习内容自动沉淀进知识库（已实施）
2. **Step 2 确定性读路由**——提问先查库，命中注入上下文作答（本步为详细实施计划）
3. **Step 3 记忆冲突解决**——合并时识别矛盾、以新内容为准改写并报告（本步为详细实施计划）
4. **Step 4 摘要自我整理**——三舱记忆（事实/未决/脉络），增量提取+确定性淘汰（本步为详细实施计划）
5. **Step 5 跨会话记忆**——画像读回、摘要与路线跨线程继承（设计草案）

---

## Step 1：确定性写触发（学习内容自动沉淀）【详细实施计划】

### 动机

coach 模式下学习内容（read 报告、agent 讲解、问答对话）的沉淀依赖 agent 主动调 `note` 工具——会漏。改为确定性触发：系统在对话积累到阈值后自动把对话内容喂给现有 note 管道沉淀。

### 已确认的设计决策

- 批处理窗口：自上次沉淀以来累计 **≥6 个用户回合** 或 **≥2500 字**（任一达到即触发），避免每回合一次 LLM 提取的浪费。
- 结果处理：**新知识点自动落库**（agent 可自然提及）；**只有相似候选才询问用户**（复用现有确认流）。
- **并行执行（v2 已落地）**：后台 daemon 线程跑纯管道（只读+LLM），结果经**进程内内存侧信道**交回，下一用户回合排水落库——沉淀耗时不再阻塞对话（异步），fire 时清 buffer 快照入 `memory_sweep_inflight`。线程失败/超时/进程重启 → **快照并回 buffer**（不重跑不阻塞），交给未来正常 fire 重扫。
- **确定性反馈/确认（v2 落地）**：自动沉淀反馈走 **SSE 进度事件**（不经过 agent）；相似候选由**确定性确认节点**（`coach_candidate_confirm`，interrupt 用户 → 解析决定 → 落库），不再依赖 agent 转述。`ROUTE_MEMORY_SWEEP_ASYNC=false` 退回 v1 同步路径（逃生舱）。

### 复用件（零新管道代码）

| 现成函数 | 用途 |
|---|---|
| `note_pipeline(tech, text)` | 召回 → 差量提取 → 相似匹配（纯函数，不落盘） |
| `persist_points(tech, new_points, candidates, indices)` | 落库：新建 + 按决定合并 |
| `format_merge_candidates` / `parse_merge_decision` | 候选展示与用户决定解析（all / 1,3 / skip） |
| `coach_note_pending` 状态 + `note_commit` 工具 | 候选待确认流的现有载体 |
| `progress` 回调 | 进度提示透传（Web SSE / CLI） |

### 图改动（graph.py）

```
coach_human（用户回复后）
  → coach_survey（透传）
  → coach_memory_write（新节点，本步核心）
  → coach_trim → coach_llm → ...
```

新节点 `coach_memory_write` 逻辑（coaching 模式外直接返回空更新）：

1. `coach_note_pending` 非空（agent 的 note 工具流进行中）→ 跳过（防止与工具流打架）。
2. 读 `memory_sweep_buffer`（自上次沉淀以来的消息对列表）：
   - 轮数 < `ROUTE_MEMORY_SWEEP_TURNS` 且字符数 < `ROUTE_MEMORY_SWEEP_CHARS` → 清空返回（本步触发后 buffer 必清，见下）。
   - 达标 → 拼文本 → `note_pipeline(tech, text)`。
3. 按结果分派（全部确定代码）：
   - `empty_reason`（无新内容）→ 清 buffer，无动作（闲聊/过程消息天然被差量提取过滤）。
   - 有 `new_points` 且无候选 → `persist_points(new_points, [], {})` 自动落库；向 coach_messages 追加一条 system 提示"已自动沉淀 N 个新知识点"（agent 可在回复中自然提及，不强制）。
   - 有候选 → **新点与候选一起暂存** `coach_note_pending = {**result, "_tech": tech, "_auto": True}`，追加 system 提示让 agent 在回复里用 `format_merge_candidates` 呈现并请用户决定 → 用户回复后复用 `note_commit` 工具流落库（与现有 note 工具行为一致）。清 buffer。

buffer 填充：`coach_human` 在追加 conversation 时，同步把（本条 assistant 讲解文本, 用户回复）压入 `memory_sweep_buffer`。buffer 有界（≤2 条 × 阈值窗口，触发即清），不占上下文。

### 状态与配置

LearnState 新增：`memory_sweep_buffer: list`（`[{role, content}]`，checkpointer 自然持久化，中断恢复不丢）。

config.py 新增：

```
ROUTE_MEMORY_SWEEP_TURNS=6    # 自上次沉淀以来累计用户回合数阈值
ROUTE_MEMORY_SWEEP_CHARS=2500 # 自上次沉淀以来累计对话字符数阈值（任一达到即触发）
ROUTE_MEMORY_SWEEP_ASYNC=true # 并行沉淀（后台线程+内存侧信道）；false 退回 v1 同步
ROUTE_MEMORY_SWEEP_TIMEOUT=60 # 后台线程超时（秒），超时未出结果 → inflight 快照同步兜底
```

### 改动文件清单

- `config.py` —— 两个环境变量
- `graph.py` —— LearnState 字段 + coach_human 填充 buffer + 新节点 coach_memory_write + 改边（coach_survey → coach_memory_write → coach_trim）
- `pipelines/route.py` —— 新增 `run_memory_sweep(tech, buffer, progress)`（纯函数：拼文本 → note_pipeline → 按结果返回 {action: "skip"|"persisted"|"pending", ...}），节点只做编排
- `tests/test_memory_sweep.py` —— 新单测（见下）

### 测试计划（零网络，monkeypatch）

- 阈值判定：轮数达标触发 / 字数达标触发 / 都未达标跳过
- 无新内容 → 不落库、buffer 清空
- 新点无候选 → 自动落库（断言 knowledge/ 出现文件）+ system 提示
- 有候选 → pending 设置、不落库 → 用户回复 all/1,3/skip → note_commit 落库
- `coach_note_pending` 非空时跳过
- 非 coaching 模式不触发（survey / planning 对话不扫）
- 图级 e2e：coach 循环多轮对话自动触发沉淀全流程
- I1 回归：`import src.cli` 仍不加载 chromadb / langgraph

### 手工验证（交付用户决定测不测）

```
python -m src.cli route "Spring Boot"
# 进入 coaching 后连续对话 6+ 轮（agent 讲解 + 提问），观察：
#   1. 自动出现"正在沉淀学习内容…"进度
#   2. 新知识点自动入库（knowledge/ 下出现新文件，agent 回复里可提及）
#   3. 出现相似候选时弹出确认，决定后落库
```

### 明确不做（本步）

- 退出时强制沉淀——你说"结束"就干净退出（in-flight 结果留给下次继续时排水）

---

## Step 2：确定性读路由（提问先查库）【详细实施计划】

### 动机

coach 模式下用户提问时，agent 是否查知识库依赖模型自觉调 `ask` 工具——会漏查、可能凭上下文硬答（老内容已被压缩丢失细节）。改为确定性：用户每次提问，系统侧先做一次知识库检索，命中就把相关笔记片段作为上下文块注入提示词，再让 agent 回答——"提问先查库"成为默认行为。

### 已确认的设计决策

- **两级闸门**：
  - **廉价闸门**（确定性，零成本）：跳过明显非学习问题（过程/元问题：继续、现在到哪了、这个路线对吗；退出意图已另处理）。
  - **质量闸门**（相似度阈值）：查库后命中相似度 ≥ `ROUTE_KB_INJECT_SIM` 才注入；无命中或低于阈值 → 不注入。
- **未命中行为**：模型**用自己的知识正常回答**（不拒答），可如实标注"知识库里还没有相关记录，以下是我基于经验的讲解"；`ask` 工具保持"查我的笔记、没查到就说没有"语义**不变**。
- **成本**：查库 = 一次 embedding + 进程内向量检索（~几百 ms），**不额外调 LLM 综合**——综合回答复用 coach 常规调用，只把片段捎带进上下文。
- **检索范围**：限定当前 tech（用户正在学它），复用 qa 的混合检索（`qa.py::_search_notes`）。

### 图改动（graph.py）

```
coach_human（用户回复后）
  → coach_survey（透传）
  → coach_memory_write（Step 1 写触发）
  → coach_kb_retrieve（新节点，本步核心）
  → coach_trim → coach_llm → ...
```

新节点 `coach_kb_retrieve` 逻辑（coaching 模式外直接返回空更新）：

1. 取最后一条用户消息作问题；过廉价闸门（确定性元问题判定）→ 命中则清空 `kb_context` 返回。
2. 混合检索（限定 tech）→ hits；相似度 ≥ `ROUTE_KB_INJECT_SIM` 的取前 `ROUTE_KB_SNIPPETS` 条。
3. 有命中 → 把片段（截断 + 来源路径）写入 `kb_context`；无命中 / 低于阈值 → 清空。
4. 每用户回合覆盖 `kb_context`（下轮问题自动替换）。

**注入**：coaching 系统提示词（`_coaching_prompt`）读取 `kb_context`，渲染"知识库相关片段（标注来源）"上下文块，并提示模型"优先依据片段作答；片段未覆盖部分可用自己知识补充，但明确区分"。这是**提示词改动**，按 PROMPT_DESIGN 硬约束**落地前提交用户审核**。

### 状态与配置

LearnState 新增：`kb_context: list | None`（`[{path, snippet}]`，checkpointer 持久化；每用户回合由 retrieve 节点覆盖）。

config.py 新增：

```
ROUTE_KB_INJECT_SIM=0.5   # 注入相似度阈值（低于不注入）
ROUTE_KB_SNIPPETS=3       # 注入片段数上限
```

### 改动文件清单

- `config.py` —— 两个环境变量
- `graph.py` —— LearnState 字段 + 新节点 coach_kb_retrieve + 布线（coach_memory_write → coach_kb_retrieve → coach_trim）
- `pipelines/route.py` —— `run_kb_retrieve(tech, question)` 纯函数（元问题闸门 + 检索 + 阈值过滤 → 返回片段列表）+ `_coaching_prompt` 注入 `kb_context` 块
- `tests/test_kb_retrieve.py` —— 新单测（见下）

### 测试计划（零网络，monkeypatch）

- 廉价闸门：元问题不检索
- 质量闸门：相似度低于阈值不注入 / 达标注入
- 检索异常（RAG 未索引 / Chroma 异常）优雅降级为空
- 非 coaching 模式不检索
- 注入格式：coaching 提示词含 kb_context 块
- 图级 e2e：用户提问 → 命中注入 → agent 依据片段回答
- I1 回归：`import src.cli` 仍不加载 chromadb / langgraph

### 手工验证（交付用户决定测不测）

```
python -m src.cli route "Spring Boot"
# 进入 coaching 后提问一个已沉淀过的问题，观察：
#   1. agent 回答带知识库依据（来源标注）
#   2. 提问知识库没有的内容 → agent 用自己的知识正常回答，不拒答
```

### 明确不做（本步）

- 检索后台并行（v1 同步，每次提问多 ~几百 ms）
- 改动 ask 工具的"查我的笔记、没查到就说没有"语义（保持不变）

---

## Step 3：记忆冲突解决（合并时以新内容为准 + 冲突报告）【详细实施计划】

### 动机

新学内容与旧笔记矛盾时（如框架新版本改了 API），旧 `merge_notes` 只做"相似合并"（拼正文），不识别矛盾——新旧对同一事实的冲突说法会被并排保留或随意融合，笔记自相矛盾。目标：合并落盘时让 LLM 识别相互矛盾的陈述，**默认以新内容为准**改写，并向用户**明确报告发现了什么矛盾、改了什么**。

### 已确认的设计决策

- **判定与合并解耦**：是否同一知识点仍走现有二态判定（标题 fast-path + LLM same/diff，`find_note_match` 不变）；**矛盾识别不放进去重判定**，后移到用户确认合并后的 `merge_notes`（本就是又一次 LLM 重写，顺带做矛盾处理，改动最小）。
- **矛盾处理策略**：合并时 LLM 对比新旧，存在对同一事实的相互矛盾陈述 → **默认以新内容为准**修正矛盾处（新内容是最新学习的），不保留矛盾旧说法；详略不同 / 新旧讲不同版本且不否定彼此 → 不算矛盾，正常补充。
- **报告机制**：`merge_notes` 返回 `{"content": 合并后正文, "report": 矛盾报告}`（无矛盾时 report 为空字符串）；`persist_points` 收集非空 report 进 `conflict_reports`，各展示点透出给用户。
- **明确不做**：不弹用户选择（无"保留旧笔记另立新篇"选项）；不做 archive 存档（合并保留不矛盾内容，旧笔记未被整体毁掉，无需存档）；不改去重判定 / note_pipeline 输出 / Web / config。
- **降级**：merge 输出 JSON 解析失败 → 整个输出当正文、无报告（与旧行为一致，安全侧）。

### 提示词（唯一新增/改动，已提交用户审核并确认）

合并提示词 `MERGE_SYSTEM_PROMPT` 增补"矛盾处理"一节 + 输出改为 JSON：`{"content": 合并后正文, "report": 矛盾报告}`；解析复用 `domain/extraction.py::parse_json_object`。

### 代码改动

- `pipelines/note.py`：
  - `MERGE_SYSTEM_PROMPT` 增补矛盾处理 + JSON 输出。
  - `merge_notes(old, new, topic)` 改为返回 `{"content", "report"}`（parse_json_object 解析；失败降级 content=raw、report=""）。
  - `persist_points` 合并分支解包 content 落盘、收集非空 report，返回加 `"conflict_reports"`。
- `graph.py` `note_confirm_node`：persist 后把 `conflict_reports` 追加进 summary / last_output。
- `pipelines/route.py` `_note_commit`：工具结果透出 `conflict_reports`（agent 转述用户）。
- `cli.py` `_run_note`（交互式 note 流）：persist 后打印 `conflict_reports`。
- 零改动：`adapters/store.py`、`adapters/llm.py`（去重判定）、Web、config、domain。

### 改动文件清单

- `src/pipelines/note.py`
- `src/graph.py`
- `src/pipelines/route.py`
- `src/cli.py`
- `tests/test_merge_conflict.py`（新）
- `eval_merge_conflict.py`（新，LLM 质量验证脚本，用户决定是否跑）

### 测试计划（零网络，monkeypatch generate_text / merge_notes）

- merge_notes 解析 JSON → 返回 {content, report} 正确
- 无矛盾（report 为空）→ report 空、content 正常
- 解析失败（输出纯 markdown / 非法 JSON）→ 降级 content=raw、report=""
- persist_points 合并候选 + merge_notes 报告 → 返回含 conflict_reports
- note_confirm_node / _note_commit / cli 透出 conflict_reports
- 既有回归：note / coach / sweep / qa / i1 / route_tools 全绿

### 手工验证（LLM 花费 + 人工看效果，交付用户决定）

1. **合成 eval**：`eval_merge_conflict.py` + 种子用例（真矛盾：端口变更 / 机制反转 / API 弃用；互补 / 重复 / 不同主题相关），跑 merge_notes 断言：矛盾对 report 非空且正文以新内容为准；非矛盾对 report 为空。
2. **真实流程**：`python -m src.cli route "Spring Boot"` 学习中沉淀一段与旧笔记矛盾的内容并确认合并，观察 CLI / coach 回复里出现矛盾报告。

### 明确不做（本步）

- 矛盾三选确认流（更新旧 / 保留另立 / 跳过）——v2 候选，见"边界与明确不做"
- archive 存档——合并保留不矛盾内容，无需
- 冲突的"AI 自动改"增强——当前"以新内容为准 + 报告"已是最简

---

## Step 4：摘要自我整理（三舱记忆）【详细实施计划】

### 动机

`coach_summary` 是单段文本，每次压缩把旧摘要 + 新消息整体交给 LLM 重写（≤200 字）。长期会话后：稳定事实（画像、决定、纠正）与临时上下文（未决问题、近期进展）混在一起承受同样的重写衰减率；"已解决"事项无淘汰机制越积越多；且摘要无机械上限（`COACH_SUMMARY_MAX_TOKENS=800` 是死配置，全项目无引用，唯一约束是提示词一句"200 字以内"）。

**核查发现的前提缺口**：原草案写"画像与路线本就在状态里独立注入"——路线确实注入，但**画像只在 survey/planning 提示词注入，coaching 模式（最长寿命的模式）没有注入**（`_coaching_prompt` 只拼 roadmap + summary + kb）。恰在最需要画像稳定性的地方，画像完全靠摘要"幸存"。

### 设计原则（从学习场景推导）

coach 长会话依赖的记忆按"怎么存才不坏"分三类：
- **稳定，绝不衰减**：画像补充、学习偏好、对陪练的纠正、重要决定——被重写掉等于陪练反复犯错；
- **易变，解决后必须淘汰**：用户未解决的问题、承诺要讲的主题、待确认事项——不淘汰则越积越多；
- **易变，允许衰减**：近期学习脉络——真相在外部（roadmap 注入位置、知识库沉淀内容），摘要只是叙事胶水。

**核心原则：LLM 每次压缩只看新消息产出增量；已积累的记忆永不再过 LLM 的手**——衰减在结构上归零，而非靠提示词求模型别忘。与项目既有哲学一致（Step 1 差量提取、note 游标）。

### 三舱状态

| 舱 | 字段 | 写入 | 淘汰 | 衰减 |
|---|---|---|---|---|
| 事实舱 | `coach_facts: list[str]` | 压缩时 LLM 提取 `facts_add`，确定性追加（strip 精确去重） | 超上限丢最旧；**永不被 LLM 重写** | 否 |
| 未决舱 | `coach_open_items: list[{id, text}]`（id 全局递增整数） | LLM 提取 `open_add` 追加 | **确定性淘汰**：LLM 输出 `resolved: [id...]` 判定，代码按 id 移除；超上限丢最旧 | 条目不改写，只增删 |
| 脉络舱 | `coach_summary: str`（≤150 字近期焦点） | LLM 顺带重写 `context` | 机械字符上限兜底 | 是（有外部真相兜底） |

### 压缩流程（每次压缩仍是 1 次 LLM 调用，成本不变）

```
coach_trim 触发压缩（>COACH_COMPRESS_AT 条消息）
  → 组装输入：现有 facts + 现有未决项（带 [id] 编号）+ 被裁掉的旧消息（user/assistant 文本）
  → 一次「记忆整理」LLM 调用 → JSON {facts_add, open_add, resolved, context}
  → 确定性应用：facts 去重追加 → open 追加（id = 现有最大 +1）→ 按 id 移除 resolved
    → facts/open 超上限丢最旧 → context 机械截断覆盖
  → LLM 失败 / JSON 解析失败 → 三舱原样保留，消息照常裁剪（宁可少记，不冒险）
```

淘汰从"重写时求模型自觉删"变为"模型只做判定（编号列表），删除由代码执行"（PROMPT_DESIGN 原则 1 的应用）。

### 注入（_coaching_prompt）

```
用户画像：{profile_summary}          ← 新增，修复 coaching 不注入画像的缺口
已确认的事实与偏好：
- ...
未决事项（待跟进，解决后移除）：
- [2] ...
此前对话摘要：{context}
```

空舱不渲染对应块；planning 不动（问卷刚结束、寿命极短）。

### 提示词（CONSOLIDATE_MEMORY_PROMPT，已提交用户审核并确认）

见 `pipelines/route.py`；要点：JSON 四字段输出、facts 只记新信息（与现有重复不输出）、resolved 输出编号由代码删、context 不复述画像/决定（有独立位置）、正反例齐备。解析复用 `domain/extraction.py::parse_json_object`。

### 代码改动

- `config.py`：`COACH_FACTS_MAX=20`、`COACH_OPEN_MAX=8`、`COACH_SUMMARY_MAX_CHARS=600`；删除死配置 `COACH_SUMMARY_MAX_TOKENS`（无引用，安全替换）。
- `pipelines/route.py`：新提示词 + `consolidate_memory(existing, messages, tech) -> {"facts","open_items","summary"}`（增量提取 + 确定性应用，纯函数）；`_coaching_prompt` 注入三块 + 画像；删除旧 `summarize_conversation` / `SUMMARIZE_COACH_PROMPT`。
- `graph.py`：LearnState 加 `coach_facts` / `coach_open_items`；coach_trim 压缩分支改调 `consolidate_memory` 写三舱。
- 零改动：Web / CLI（只读 conversation/last_output，不碰 coach_summary）。

### 改动文件清单

- `src/config.py`
- `src/pipelines/route.py`
- `src/graph.py`
- `tests/test_memory_consolidation.py`（新）
- `tests/test_coach_loop.py`（trim 测试适配）

### 测试计划（零网络，monkeypatch generate_text）

- 增量应用：facts 追加+精确去重 / open 追加 id 连续 / resolved 按 id 移除（含不存在 id 忽略）/ context 覆盖
- 机械上限：facts 超限丢最旧 / open 超限丢最旧 / context 超长截断 / context 空保留旧摘要
- 降级：LLM 异常、JSON 解析失败 → 三舱原样；空消息不调 LLM
- 注入：coaching 提示词含画像行（缺口修复断言）/ facts 块 / 未决块；空舱不含
- coach_trim：压缩触发一次整理、三舱写入、消息裁剪到 N 轮
- 既有回归：coach_loop / memory_sweep / kb_retrieve / qa / i1 / route_tools 全绿

### 手工验证（LLM 花费 + 人工看效果，交付用户决定）

`python -m src.cli route "Spring Boot"` 连续对话 20+ 轮触发压缩，观察：
1. 压缩后 agent 仍记得早前的画像/偏好/纠正（事实舱幸存）；
2. 已回答的疑问不再被反复提起（未决项被淘汰）；
3. 提示词注入可见三舱块（可临时打印验证）。

### 明确不做（本步）

- 事实舱的 LLM 合并去重（v1 精确去重+上限即可，同义重复留 v2）
- 跨会话继承事实/未决舱（Step 5；三舱结构为其预留了更好的继承素材）
- survey/planning 模式的舱注入

---

## Step 5：跨会话记忆【设计草案】

### 动机与现状

- `profile.json` **只写不读**：画像落盘后从未读回上下文（学 Java 得出的"小白、每天 2 小时"不会带到学 Python）。
- 摘要与路线**在会话线程内**：重新规划开新线程不继承（coach_summary 丢失、路线需重新问卷生成）。

### 设计（三小块）

**a) 画像读回（同 tech + 跨 tech）**
- 新线程初始化时读 `profile.json` 该 tech 档案：已答字段预填 `survey_answers`（问卷跳过已答字段，只问缺的 + 诊断题），画像直接注入提示词。
- 跨 tech：profile.json 增加聚合条目（bucket / 时间预算 / 目标偏好取各 tech 聚合或最近值）；新 tech 启动时预填为**可改的默认值**，问卷里让用户确认一次（防旧画像误导）。

**b) 摘要继承（同 tech）**
- profile 档案字段加 `last_summary`：每次会话压缩 / 结束时写回；同 tech 新线程加载为 `coach_summary` 初值。

**c) 路线继承（同 tech 重新规划）**
- 重新规划新线程时把 `roadmaps/<tech>.json` 载入 state（路线 + 当前阶段 + 里程碑进度保留），**跳过重新问卷**，直接进入"确认 / 修订现有路线"环节（用户可改或重生成）。
- CLI 入口的"继续 / 重新规划"语义细化：继续 = 同线程；重新规划 = 新线程但继承画像 + 摘要 + 路线。

### 改动面

- `adapters/learner.py`——profile 读取接口（目前只写）+ `_global` 聚合 + `last_summary` 读写
- `graph.py` / `cli.py`——新线程初始化加载（画像预填 / 摘要初值 / 路线载入）
- 单测：画像预填问卷 / `_global` 聚合 / last_summary 写回与加载 / 新线程路线载入直接进入确认环节
- 复用现有文件位置（LEARNER_DIR / ROADMAP_DIR），无新目录

---

## 边界与明确不做

- **选择性遗忘 / 复习调度**：进阶能力，未排期（知识库只增不减是有意为之，人工可控）
- **跨 tech 的摘要继承**：v1 只做同 tech；`_global` 只聚合画像，不聚合摘要
- **冲突三选确认流**（更新旧 / 保留另立 / 跳过）：v1 走"合并时以新内容为准 + 报告"，不弹选择；三选留作 v2 候选
- **并行沉淀**：v2 增强，见 Step 1

## 提示词审核要求

Step 1 完全复用现有提取提示词（无新提示词）；Step 2（coaching 提示词注入 kb_context 块）、Step 3（merge 提示词增补矛盾处理 + JSON 输出）与 Step 4（CONSOLIDATE_MEMORY_PROMPT 记忆整理）的新提示词 / 改动，按 PROMPT_DESIGN 硬约束，**落地前提交用户审核**。

---

## 实施状态记录（2026-08-29）

### Step 1 v2 并行沉淀（已实现，问题 2 已修，问题 1/3 待真实环境验证）

> 本节是 2026-08-29 的状态快照。文中「尚未在真实环境完全验证」指的是**当时**尚未复验，不代表当前状态。

- **问题 2（后台失败/重启触发同步兜底 → 阻塞对话 + note SSE 流）**：✅ 已修复。排水阶段把"同步兜底重跑"改为"**快照并回 buffer，未来正常 fire 重扫**"（不重跑、不阻塞、无 SSE）。根因：原设计在排水分支同步重跑 `run_memory_sweep` 且带 `_get_progress()`，2-5s（实际可能几十秒）阻塞主线并弹 note 进度。
- **问题 1（自动沉淀无用户反馈）**：后端已加确定性 SSE 进度事件 `_emit_sweep_feedback("🗂️ 已自动沉淀 N 个")`（不经过 agent）。前端根因：`chat.js::showCoachReplyInput` 用 `el.innerHTML=` **整块替换 runStatus 面板**，把刚追加的进度行（含"已自动沉淀"）瞬间抹掉。**已修复**为保留进度行、输入框追加在下方。⚠️ **尚未在真实环境完全验证**（需强刷加载新 JS）。
- **问题 3（相似候选呈现可靠性）**：已实现确定性确认节点 `coach_candidate_confirm`（interrupt 用户 → `parse_merge_decision` → `persist_points`，**完全不经过 agent**），时机 A（排水后、agent 回复当前问题前）。⚠️ **尚未在真实环境完全验证**。
- **耗时认知修正**：note 提取可能耗时几十秒（非 2-5 秒）→ `ROUTE_MEMORY_SWEEP_TIMEOUT` 60 → **300**。
- **根因补遗（重要）**：用户持续看到的"note 正在执行同步阻塞"真正的元凶是 **agent 的 note 工具**（`route.py::_note`，同步跑 `note_pipeline` + `ctx.progress` SSE）——它不是 sweep 流，前三个修复都不覆盖它。**已修复**：coaching 工具集移除 `NOTE_SCHEMA/NOTE_COMMIT_SCHEMA`，coaching 提示词同步去掉 note 并注明"学习内容自动沉淀，无需调用 note"；删除死测试 `test_coaching_note_merge_confirm_end_to_end`。`_note/_note_commit` 实现保留在 `_TOOL_IMPL`（未被引用，可后续清理）。
- **部署注意**：后端改动（graph.py/route.py/config.py）需**重启 `python -m src.web.server`** 才生效；浏览器仅强刷 JS 不够。之前"一个都没起效"部分原因是未重启服务。
- **卡死 pending 实锤（重要）**：用户真实会话（`learn-会话B`）checkpoint 里发现**旧 note 工具流遗留的 `coach_note_pending`（无 `_auto`）**，它同时阻塞 buffer 积累（coach_human 的 `not coach_note_pending` 条件）与 sweep fire（memory_write 的 pending 检查）→ sweep 彻底不执行。根因：旧 pending 只靠 agent 的 note_commit 解决，note 工具移除后永久卡死。**已修复**：`_route_after_memory_write` 改为**任何 pending 都路由到确定性确认节点**（不再要求 `_auto`），下一条用户消息即弹出候选由用户拍板解决。已验证：注入 legacy pending → 候选确认弹出 → skip 解决 → sweep 恢复。

待验证事项（真实环境，重启服务后回来继续）：
1. 触发沉淀回合 agent 回复不再空等（sweep 后台线程）；
2. 对话中不再出现"note 正在执行"同步阻塞（note 工具已移除）；
3. Web 端"已自动沉淀 N 个"进度行可见（强刷后）；
4. 相似候选确定弹出确认（不经过 agent 转述）。

