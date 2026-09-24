# 优化日志（Optimization Log）

> 本文件记录 Tech Learner Agent 功能的**体验优化**历史，**追加式维护**：后续优化统一在本文件追加新条目，不覆盖旧条目。报告/评估类文档（含结论数据）仍按 `docs/report/` 的版本文件规范维护。
>
> 每条记录结构：**问题 → 改动 → 效果**。日期格式：`YYYY-MM-DD`。

---

## 2026-09-01 · route 问卷与陪练系列优化

基于实际体验 route 功能发现的一批问题，逐项改进，均含零网络单测并通过全量回归（278 passed）。

### 1. 学习目标从「二选一」改为「自由文本」

**问题**：问卷「学习目标」被设计成强制二选一的枚举（`min_project` / `deep`），提问、解析、画像渲染三处锁死。用户回答「基本看懂项目代码逻辑」这类真实目标两头不沾，被报错打回重选。而下游 `generate_roadmap` 的 goal 本来就是自由文本，枚举反而丢失了个性化信息。

**改动**：
- `src/domain/survey.py` — goal 改为自由文本：开放式提问（「学完后你想做到什么」），解析只校验非空，**原样保存用户原话**；画像摘要渲染目标原文，旧枚举值（`min_project`/`deep`）兼容映射回可读文案；
- `src/pipelines/route.py` — 告知模型的答案格式从「二选一」改为「自由文本，原样记录用户原话」。

**效果**：个性化目标直接注入 planning / coaching 提示词，路线更贴合用户诉求。

### 2. 诊断题对错判定：强制单选选择题 + 代码确定性判定

**问题**：问卷的动态诊断题只收集回答原文，**题目不保存、无对错判定**，且诊断数据不进画像摘要、落盘时被剔除（`route.py` 落画像时 `if k != "diagnostics"`）——planning/coaching 完全不知道用户实际水平，路线只能靠自评 + 相关技术 + 目标猜。

**关键洞察（用户提出）**：模型出题时本来就「知道答案」——与其让 LLM 再判一次对错，不如强制**单选选择题 + 内嵌标准答案**，由代码确定性比对。

**改动**：
- `src/domain/survey.py` — 新增纯函数 `extract_diag_answer`（剥离 `【答案】X` 标记，返回题目+标准答案）、`parse_diag_choice`（提取用户回复的选项字母）；`profile_summary` 渲染「诊断自测：N题中 X对 Y错」；兼容旧 `[str]` 诊断数据；
- `src/pipelines/route.py` — 出题提示词强制单选选择题（4 选项、唯一正确答案）、末尾内嵌 `【答案】X`、引导用户回选项字母；
- `src/graph.py` — `coach_human` 展示前剥离答案行（用户看不到答案）；`coach_survey` 采集题目+回答，比对选项字母**代码确定性判 right/wrong 二值**（零额外 LLM 调用）。

**效果**：判定 100% 确定、可审计；自评高分但诊断全错 → 路线偏基础，自评低分但诊断全对 → 适当提速。模型没按格式带答案 / 用户没回字母 → 降级「无法判定」，不误判。

### 3. coaching 阶段 Agent 可改路线：revise_roadmap + 进度保留

**问题**：陪练阶段 Agent 改不了路线——coaching 工具集没有 `generate_roadmap`，`update_roadmap` 只能勾/取消里程碑；手动改 `roadmaps/*.md` 不生效（Agent 读的是 checkpointer 状态）；中途想大改只能退出 + `route --resume` 重走问卷。

**关键坑（实现前发现）**：`build_roadmap` 总是全量重建（里程碑全 `done=False`、`current_stage` 回 s1），直接复用会把已勾选进度清零。

**改动**：
- `src/domain/roadmap.py` — 新增纯函数 `merge_progress(old, new)`：按里程碑描述（desc）完全匹配，把旧路线已勾选进度合并进新路线，`current_stage` 校正到第一个未完成阶段（全完成 → `completed`）；
- `src/pipelines/route.py` — 新增工具 `revise_roadmap`（参数与 `generate_roadmap` 同构），构建后走 `merge_progress` 保进度，落盘并返回 `kept_done`；加入 coaching 工具集；提示词约束「用户要求改路线时先确认改动点再调用，改完呈现新路线」。

**效果**：陪练中说「跳过第二阶段、时长减半」等，Agent 能真正修订路线且已勾选进度不丢。

---

### 4. 里程碑推进把关：核对真实完成 + 用户确认后才推进

