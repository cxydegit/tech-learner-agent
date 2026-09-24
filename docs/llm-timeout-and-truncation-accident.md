# 事故报告：collect 静默 22 分钟 + 资料报告撞 4096 被硬截断

> 发生 2026-09-09 16:29–16:57 ｜ 会话 `learn-会话A` ｜ 模型 `qwen3.8-flash`
> 肇事路径：`src/adapters/llm.py::generate_text`（经 `src/pipelines/collect.py`、`src/pipelines/read.py`）
> 事故分级：可用性（长时无响应）+ 数据质量（静默产出残篇）

**一句话**：报告生成这条通道没有业务超时（SDK 默认 600 秒），且复用了对话级的 `max_tokens=4096`。
模型在服务端慢响应 + 长输出时，这两个既有缺陷同时发作——单次调用静默拖到分钟级，输出又被硬切在
句子/表格中间；而 `coach_tool` 串行执行、阶段进度停更，让"慢"在外观上等于"死"。

---

## 一、现象

### 1.1 现象 A：一轮对话 22 分钟零回复（用户观感：卡死）

用户在 16:29 输入「A」，`conversation` 里此后**没有任何新消息**，直到 16:51:25。用户于 16:40
反馈"卡死了"。实际进程存活（uvicorn PID 11548、前端 SSE 连接仍在），线程一直在推进——

| 时间 | 事件 | 证据 |
|---|---|---|
| 16:29:44 | 模型一轮内**发起两个 `collect`**（claude-code skills + plugins 两个主题） | checkpoint `coach_messages` 的 `assistant.tool_calls` |
| 16:29:44 → 16:35:30 | `collect #1`（skills）执行完，报告落盘 | `materials/claude-code-skills-materials-0909-1635.md`（mtime 16:35:30，10,556 字符 / 15,971 字节） |
| **16:35:30 → 16:51:25** | `collect #2`（plugins）**静默 15 分 55 秒**后才落盘 | `materials/claude-code-plugins-materials-0909-1651.md`（mtime 16:51:25） |
| 16:51:25 | 两个工具结果**一起**写回 `coach_messages` | checkpoint 该步消息数 +1（两条 tool 消息同批） |
| 16:51:39 | 模型紧接着发起 `read` ×2 | checkpoint 的 `assistant.tool_calls` |
| 16:53:28 / 16:55:51 | 两篇解读报告相继落盘 | `reports/plugins-reference---claude-code-docs-20260909-解读.md`、`reports/create-plugins---claude-code-docs-20260909-解读.md` |

即：**该回合从用户输入到拿到正常回复约 27 分钟，其中 21 分 41 秒对话流完全静止。**

关于"至少调用了两次 collect"：这不是重复调用 bug，是模型**一次决策里同时要了 skills 与 plugins
两个主题**，恰好撞上长尾耗时，代价被串行执行放大。

### 1.2 现象 B：两篇资料报告被硬截断

| 文件 | 大小 | 落盘 | 截断形态 |
|---|---|---|---|
| `claude-code-skills-materials-0909-1635.md` | 10,556 字符 / 15,971 字节 | 16:35:30 | 断在正文句中：`如果 Skill 的 body ` 之后即结束 |
| `claude-code-plugins-materials-0909-1651.md` | 11,195 字符 / 16,268 字节 | 16:51:25 | 断在表格中间：`\| 没有 name \| 按文件名命名。` 后即结束，表格未闭合 |

两篇都止于 ~16K 字节量级，**这是"撞上 token 上限被服务端硬切"的典型特征**：切点落在哪不保证，
既不补句号也不闭合 Markdown 结构。

对照组更能说明问题：同一轮 `read` 产出的两篇解读报告（14,955 / 16,346 字节，16:53/16:55 落盘）
**结构完整、正常收尾**。差别在模板——`read` 是锁死的 7 节结构，模型会主动收敛；`collect` 模板
自由度高，模型放开写，就顶到了上限。

**危害不只"少了一段"**：残篇作为正常产物落盘，随后又被同轮的 `read` / `ask` 当作资料读入并写进
解读报告与知识库——错误内容进入了检索语料，事后无从区分"这段内容原本就没有"还是"被切掉了"。

