# Note 沉淀并行化完整报告（记忆系统 Step 1 v2）

> 日期：2026-08-29 ｜ 范围：route（定制化学习路线）模块的"学习内容自动沉淀"从同步阻塞改造为后台并行 + 确定性反馈/确认的全过程。

## 一、背景与目标

route 模块的 coach（陪练执行）阶段，学习内容靠"自动沉淀"写进知识库：对话积累到阈值后，把这段对话喂给 note 管道（召回 → LLM 差量提取 → 相似匹配），新知识点自动落库、相似候选由用户确认。

v1 是**同步执行**：触发沉淀的那一回合，agent 回复前要空等整个 note 管道跑完——而 note 提取实际可能耗时**几十秒**（不是预估的 2-5 秒），严重阻塞对话。

本次改造目标：
1. **沉淀不阻塞对话**（后台线程执行）；
2. **反馈确定性**（"已沉淀 N 个"必须出现，不依赖 agent 转述）；
3. **候选确认确定性**（相似候选必须弹给用户，不依赖 agent 呈现）；
4. 失败不丢内容、可自愈。

## 二、最终流程（自然语言 + 代码位置）

### 整体架构：一条"确定性写触发"流水线，三处落地

```
用户回复 → coach_human（攒对话）
         → coach_memory_write（单节点双阶段：排水 → fire）
         →（有候选？）coach_candidate_confirm（确定性确认）
         →（否则）coach_kb_retrieve（读路由）→ coach_llm（agent 回复）
```

### 第 1 步：攒对话（coach_human）

每个 coaching 回合，用户回复后，`coach_human` 把本回合的（assistant 讲解 + 用户回复）压进 `memory_sweep_buffer`（`src/graph.py`，仅 coaching 模式且无待确认笔记时）。buffer 是纯内存积累，checkpointer 持久化，中断恢复不丢。

### 第 2 步：fire——达到阈值就交给后台线程（coach_memory_write）

每回合的 `coach_memory_write` 做两件事（`src/graph.py`）：

**排水（处理上一回合 fire 的结果，毫秒级）**：
- 后台线程结果就绪 → 按结果分派：
  - `persisted`（新知识点已自动落库）→ 发确定性 SSE 进度事件 `🗂️ 已自动沉淀 N 个新知识点`（Web 端可见，不经过 agent）+ 给 agent 一条 system 提示；
  - `pending`（有相似候选）→ 暂存 `coach_note_pending`，图路由到确定性确认节点；
  - `skip`（无新内容）→ 只清在途标记。
- 线程仍在跑（未超时）→ 本回合**不阻塞、不重复 fire**，下回合再排；
- 线程失败 / 超时 / 进程重启 → **把快照并回 buffer**（不重跑、不阻塞），交给未来某个正常 fire 重扫——内容不丢。

**fire（触发新沉淀）**：无在途请求且 buffer 达阈值（累计 ≥6 个用户回合 或 ≥2500 字，`ROUTE_MEMORY_SWEEP_TURNS` / `ROUTE_MEMORY_SWEEP_CHARS`）→ 把 buffer **快照**进 `memory_sweep_inflight`（含 tech、buffer、fired_at），清空 buffer，spawn 一个 daemon 线程（`_start_sweep_thread`）。

### 第 3 步：后台线程——纯管道，静默执行

```python
def _start_sweep_thread(tech, buffer, tid):
    def worker():
        try:
            result = run_memory_sweep(tech, buffer)   # 纯读 + LLM，progress=None 后台静默
        except Exception as e:
            result = {"action": "error", "error": ...}
        with _sweep_results_lock:
            _sweep_results[tid] = result               # 进程内内存侧信道
    threading.Thread(target=worker, daemon=True).start()
```

- `run_memory_sweep`（`src/pipelines/route.py`）复用现有 `note_pipeline`：召回旧笔记 → LLM 差量提取 → 逐条相似匹配，**只读 + 调 LLM，绝不落库**（落库在确认后由确定性代码做）；
- 结果经**进程内内存侧信道** `_sweep_results`（键 = thread_id）交回——刻意不用文件：满足"后台 daemon 线程绝不写持久化状态"的硬约束（进程退出时内存无害丢失，重启靠 inflight 快照兜底）；
- daemon 硬约束：后台线程只做纯读 + 写进程内 dict，绝不写文件 / Chroma / 图状态。

### 第 4 步：排水应用——确定性反馈与确认

- **新知识点**：`persist_points` 在后台线程内已落库，排水只发 SSE 反馈 + system 提示，agent 可在回复中自然提及（不强制）；
- **相似候选**：排水设置 `coach_note_pending`，图条件边 `_route_after_memory_write` 路由到 `coach_candidate_confirm` 节点——**interrupt 用户**（复用 `format_merge_candidates` 展示候选），用户回复 `all / 编号逗号分隔 / skip` 后，`parse_merge_decision` 确定性解析 → `persist_points` 落库 → 清空 pending。**完全不经过 agent**；
- **任何 pending 都路由到确认节点**（不限于 sweep 的 `_auto` 标记）——旧遗留 pending 也会在下一条消息被弹出、由用户拍板解决，杜绝卡死。

### 图布线