**问题**：陪练中 Agent 常在一个里程碑尚未真正完成时就自动推进到下一个，且**未经用户同意**——提示词只写「里程碑完成时用 update_roadmap 勾选」，无任何里程碑边界约束。

**改动**：
- `src/domain/exit_intent.py` — 新增 `is_advance_directive`：确定性判定用户上一条回复是否为「直接进入下一阶段 / 直接推进 / 不用再问」类明确指令（要求 4 豁免）；
- `src/pipelines/route.py` — `_coaching_prompt` 新增「里程碑推进规则」（勾选前逐项核对待办真实完成；勾选后停下总结 + 询问；用户上轮明确指令则免确认）；`_update_roadmap` 勾选后设 `coach_milestone_pending` 卡点（除非用户上轮已明确推进），note 要求总结询问；
- `src/graph.py` — 新增 `coach_milestone_pending` 状态字段；`coach_human` 用户回复后清空（一次性闸门）。

**效果**：勾选里程碑后必然停下总结 + 询问，用户明确同意前不推进；用户说「直接进入下一阶段」则免确认直接推进；里程碑待办未完成时 Agent 先补做再勾选。

**设计取舍**：确认与否由 Agent 理解用户回复（LLM 擅长自然语言确认），状态标记只保证「勾选后必须停下问一次」；`current_stage` 状态字段的自动推进保持不变（把关的是教学行为，不是状态字段）。

### 5. 里程碑验收闸门：evidence 双层校验 + 批量勾选护栏

**问题（第 4 条的提示词约束被实测证伪）**：Agent 布置任务后用户说「你来回答」，Agent 既没回答、无任何实质产出，直接把**所有**里程碑勾满并宣布「学习路线全部完成」——两个失效点：待办内容从未出现在对话里却勾选；一轮内批量勾选架空了确认闸门。结论：验收不能靠 Agent 自觉，必须代码强制。

**改动**（业界模式：LLM-as-Judge 升级为 evidence-grounded judge，引用归因式双层校验）：
- `src/config.py` — 新增 `ROUTE_MILESTONE_VERIFY`（默认开）/ `ROUTE_VERIFY_TRANSCRIPT_CHARS`（送审对话尾部上限，默认 6000）；
- `src/domain/exit_intent.py` — 新增 `is_completion_claim`：用户明确声明完成（对话外完成的里程碑，如「本地跑通了」）→ 验收豁免；否定/疑问守卫防「还没搞定」「搞定了没？」误判；
- `src/pipelines/route.py` — `update_roadmap` 勾选（done=true）新增 `evidence` 参数（对话原文证据引用），勾选走四道闸：
  1. **批量护栏**（确定性）：已有待确认里程碑（`coach_milestone_pending`）→ 拒绝再勾，一轮只走一个「勾选→确认」循环；
  2. **豁免**（确定性）：用户完成声明 / `ROUTE_MILESTONE_VERIFY` 关闭 → 跳过验收；
  3. **引用校验**（确定性、零成本）：evidence 逐行与对话记录（无界 conversation 尾部）做空白归一 substring 比对，编造证据在此拦死；
  4. **LLM 验收**（语义）：独立验收调用判「证据是否实质覆盖里程碑」——宣布完成/布置未做/任务转交无回应都不算证据；未覆盖 → 拒绝勾选并回喂缺失项。验收器故障降级放行（不卡死流程）。
  另：planning 提示词要求里程碑尽量写成对话内可检验的形式；coaching 提示词同步勾选须附证据。

**效果**：对照事故案例——待办「回答三大框架选型问题」没回答就勾选：闸 3/4 拒绝（对话里只有布置与转交，无实质回答），Agent 必须真的回答后才能勾；一口气勾满全部：闸 1 在第二个勾选拒绝。

### 6. 验收闸门卡死修复：重试节流 + 反馈纠偏（真实会话事故）

**事故（真实会话 learn-会话D，2026-09-01）**：第 5 条验收闸门上线后，Agent 为勾选 `s2-m1` 连续调用 `update_roadmap` 8 次全被拒——先是给引用加「用户原文：」前缀导致逐字比对误判编造；被拒后它**在 evidence 参数里现写对比内容当证据**（从未作为回复发到对话里），继续被拒，烧光回合工具预算 →「本轮工具调用次数已达上限」。下一回合用户说「写选型结论」，Agent 仍不回答、继续闷头勾选，再次烧光预算 → 第二次「已达上限」。用户的问题始终没被回答。