### 1.3 现象 C：全程没有观测数据

`usage`（token 数）、`finish_reason`、单次调用耗时、重试与降级路径**全部被丢弃**——`generate_text`
只 `return response.choices[0].message.content`，`chat_with_tools` 只取 `content` / `tool_calls`。
全仓没有任何 logging（`grep -rn "getLogger\|^import logging" src/` 无结果）。

直接后果：**上面两条现象的原因，只能靠翻 checkpoint 数据库 + 观察文件断口反推**，而"截断 =
`finish_reason: length`"、"慢 = 走到 600 秒超时"这两条最关键的实锤，恰恰是拿不到的那两个字段。

---

## 二、原因

### 2.1 根因一：报告生成没有业务超时，SDK 默认 600 秒

`src/adapters/llm.py::generate_text`（修复前至今）：

```python
    client = OpenAI(
        api_key=config.OPENAI_API_KEY,
        base_url=config.OPENAI_BASE_URL,
    )
    response = client.chat.completions.create(
        model=config.LLM_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        temperature=0.5,
        ...
```

构造客户端时**没有传 `timeout`**，openai SDK 的默认值是 `httpx.Timeout(timeout=600, connect=5.0)`
（`openai==2.51.0` `_constants.py`：`DEFAULT_TIMEOUT` = 600s / `DEFAULT_MAX_RETRIES` = 2）。
语义是：**只要服务端不主动断开，单次请求可以静默挂 10 分钟**，期间业务层没有任何东西能打断它。

`collect #2` 观测到 15 分 55 秒的静默，与"LLM 生成约 10 分钟"（用户体感）叠加在同一台阶上，量级吻合。
需要说清楚的是：**没有一个业务层计时器在 10 分钟时触发**——这不是"超时被观察到"，而是"根本没有
超时，只有 SDK 的 600 秒兜底在起作用"。

同时 SDK 默认 `max_retries=2`：一旦触到 600 秒，它会自动再发一次请求，服务端从头生成一遍——
耗时翻倍、计费翻倍，且全部无日志。

对照：同文件的 `chat_with_tools` 已在后续改动中显式收紧（见第四节），**但 `generate_text` 没有**。

### 2.2 根因二：长文报告复用了对话级的 `max_tokens=4096`

`config.py:24`：`LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))`，`.env` 未设 → 生效值 4096。
`generate_text` 写死 `max_tokens=config.LLM_MAX_TOKENS`，且**没有 `max_tokens` 覆盖参数**
（`chat_with_tools` 有 `max_tokens: int | None = None`，两条通道待遇不同）。

OpenAI 兼容协议下 `max_tokens` 的语义是"生成到该 token 数即截断，不保证落在句子或结构边界"。
两篇 10K+ 字符的报告（中文 ~1 字 ≈ 1~1.5 token，叠加 Markdown 表格与英文术语）正好顶到 4096，
被就地切断。

**这是"参数复用"造成的**：4096 对交互式对话（一次回复几百 token）够用，对"整篇学习资料"不够。
`collect` 的模板又鼓励模型展开写，于是它一路写到撞墙。

### 2.3 放大器一：`coach_tool` 串行执行，且攒齐全部结果才写一次 state

`src/graph.py:542-561`：

```python
    for tc in tool_calls:                      # ← 串行：第 2 个工具要等第 1 个彻底跑完
        try:
            out = run_coach_tool(tc["name"], args, ctx)
        except Exception as e:
            out = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        results.append({...})
    updates = {"coach_messages": state["coach_messages"] + results, ...}   # ← 一次性提交全部结果
    return updates
```

两层影响：

1. **串行**：`collect #1` 用时 6 分钟与 `collect #2` 用时 16 分钟是相加的，不是取最大。
2. **攒齐才提交**：即使 `collect #1` 在 16:35 就有了结果，`coach_messages` 也必须等
   `collect #2` 在 16:51 结束后才能带上这两条 tool 消息——**所以对话流在这 22 分钟里一条都不更新**。

（注：串行本身是有意的保守设计，因为工具会通过 `ctx.updates` 改 `mode` 等共享状态，并发会引入竞态；
这一层属于"已知取舍"，不是缺陷，但它把单点长尾的代价线性放大了。）