```
coach_human → coach_survey → coach_memory_write
    └→（有 pending？）coach_candidate_confirm → coach_kb_retrieve → coach_trim → coach_llm
    └→（无 pending）coach_kb_retrieve → coach_trim → coach_llm
```

## 三、遇到的问题与解决

| # | 问题 | 根因 | 解决 |
|---|---|---|---|
| 1 | 触发沉淀回合 agent 回复前空等 2-5 秒 | v1 同步调 note 管道，而 note 实际耗时几十秒 | v2 后台 daemon 线程执行，耗时藏进"agent 回复 + 用户阅读打字"窗口 |
| 2 | 后台失败时出现"note 正在执行"SSE 阻塞对话 | 排水阶段的"同步兜底重跑"带 `_get_progress()`，在主线重跑 note 管道 | 改为 **buffer 恢复**：失败/超时把快照并回 buffer，未来正常 fire 重扫（不重跑、不阻塞、无 SSE） |
| 3 | 自动沉淀无用户反馈（"已沉淀 N 个"不出现） | 后端已发 SSE 进度事件，但前端 `showCoachReplyInput` 用 `el.innerHTML=` 整块替换 runStatus 面板，把进度行瞬间抹掉 | 前端改为**保留进度行、输入框追加在下方** |
| 4 | 相似候选呈现依赖 agent、不可靠 | 候选通过 system 提示让 agent 转述，真实模型可能忽略 | 新增**确定性确认节点** `coach_candidate_confirm`：interrupt 用户 → 解析 → 落库，不经过 agent（时机 A：排水后、agent 回复当前问题前） |
| 5 | agent 自主调 note 工具仍同步阻塞 | note 工具（`_note`）同步跑 `note_pipeline` + `ctx.progress`（SSE），是 sweep 之外另一条阻塞流 | **从 coaching 工具集移除 `note`/`note_commit`**（自动沉淀已覆盖其职责），coaching 提示词同步更新 |
| 6 | 重启后 sweep 彻底不执行（6+ 轮无触发） | 用户会话 checkpoint 里卡了一个**旧 note 工具流遗留的 `coach_note_pending`**（无 `_auto`）：阻塞 buffer 积累与 fire，且旧路由只处理 `_auto`、note_commit 工具又已移除 → 永久死锁 | 路由改为**任何 pending 都进确定性确认节点**，下一条消息弹出候选由用户解决（已验证：注入遗留 pending → 弹出确认 → skip → sweep 恢复） |
| 7 | 测试运行污染真实知识库（真实 `knowledge/<tech>/<topic>.md` 里多出两篇） | `test_e2e_async_fire_then_drain` 漏 mock `persist_points`，把测试夹具写进真实 `knowledge/` | 测试补 mock + `KNOWLEDGE_DIR` 隔离到 tmp_path |
| 8 | 耗时认知错误导致超时误判 | 以为 note 只要 2-5 秒，`ROUTE_MEMORY_SWEEP_TIMEOUT=60` 会把合法慢线程误判超时 | 修正认知（几十秒）+ 超时提到 **300** |
| 9 | 部署后修复"没起效" | 后端改动（graph.py/route.py/config.py）需**重启服务**才生效，浏览器强刷只更新 JS | 部署注意：重启 `python -m src.web.server` + 强刷 |

## 四、最终效果

**用户视角**：
- 触发沉淀的回合，agent **照常立即回复**——几十秒的提取在后台线程跑，与对话完全并行；
- 新知识点自动落库后，Web 端出现 `🗂️ 已自动沉淀 N 个新知识点` 进度行（确定可见）；
- 有相似候选时，**候选列表直接弹出**（all / 编号 / skip），决定后确定性落库——全程不经过 agent；
- 对话中**不再出现**"note 正在执行"的同步阻塞流；
- 后台失败 / 服务重启：内容**不丢**（快照并回 buffer，未来重扫），只是延迟。

**系统视角**：
- 不阻塞：后台线程 + 进程内内存侧信道 + 快照兜底，最坏情况（线程死/重启）也只在恢复后重扫，不阻塞任何回合；
- 确定性：反馈（SSE）与候选确认（interrupt 节点）都不依赖模型自觉；
- 自愈：失败 → buffer 恢复 → 未来重扫；卡死 pending → 路由到确认节点由用户解决；
- 逃生舱：`ROUTE_MEMORY_SWEEP_ASYNC=false` 退回 v1 同步路径。

**关键配置**：

```
ROUTE_MEMORY_SWEEP_TURNS=6     # 沉淀触发：累计用户回合数阈值
ROUTE_MEMORY_SWEEP_CHARS=2500  # 沉淀触发：累计对话字符数阈值（任一达到即触发）
ROUTE_MEMORY_SWEEP_ASYNC=true  # 并行沉淀开关；false 退回 v1 同步（逃生舱）
ROUTE_MEMORY_SWEEP_TIMEOUT=300 # 后台线程超时（秒）；超时未出结果 → 快照并回 buffer 重扫
```

**测试**：`tests/test_memory_sweep.py`（14 个，v1 同步路径 pinned）+ `tests/test_memory_sweep_async.py`（15 个，fire/drain/超时兜底/确认节点/图级 e2e），全量 265 passed。