**根因（4 个，均有会话记录实证）**：
1. 拒绝反馈没点破「evidence 参数里现写的内容不算证据」，Agent 把现写当「已完成」；
2. 拒绝不设节流，换措辞重试可无限烧预算（绕过重复签名检测）；
3. 引用比对太死板（前缀/引号包装即判编造）；
4. 连带发现状态 bug：勾满标 completed 后取消勾选，status 不回退（提示词一直显示「✅ 已完成」但里程碑未勾满，误导 Agent）。

**修复**：
- `src/pipelines/route.py` — 拒绝节流（确定性）：同回合被拒 ≥2 次封锁 `update_roadmap`（`status=blocked` + 明确指令「停止调用，直接把缺失内容回复给用户」），剩余预算留给回答用户；拒绝反馈改写并加 `action: teach_then_recheck`（点破「参数里现写不算，必须发到对话里」）；引用比对剥装饰性包装（前缀「用户原文：」/外层引号）；
- `src/graph.py` — 新增 `coach_verify_rejects` 状态（coach_human 每回合清零，节流只限本回合）；
- `src/domain/roadmap.py` — `complete_milestone` 取消勾选时若 status=completed → 回退 active + 当前阶段指回第一个未完成阶段（修状态不一致）。

**效果**：同一路径重放——最多浪费 2 次工具调用就被确定性封锁，Agent 转向直接回答用户；真实引用不被误判编造；completed 状态与勾选进度保持一致。

### 7. 验收简化：去掉 evidence 参数，验证员直读对话记录

**问题（真实会话）**：evidence 逐字比对把「引用管理」变成必经环节——Agent 的回答带 Markdown 格式，模型引用时天然「意引」不去格式符号 → 字面比对误判 → Agent 得出「内容必须重新正式输出」的结论 → 重输出全文 + 等下一轮再勾，一次「回答→认可→勾选」被拆成四个回合（用户反馈「太死板」）。且 evidence 参数本身是注入通道（第 6 条事故里模型在参数里现写内容当证据）。

**改动**：
- `src/pipelines/route.py` — 删除 evidence 参数与字面比对层（`_evidence_missing`/`_quote_variants`）；验收提示词改为直读对话版（验收员只看「里程碑标准 + 对话记录」，判定规则不变：宣布完成/布置未做/转交无回应都不算）；`verify_milestone(milestone_desc, transcript)`；
- coaching 提示词规则更新：「勾选由系统自动核对对话记录，不要为凑证据重复输出已讲过的内容」。

**防伪分析（为什么不回到纯提示词老路）**：与最初失败的自查方案的本质区别在于①判断者分离（独立验收调用，无上下文偏置）②强制点在代码（拒绝即不落盘，模型无法绕过）③判断依据只有对话事实（意图不在输入里）。去掉 evidence 后模型反而**失去注入通道**——想通过验收唯一途径是真把内容讲进对话。代价：失去零成本确定性预筛，每次勾选都花一次验收调用（勾选低频 + 节流闸兜底，可接受）。

**效果**：回答 → 用户认可 → 直接勾选（验收员从对话记录看到完整内容）→ 一个回合完成，重输出环节消失。

---

## 2026-09-11 · 工具调用通道：错误分档 + 退避预算 + 降级可见性

起因：`docs/coach_trim-tool-accident.md` 复盘里点出两项「待单独决策」的行为变更（重试掩盖错误性质、提示语指向错误方向）；复查时又发现降级路径本身还有三处问题。

### 1. 重试分档：按「重试能不能改变结果」决定，而不是一律重试 3 次

**问题**：`chat_with_tools` 用 `for attempt in range(3)` + `except Exception` 兜住一切，四类失败被混成一种：

- **叠乘**：SDK 客户端默认 `max_retries=2`（对连接错误 / 408 / 409 / 429 / 5xx 退避重试），外层再套 3 次 → 最坏 3×3 = 9 次请求；且 SDK 默认超时 600s，极端情况下要几十分钟才走到降级调用（web 层 `runner.py` 对 coach 循环没有超时兜底）。
- **确定性错误白重试**：401（key 失效）/ 404（模型名错）/ 400（请求体不合法）重试一百次也不会有不同结果，却照样重试 3 次 + 降级 1 次 = 白烧 4 次调用。
- **本地解析异常也重试**：`_parse_chat_response` 在 try 内，响应结构异常（本地 bug）被当成瞬时故障重试。
- **提示语误导**：所有失败最终都包装成同一句「模型调用暂时不可用，请稍后再试」——key 失效的用户会一直「稍后再试」下去。