### 2.4 放大器二：进度反馈的可见边界，正好停在整个黑洞前面

进度是 `coach_tool` 通过 `CoachCtx(progress=...)` 往下传的回调，只在**管道阶段切换**时发一条：
`🔍 搜索` → `🛡️ 预筛` → `🛰️ 并发抓取` → `🧠 LLM 生成学习资料`。

`🧠` 之后是一次完整的 `generate_text` 调用，到它返回之前**没有任何中间信号**——而这一步正是耗时的
99%。用户看到的最后一条动态是"🧠 LLM 生成"，然后 10 分钟以上没有下一句。前端可展示的
`conversation` 又要等 `coach_tool` 整个 return 才更新，于是"最后一条进度 → 新消息"之间是完全空白。

### 2.5 输入侧：collect 的 prefill 规模本来就偏大

`src/pipelines/collect.py:153-185`，喂给报告的 `user_content` 由三块拼成：

- 抓取正文：`MAX_FETCH_PAGES=5` 个页面，每页取前 4,000 字符 → 最多 ~20,000 字符；
- 搜索结果摘要：前 10 条，每条标题 + 链接 + 摘要前 200 字符；
- 质量预筛的排除清单。

也就是**单次请求的输入在几万字符 / 上万 token 量级**。生成快慢不只取决于输出长度：这样规模的
prefill 会显著抬高首 token 延迟，并在服务端排队时成倍放大。`read` 侧更直接，把抓取全文
（`fetch` 层上限 `MAX_FETCH_CHARS=16000`）原样送进 `generate_text`（`read.py:171`）。

抓取环节本身是有保护的（`fetch_many` 共享 45 秒 deadline、单页失败记空不拖垮整批），
**出问题的环节全在"LLM 生成"这一步**。

### 2.6 为什么换成 `qwen3.8-flash` 才显形

这两个缺陷一直存在，只是被新模型的输出倾向放大了：输出更长（更容易撞 4096）、响应更慢
（更容易触到 600 秒台阶）。同一批代码在短回复、快响应的模型上不会暴露——**这是"参数与环境
耦合"的典型形态：缺陷在代码里，触发条件在模型侧。**

---

## 三、结论：这是两个既有缺陷被放大，不是模型故障

| 问题 | 性质 | 根因位置 |
|---|---|---|
| 22 分钟零回复 | 单点无超时 + 串行 + 零中间反馈叠加出的观感 | `llm.py::generate_text` 无 `timeout`；`graph.py::coach_tool` 串行且攒齐才提交 |
| 报告被硬截断 | 长文生成复用了对话级 token 预算 | `llm.py::generate_text` 固定 `max_tokens=4096`（`config.LLM_MAX_TOKENS`） |
| 定位极慢 | 观测缺口 | `usage` / `finish_reason` / 耗时 / 重试路径全丢弃，无日志 |

**推断强度声明**：以上"截断 = 撞 `max_tokens`"、"慢 = 走到 SDK 600 秒"是由**文件断口形态 + 代码参数
+ SDK 默认值**构成的一致证据链，**不是服务端日志的直接实锤**——因为拿不到 `finish_reason` 与耗时。
想坐实，去 dashscope 控制台查这两次调用的 `finish_reason`（应为 `length`）与耗时（应接近 600s）。

---

## 四、处置状态（以 2026-09-13 工作区代码为准）