**改动**：
- `src/config.py` — 新增 `LLM_REQUEST_TIMEOUT`（默认 45s）、`LLM_MAX_ATTEMPTS`（3）、`LLM_RETRY_BUDGET_SECONDS`（90）、`LLM_RETRY_BASE_DELAY`（1.0）；
- `src/adapters/llm.py` — `_get_client()` 进程内单例（复用连接池）且 `max_retries=0`（关掉 SDK 内置重试，避免与外侧叠乘）、显式 `timeout`；新增 `_classify` 三档分流（`retry` 瞬时原样重试 / `degrade` 本请求没救但去掉 tools 可能成 / `abort` 两类都不做）、`_backoff_delay`（指数 + 抖动）、`deadline` 总预算（超预算立即降级，每次请求的 timeout 也按剩余预算收紧）；`ToolCallError` 增加 `fatal` 标记。

**效果**：瞬时错误退避 1s→2s 重试、总耗时封顶 ≈ 预算 + 一次请求超时；确定性错误 1 次调用即失败（原先 4 次），文案带上 HTTP 码与具体原因（`_fatal_message`：401→API key 无效 / 404→模型名不存在 / 400→请求被拒绝（参数或会话历史不合法））；`graph.coach_llm` 对 fatal 错误提示「重试无用」而非「稍后再试」。代码 bug（非 SDK 异常）原样抛出显形，不再被包装成「模型暂时不可用」——真 bug 不该伪装成网络抖动。

### 2. 降级可见：用户看得见，模型也知道自己没有工具

**问题**：`parsed["fallback"] = True` 在 `src/` 全目录无任何读取方（docstring 声称「调用方可感知降级」，实际没接上）；降级请求只 `pop("tools")`、system 提示词原封不动——提示词里仍写着「你可以用工具推进学习：collect / read / update_roadmap…」，模型不知道自己没有工具了，最可能的输出是「好的，我已经为你生成了路线」这类**叙述式假成功**：用户看到的是一条正常回复，实际路线 / 进度 / 笔记什么都没变。

**改动**：
- `src/adapters/llm.py` — 新增 `FALLBACK_SYSTEM_SUFFIX`：降级请求的系统提示词显式声明「本次工具通道不可用、无法调用任何工具、严禁声称已完成需工具的动作」；
- `src/graph.py` — `coach_llm` 读 `result["fallback"]`，命中则在用户可见回复前挂 `COACH_DEGRADED_NOTICE`（「本次工具通道不可用，未执行任何操作」）。

**效果**：降级不再是静默行为——用户当场知道这一轮没执行任何工具动作，模型也不会顺着提示词编造「已完成」。

**测试**：`tests/test_llm_chat_tools.py`（重写，12 个用例：分档 / 退避 / 预算截断 / 降级提示词 / bug 透传，用替身 time 模块驱动预算，零真实等待）、`tests/test_coach_loop.py`（新增 3 个用例：降级提示前缀 / fatal 与瞬时错误文案分流）。全量回归 328 passed。

**未做（第二步）**：`generate_text` 的重试与降级契约对齐——它目前一次重试都没有，而 dedup 判定 / `consolidate_memory` / `verify_milestone` / collect·read·note·qa 全走它，同样的网络抖动在两条通道的语义完全不同（一边报错、一边在调用点静默降级成「不合并 / 跳过验收」）。涉及既有降级语义，单独决策。

---

## 2026-09-13 · LLM 调用可观测性 + 报告通道超时（复盘 P0 落地）

承接 `llm-timeout-and-truncation-accident.md`（collect 静默 22 分钟 + 报告被硬截断）的 P0 待办，
顺带回答一个更基本的问题：**配置里的超时数字，是不是拍的？**

### 1. 超时定值改为实测驱动（对话 45s 保留、报告新增 120s）

**问题**：对话通道的 45s 是先拍的（原始动机只是"SDK 默认 600s 太长"）；报告通道（`generate_text`）
则**完全没有业务超时**，SDK 默认 600s + 自带 2 次重试——正是那次 15 分 55 秒静默的根因。