| 项 | 状态 | 说明 |
|---|---|---|
| `chat_with_tools` 超时与重试 | **已改（commit 896db65）** | `_get_client()` 进程内单例，显式 `timeout=config.LLM_REQUEST_TIMEOUT`(45s)、`max_retries=0`；外层指数退避 + `LLM_RETRY_BUDGET_SECONDS`(90s) 总预算 + `_classify` 错误分档（retry / degrade / abort）+ fatal 专用文案。交互式循环的 600 秒台阶与 3×3=9 次请求放大已消除；45s 有实测支撑，见附录 C |
| `generate_text` 超时 | **已修（未提交）** | 改走同一个 `_get_client()`（`max_retries=0`，SDK 的 2 次重试已收紧）+ 显式 `timeout=config.LLM_REPORT_TIMEOUT`(120s)，**单次不重试**——长任务重发等于再烧一份 token 再等一轮 |
| 报告 `max_tokens` 预算 | **已改（未提交）** | 新增 `REPORT_MAX_TOKENS`(8000)，collect / read 两个报告调用点显式传入；被截断（`finish_reason == "length"`）时正文末尾自动追加显式标注 + 打 WARNING 级日志——**残篇不再静默进入知识库**。⚠️ 同时给两份报告提示词加了「字数控制在 800~2000 汉字」，但实测在当前模型上**无可测效果**（附录 C 的 A/B），保留它只是对"换更啰嗦的模型"的廉价保险；真正的兜底是预算与标注 |
| `coach_tool` 串行与批次提交 | **未动** | 属有意取舍，需与 `ctx.updates` 竞态一起评估 |
| 工具级进度（工具名 / 已耗时） | **已改（未提交）** | 每个工具执行前后各一条进度（`⚙️ collect 执行中...` / `✅ collect 耗时 212s`，失败为 `⚠️`）；并补上 CLI 的 route 路径（此前只有 Web 注册进度，CLI 全程静默）。§2.4 指出的另一个洞（「🧠 LLM 生成」之后那一步全程无信号）也已补上：报告生成挂心跳，每 30s 发一条「⏳ 仍在进行…（已 Ns）」（`LLM_HEARTBEAT_SECONDS`，0 关闭） |
| 贵工具失败后重跑 / 单轮次数 | **已改（未提交）** | 阶段标签（`CollectStageError` / `ReadStageError`）+ 失败闸：生成段失败即拒绝同回合重跑（搜索与抓取都已成功过），搜索段首次失败放行一次，参数错误不记；贵工具单轮上限 `ROUTE_MAX_HEAVY_TOOLS_PER_TURN=2`（超限停下问用户，不硬拒——两个不同主题是合法需求） |
| LLM 调用埋点（`finish_reason` / `usage` / 耗时） | **已改（未提交）** | `llm.py::_log_call` 一次尝试一行 JSON：`site` / `attempt` / 耗时 / `status` / `kind` / HTTP 码 / `finish_reason` / prompt·completion token / `fallback`。默认开、不设开关；**只记元数据，提示词与正文不进日志**。10 个调用点带 `call_site`（collect.report / read.report / read.classify / note.* / qa.answer / dedup.judge / verify.milestone / coach.consolidate / coach.chat） |

> 注：`generate_text` 现已受超时保护，但它与对话通道是**两套上限**（报告 120s / 对话 45s）——
> 报告通道单次调用是长任务、失败代价是整轮 collect 白做，故余量取得更宽。

---

## 五、待办（沿用既定优先级，仅列不展开）

- **P0**　① `generate_text` 显式超时（90~120s）+ 收紧 SDK 重试 —— **已完成 2026-09-13**（120s）；
  ② 同处加 LLM 埋点（`finish_reason` / `usage` / 耗时 / 重试 / fallback / call-site）—— **已完成 2026-09-13**；
  ③ collect、read 报告独立 `max_tokens`（`REPORT_MAX_TOKENS=8000`）**并**在模板里给出字数上限
  —— **已完成 2026-09-13**（`max_tokens` 生效；模板字数行经 A/B 实测无显著效果，见附录 C）。
- **P1**　④ `coach_tool` 执行工具前后发进度（工具名 + 已耗时）——**已完成 2026-09-13**（含 CLI 接线 + 报告生成心跳，见 §2.4 那半个洞）；⑤ run 级审计事件
  （`web/runner.py::_worker` 与 CLI 两个入口）+ 与 `checkpoint_id` 关联。
- **P2**　⑥ 事件载体正式化（本地 JSONL / 独立 sqlite，暂不引 OTel / Langfuse）+ 统一 schema；
  ⑦ 副作用与行为信号埋点（笔记入库 + Chroma 索引、文件写入、roadmap 保存、护栏触发、sweep fire/drain）。
- **P3**　⑧ 截断兜底检测（结构未闭合标注）；⑨ 重工具单轮限发一个或安全并行 —— **已改（2026-09-13，取"单轮上限 2 + 失败闸"，未取"限发一个"：两个不同主题是合法需求，事故的病根是串行与批次提交，不是调用次数）**；⑩ 审计查询端点与前端面板。