**改动**：在真实网关（`deepseek-flash` / 腾讯 MaaS）打 5 次计时调用，量出各形态的真实尾延迟
（数据表见事故复盘附录 C）：极短输入 2.2s / 真 coach 提示词 3.8s / 需求长输出 22.8s（`finish_reason=length`，
completion 恰好 4096）/ 报告形态（3912 prompt + 3311 completion）18.0s / 降级形态 1.2s。
据此：对话通道保留 45s（最坏合法 23s ≈ 2× 余量），报告通道新增 `LLM_REPORT_TIMEOUT=120s`
（4~6× 余量，因为报告失败会让整轮 collect 白做）。

**效果**：数字从"拍的"变成"有依据、且依据写进注释"——包括**换模型必须重测**这条警告
（事故形态正是"模型换了、数字没换"，缺陷在代码里、触发条件在模型侧）。
实测同时**证伪了我的一个假设**：原以为 45s 可能过紧、打算提到 90~120s，数据不支持（当前网关
180 tok/s，满输出仅 23s），故未改。

### 2. `generate_text` 显式超时 + 收紧 SDK 重试

**问题**：报告生成是裸客户端——无 `timeout`、每次调用新建 `OpenAI()`（不复用连接池）、
SDK 默认 `max_retries=2` 会在触到超时后再重发一遍（耗时翻倍、计费翻倍）。

**改动**：改走与对话通道同一个 `_get_client()` 单例（`max_retries=0`），显式传
`timeout=config.LLM_REPORT_TIMEOUT`，**单次不重试**（长任务重发等于再烧一份 token 再等一轮）。

**效果**：600s 静默 → 120s 内失败并交由调用方降级；SDK 重试放大与重复建客户端的开销一并消除。

### 3. LLM 调用埋点（事故定位成本的主要来源）

**问题**：`finish_reason` / `usage` / 耗时 / 重试与降级路径**全部被丢弃**，全仓零 logging。
后果是那次事故只能靠翻 checkpoint 数据库 + 看文件断口反推，"截断 = `length`""慢 = 走到 600s"
这两条最关键的实锤恰恰拿不到。

**改动**：`llm.py::_log_call` —— 一次尝试一行 JSON，字段 `site` / `attempt` / 耗时 /
`status` / `kind` / HTTP 码 / `finish_reason` / prompt·completion token / `fallback`；
重试几次就几行，降级那次用 `attempt=0 + fallback=true` 标记。默认开、不设开关（避免重演
"日志没开所以又查不到"）。handler 在**导入时**挂一次，不用惰性挂载——后台沉淀线程与主线程
可能同时首调，惰性挂载的「检查-再挂载」不是原子的，会挂出两个 handler 让每行重复。
**只记元数据，提示词与正文不进日志**（学的是用户自己的东西）。10 个调用点带 `call_site`：
collect.report / read.report / read.classify / note.extract·suggest·merge / qa.answer /
dedup.judge / verify.milestone / coach.consolidate / coach.chat。

**效果**：真实调用已验证输出（示例）：

```json
{"event": "llm_call", "site": "collect.report", "attempt": 1, "elapsed_s": 1.8, "status": "ok",
 "kind": "ok", "http": null, "finish_reason": "stop", "prompt_tokens": 43,
 "completion_tokens": 15, "fallback": false, "error": ""}
```

"截断"与"慢"从推断变成可直接读的字段；复盘 P0 的两条验收信号（能看到 `finish_reason` 与耗时、
120s 内失败而非静默 10 分钟）已达成。

### 4. 顺带验证：降级请求的网关兼容性

**问题**：降级请求保留含 `role: tool` 回执的消息历史、却去掉 `tools` 参数——担心部分兼容网关
会拒这种组合（若拒，整条降级路径就是坏的，而单测用的是假客户端验不出来）。

**验证**：真实网关实测（附录 C 的 10-c）——**被正常接受**，返回纯文本，担心不成立。

**测试**：`tests/test_llm_chat_tools.py` 扩到 20 个用例（新增：报告通道独立超时、单次不重试、
埋点字段、重试/降级逐次记账、**日志不含提示词与正文**）；各测试文件里 `generate_text` /
`chat_with_tools` 的假函数改为容忍新关键字参数。全量 335 passed / `ruff` 通过。

### 5. 报告 token 预算与截断标注（P0③）

**问题**：整篇报告与对话回复共用 `LLM_MAX_TOKENS=4096`。实测 55 篇真实报告：token 中位数
~1.8K、p90 ~3.5K、**最大 ~5.0K**，被截断的长尾正好落在超限那批里。

**改动**：
- `src/config.py` — 新增 `REPORT_MAX_TOKENS=8000`，注释写明与 `LLM_REPORT_TIMEOUT` 的联立关系
  （最坏耗时 ≈ 本值 ÷ 实测吞吐，8000 token 在 184 tok/s 下约 43s，对 120s 超时留 2.8×）；
- `src/adapters/llm.py` — `generate_text` 增加 `max_tokens` 覆盖参数与 `truncation_notice`；
  被截断（`finish_reason == "length"`）时追加显式标注，并打一条 **WARNING** 级
  `llm_truncated` 事件（截断是数据质量事件，不该混在常规 INFO 里）；
- `src/pipelines/collect.py` / `read.py` — 两个报告调用点传预算与标注；两份报告提示词加
  「报告不要太过冗长，字数控制在 800~2000 汉字」。

**效果**：残篇不再静默。此前被截断的报告会**当作正常产物落盘**，随后被同轮 read / ask 读进
解读报告与知识库——事后分不清"原文没有"还是"被切掉了"；现在正文末尾带
`> ⚠️ **本报告未写完**：生成时达到长度上限被截断…`，日志里有 WARNING 可统计。

**字数约束的实测反驳（重要）**：A/B 对比（同一输入，唯一差别是那行字数约束）——

| 输入规模 | A 无约束 | B 有约束 |
|---|---|---|
| 1.9K prompt token | 707 汉字 / 1083 token | 922 汉字 / 1841 token |
| 7.6K prompt token（近真实 collect） | 861 汉字 / 2201 token | 882 汉字 / 1962 token |

**在接近真实规模的输入上，这行约束没有可测效果**——模型本来就落在 800~2000 汉字区间内。
我原本的说法（"模板自由度高 → 模型放开写 → 顶到上限"）在当前模型上不成立：给 1.3 万字符
输入，它也只在 ~880 汉字处收尾。保留这行只是对"换更啰嗦的模型"的廉价保险，真正兜底的是
预算与标注。**这个数字口径本身也踩过坑**：最初把 `len()` 字符数当成"字数"，而汉字只占字符数的
约 1/3，两者差 3 倍——已在复盘附录 C.2 记下。

**验证**：`max_tokens=30` 强制触发截断 → 日志 `finish_reason: "length"` + WARNING
`llm_truncated(annotated=true)`，正文带标注。测试新增 6 个用例（预算覆盖、默认不变、
截断标注、判定类调用点不加标注、stop 不加标注），并在 `tests/test_collect.py` 补了**管道接线**
用例（该层原先无覆盖：预算或标注没接上不会报错，只会静默退回 4096 被截断）。
全量 341 passed / `ruff` 通过。

### 6. 编排层：失败不再重跑、贵工具单轮上限、工具级进度（P1④ + P3⑨）

上面 1~5 修的是**单次调用**的边界（超时、预算、埋点）。这一项修的是**事故里真正的放大器**——
事故当时的 22 分钟静默，单次调用修完也没消除：串行执行 + 攒齐才提交 + 全程零反馈，三者叠加。

**问题**：
- **贵工具失败后会被重跑**：`_collect` 把异常包成 `{"status":"error"}` 回喂模型，没有"别重试"的
  指引，模型大概率再调一次 collect —— 而重跑 = 重新搜索 + 重新抓取 + 重新生成（烧 Tavily /
  Firecrawl 额度 + 分钟级耗时）。既有重复检测护栏要「连续两次完全相同的签名」才触发，也就是
  第 3 次才拦，且参数稍变就永不触发。
- **总预算不等于贵工具预算**：`ROUTE_MAX_TOOL_CALLS_PER_TURN=8` 是**所有**工具的总数，模型一轮
  可以连发 8 个 collect（每次约 3.75 分钟 → 最坏半小时）。
- **工具级进度缺失**：进度停在「🧠 LLM 生成」之后，到工具返回前全黑（事故里 22 分钟）。
  而且这套进度**只有 Web 有**——CLI 的 route 路径注册表为空，全程静默。

**改动**：
1. **超时不重试也不降级**（`llm._classify` 单列 `_TIMEOUT`）：实测最坏合法生成 ≈23s，45s 超时是它的
   2 倍，更像故障而不是"生成得慢"；重发（含降级再发）等于把整条 prompt 再烧一遍、再让用户等一遍。
   真实网关验证：把超时压到 0.5s 强制触发 → 日志只有 1 条 `llm_call(kind=timeout)`、无降级请求、
   抛非 fatal 的 `ToolCallError`。