> P1⑤ / P2⑥⑦ / P3⑩ 的完整落地方案见 `OBSERVABILITY_PLAN.md`（现状核实、缺口清单、四阶段步骤与验收）。

**P0 验收信号**：~~日志里能看到每次 LLM 调用的 `finish_reason` 与耗时~~ **已达成（2026-09-13，
真实调用已验证输出 `finish_reason` / 耗时 / token 数）**；~~模拟慢响应时 120 秒内失败并给出降级提示，
而不是静默 10 分钟~~ **已达成（`generate_text` 120s 超时 + 调用方降级）**；~~`collect` 报告不再中断
在句子中间~~ **已达成（预算 4096→8000；万一仍被截断则正文末尾带显式标注 + WARNING 日志，
不再有静默残篇）**。

---

## 附录 A：涉及位置

| 位置 | 内容 |
|---|---|
| `src/adapters/llm.py::generate_text` | 无 `timeout` 的裸 `OpenAI(...)`；固定 `max_tokens=config.LLM_MAX_TOKENS`；返回值丢弃 `usage` / `finish_reason` |
| `src/adapters/llm.py::chat_with_tools` | 同源缺陷；已加固（`_get_client` / `_classify` / 退避预算） |
| `src/config.py::LLM_MAX_TOKENS` | `4096`（未在 `.env` 覆盖）——报告截断的硬上限 |
| `src/config.py::LLM_REQUEST_TIMEOUT` / `LLM_MAX_ATTEMPTS` / `LLM_RETRY_BUDGET_SECONDS` | 新增的重试与超时配置，目前只被 `chat_with_tools` 使用 |
| `src/pipelines/collect.py:186` | 报告生成调用点；input 由 5×4000 字符抓取块 + 10 条搜索摘要拼成 |
| `src/pipelines/read.py:171` | 解读报告生成调用点（输入为全文，上限 `MAX_FETCH_CHARS=16000`） |
| `src/pipelines/read.py:114` | 技术文档分类调用（同样无超时保护，输入 3000 字符） |
| `src/graph.py::coach_tool` | 串行 `for` 循环执行工具，攒齐全部结果后一次性写 `coach_messages` |
| `.graph/checkpoints.sqlite` | 唯一可用的执行历史（SqliteSaver 快照），本次时间线全部由它还原 |

## 附录 B：复查方法（只读，可随时重跑）

`.graph/checkpoints.sqlite` 的 `checkpoints.checkpoint` 列是 **msgpack** 序列化的 blob（直接
`json.loads` 会 `UnicodeDecodeError`），`metadata` 是 JSON bytes。只读回放：

```python
import sqlite3, json, msgpack
DB, TID = r".graph/checkpoints.sqlite", "learn-会话A"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT checkpoint_id, checkpoint, metadata FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_id",
    (TID,)).fetchall()
for cid, cp_raw, md_raw in rows:
    cp = msgpack.unpackb(cp_raw, raw=False)
    md = json.loads(md_raw) if md_raw else {}
    msgs = cp["channel_values"].get("coach_messages") or []
    last = msgs[-1] if msgs else {}
    names = [(tc.get("function") or {}).get("name") for tc in (last.get("tool_calls") or [])]
    print(cp.get("ts"), "step=", md.get("step"), "last=", last.get("role"), "tools=", names)
```

复核文件完整性（断口形态）：

```bash
tail -c 160 materials/claude-code-skills-materials-0909-1635.md
tail -c 160 materials/claude-code-plugins-materials-0909-1651.md
```

**踩过的坑**：`SqliteSaver.list()` 返回 `ORDER BY checkpoint_id DESC`（最新在前），
按"循环最后一条是最新"去读会读到最老的状态，得出相反结论。

---

## 附录 C：超时定值的实测依据（2026-09-13）

配置里的超时数字不该是拍的。当天在真实网关（`deepseek-flash` / 腾讯 MaaS）上打了 5 次计时调用：