2. **阶段标签 + 失败闸**：`collect.py::CollectStageError(stage)` / `read.py::ReadStageError(stage)`
   给失败打阶段标签（search / fetch / generate；read 为 classify / generate）；`route.py`
   新增 `_heavy_blocked` / `_record_heavy_failure` 与状态字段 `coach_heavy_failures`（每回合清零）——

   | 情形 | 决策 |
   |---|---|
   | 参数错误（缺 tech/url） | 放行（不记失败，模型补参数再调是正当的） |
   | 搜索段失败（首次） | 放行一次（早期失败，流水线还没烧抓取与生成） |
   | 生成段失败 / 第二次搜索段失败 | **拒绝**（重跑要把搜索与抓取全部重演） |
   | 搜索段鉴权 / 额度用尽 | 拒绝（必然同样失败，且属配置问题） |

3. **贵工具单轮上限** `ROUTE_MAX_HEAVY_TOOLS_PER_TURN=2`（`HEAVY_TOOLS = {collect, read}`；
   ask/note 不烧外部额度不算贵）。超限**不硬拒**，改为停下问用户先做哪个——两个不同主题是
   合法需求（事故当天模型要的正是 skills + plugins 两个主题，它没错）。
4. **工具级进度 + CLI 接线**：每个工具前后各一条（`⚙️ collect 执行中...` / `✅ collect 耗时 212s`，
   失败为 `⚠️`）；`cli.py::_drive` 用同一个 `web_progress` 注册表把 `console.print` 接上，
   CLI 的 route 从此也能看到进度（`tests/test_cli.py` 守住这条接线）。
5. **资料不足信号**：`fetch_many` 设计成绝不抛错（单页失败记空），所以"5 页全没抓到"原本是
   **完全静默**的——模型会拿只剩搜索摘要的输入生成一份看起来正常的报告。现在"抓取全失败"与
   "搜索零结果"都会在提示词里显式说明，并在返回值带 `resource_ok=False` 透给模型。

**效果**：一次贵的失败从"整条流水线重演"变成"不重跑 + 如实告诉用户稍后再试"；一轮最多 2 个
贵工具；长工具期间用户每隔几分钟就能看到进展（Web 与 CLI 都有）。**注意**：`coach_tool` 的
串行执行与「攒齐才提交」仍未动（属有意取舍，要动得连 `ctx.updates` 竞态一起评估）——所以
"多个贵工具依次执行"的时长下限没变，变的是**它不再是黑箱**。

**测试**：新增 14 个用例（超时立即失败 / 失败闸三类情形 / 贵工具上限与放行 / 进度回执两条 /
资料不足三条 / CLI 进度注册），另有两个**真实图驱动的端到端**用例：一轮要 3 个 collect →
护栏在执行前拦下（工具一次都没执行、额度没烧）、并向用户提问；一轮 2 个不同主题的 collect →
正常执行且每个都有前后进度。全量 358 passed / `ruff` 通过。

### 7. 顺带查出的耗时腿：GitHub 星数改并发 + 封顶

**来源**：回答"单工具最长耗时是多少"时逐腿核算，发现预筛里的星数查询是一条没上限的腿。

**问题**：`screen_results` 打分时对每个 github 结果**逐条串行**回调 `fetch_star_count`
（`urlopen(timeout=10)`），而输入是**全部**去重后的搜索结果（`3~4` 条 query × `max_results=10`
→ 最坏约 40 条），条数不设上限 → 最坏几百秒。而星数在整个评分里只是个加分项。

**改动**：
- `src/pipelines/collect.py` — 新增 `_prefetch_star_counts()`：先并发查好放进 `{url: stars}`
  缓存，`screen_results` 的回调只读缓存。**`quality.py` 一行没动**（星数本来就是注入回调，
  所以纯函数与签名保持不变，逐条单测继续有效）；照抄 `fetch_many` 的线程池模式。
- `src/adapters/github.py` — 新增 `is_repo_url()` 粗筛（只让 github 仓库形状的链接占查询名额）。
- `src/config.py` — `GITHUB_STAR_MAX_LOOKUPS`（默认 10，**硬上限**）+ `GITHUB_STAR_WORKERS`（默认 5）。

**效果**：最坏耗时从"条数 × 10s 串行"（可达数分钟）降到 **≤ ceil(10/5) × 10s = 20s**；
未命中的链接不再触发任何网络请求（回调只读缓存）。无 token 时行为不变（一个请求都不发）。