| 探测 | 形态 | 耗时 | `finish_reason` | prompt / completion | 吞吐 |
|---|---|---|---|---|---|
| 8-1 | 极短输入 + 64 token | 2.2s | stop | 32 / 53 | 24 tok/s |
| 8-2 | 真 coach 提示词（667 字符）+ 提问，max_tokens=4096 | 3.8s | stop | 409 / 613 | 160 tok/s |
| 8-3 | 要求约 2000 字长输出，max_tokens=4096 | **22.8s** | **length** | 56 / **4096** | 179 tok/s |
| 8-4 | 报告形态：5 段抓取正文（6593 字符）+ 满 max_tokens | 18.0s | stop | 3912 / 3311 | 184 tok/s |
| 10-c | 降级形态：历史含 `role: tool` 回执 + **不传** `tools` | 1.2s | stop | 83 / 61 | 网关正常接受 |

三条结论：

1. **对话通道 45s 有实测支撑**：最坏合法时长 = 满 4096 token ≈ 23s（8-3），留约 2× 余量。
   报告通道取 **120s**：在 8-4 的 18s 之上留 4~6×，因为报告失败会让整轮 collect 白做。
   ⚠️ **这两个数绑定当时的模型速度**，换更慢的模型/网关必须重测——本事故正是"模型换了、数字没换"。
2. **截断机制当场复现**：8-3 拿到 `finish_reason: length` 且 completion 恰好 4096。第三节当时
   只能把"截断 = 撞 `max_tokens`"列为推断（缺服务端实锤），现在有了可复现的现场。注意这复现的是
   **机制**（长输出 + 4096 上限），不是 09-09 那两次调用本身的回执——要坐实那次仍需服务端日志。
3. **降级请求形态被网关接受**（10-c）：历史里带 `role: tool` 回执、但不传 `tools` 参数，网关正常
   返回纯文本。此前担心"某些兼容网关会拒这种组合"，实测不成立。

### C.2 报告长度分布与字数约束的 A/B（2026-09-13）

**真实产出统计**（29 篇 `materials/` + 26 篇 `reports/`，共 55 篇）：

| 口径 | materials 中位 | materials p90 | materials 最大 | reports 中位 | reports p90 | reports 最大 |
|---|---|---|---|---|---|---|
| 字符数 `len()` | 2,799 | 4,352 | 10,759 | 3,368 | 7,121 | 10,739 |
| 汉字数（仅 CJK） | 1,028 | 1,906 | 2,367 | 990 | 1,933 | 2,251 |
| Word 式字数 | 1,142 | 2,112 | 3,063 | 1,178 | 2,388 | 3,055 |
| token 估算（cl100k，±20%） | ~1,700 | ~3,050 | ~5,023 | ~1,860 | ~3,920 | ~5,047 |

⚠️ **汉字只占字符数的约 1/3**（其余是英文术语 / 代码 / URL / Markdown 符号）。讨论"字数"必须
说清是哪个口径，否则同一个约束会被理解成 3 倍的长度差——这个坑当天真的踩了一次。

**结论**：4096 覆盖 51/55（93%），超出的 4 篇全在 9-09 那批长尾里；
`REPORT_MAX_TOKENS=8000` 对历史最长报告留约 60% 余量。

**字数约束 A/B**（同一输入，唯一差别是提示词里那行「报告不要太过冗长，字数控制在 800~2000 汉字」）：

| 输入规模 | A 无约束 | B 有约束 |
|---|---|---|
| 1.9K prompt token | 707 汉字 / 1083 token / 6.1s | 922 汉字 / 1841 token / 10.1s |
| 7.6K prompt token（近真实 collect） | 861 汉字 / 2201 token / 11.7s | 882 汉字 / 1962 token / 10.4s |

**这行约束没有实测支持**：在接近真实规模的输入上，模型本来就落在 800~2000 汉字区间内，
加不加几乎一样（861 vs 882 汉字）。保留它只是便宜的保险（9-09 那批出自更啰嗦的模型，
报告到 10K 字符量级），**真正的兜底是预算与截断标注**。日后若嫌提示词冗长，这行可以第一个删。

**截断标注链路验证**：`max_tokens=30` 强制触发 → 日志里 `finish_reason: "length"`（INFO）
紧跟 `{"event": "llm_truncated", ..., "annotated": true}`（WARNING），正文末尾带显式标注。