**测试**：新增 5 个用例（封顶只查前 N 条 / 非 github 与"不像仓库"的链接不占名额 / 无 token
零请求 / **并发性**（8 条 ×0.25s、4 线程，实测远快于串行的 2s）/ 接线：回调读缓存且未命中不发网络）。
全量 363 passed。

### 8. read 的抓取腿：补上显式超时（与 collect 对齐）

**来源**：同一次逐腿核算查出的第二条——`read_pipeline` 直接调 `fetch_tool(url)` 不传任何超时。

**问题**：Firecrawl 客户端的 `timeout` 默认是 `None`（**不给 HTTP 请求设超时**），叠加 SDK 自己的
`max_retries=3`，read 的单页抓取最坏可以长时间挂住（理论无界）。collect 那侧不受影响——
`fetch_many` 有共享墙钟 deadline 且不等超时线程。这正是"裸客户端不设超时"那类事故的同一个形态。

**改动**：
- `src/adapters/fetch.py` — `fetch_tool` 新增可选 `max_retries`（覆盖 SDK 默认的 3 次重试），
  并在 docstring 里写明 **`timeout` 与 `max_retries` 必须成对给**（只给 timeout → 最坏 = timeout × 4）；
- `src/pipelines/read.py` — `fetch_tool(url, timeout=config.FETCH_TIMEOUT_SECONDS, max_retries=0)`。

**效果**：read 的抓取腿从「无上限」变成**最坏 45s 后抛错**。于是 read 的最长耗时也第一次有了确定
上界：抓取 ≤45s + 分类 ≤120s + 报告 ≤120s ≈ **最坏 4.75 分钟**。

**未改（明确记下）**：collect 侧的 `fetch_many` 保留 SDK 默认重试——我们的墙钟不等它，所以不影响
耗时，但**后台线程仍可能各重试 3 次、白烧 Firecrawl 额度**（每页最多 4 次）。是否收紧另议。

**测试**：新增 `tests/test_read.py`（该管道原先无直接单测）：抓取调用必须带 `timeout` 与
`max_retries=0`；抓取抛错时向上传递（不吞成空报告）。全量 365 passed。

### 9. 长调用心跳：把「🧠 之后」那段静默也填上

**问题**：第 6 项补的是"工具之间"的空档（进入/离开 + 耗时）。但事故复盘 §2.4 指的其实是另一个
洞：**`🧠 LLM 生成...` 之后那一次完整的 `generate_text` 调用，到它返回之前没有任何中间信号
——而这正是耗时占 99% 的一步**。第 5、6 项之后它被超时封到了 ≤120s，但静默本身还在。

**改动**：
- `src/config.py` — `LLM_HEARTBEAT_SECONDS`（默认 30s，0 关闭）；
- `src/adapters/llm.py` — `_heartbeat()` 上下文管理器（daemon 线程 + `Event.wait` 计时，
  退出时 `stop.set()`；进度回调异常绝不影响主流程），`generate_text` 新增 `progress` /
  `progress_label` 两个可选参数；
- `collect.py` / `read.py` — 两处报告生成挂上心跳（标签分别为「LLM 生成学习资料」/「LLM 生成解读报告」）。

**效果**（真实调用验证，interval=2s、调用 19.0s）：发出 9 条
`⏳ LLM 生成学习资料 仍在进行...（已 2s/4s/…/18s）`，秒数准确；**返回后再等 4s 条数不变**
（已停止，不会在工具结束后继续往会话里塞消息）。用户从此不再面对"🧠 之后一片黑"。

**未覆盖**（明确记下）：心跳只挂在两处报告生成（耗时 99% 的那一步）。read 的文档分类、
dedup / note / qa / verify / consolidate 这些 `generate_text` 调用典型耗时是秒级，没挂；
**`chat_with_tools`（对话通道）也没挂**——它最坏仍有约 135s 静默（45s×2 + 降级 45s），
要挂的话是同样的一行参数。

**测试**：新增 5 个用例（慢调用期间确实发心跳且带标签与秒数 / 返回后停止 / 无回调与关闭时
不启动线程 / collect 与 read 的接线断言）。全量 370 passed / `ruff` 通过。

---

## 待办

- **`self_level` 小数静默截断**（截至 2026-09-13 已发现、未实施）：用户输入 `3.5` 被解析正则取整为 `3`（`3.5→3`、`7.5→7`、`10.5→10` 通过校验）。建议接受小数（存 float 或四舍五入到最近整数），避免分级入口的静默失真。
