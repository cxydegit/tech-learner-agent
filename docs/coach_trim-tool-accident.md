# 事故报告：裁剪把 tool 回执和它的 assistant 切散，长会话再也不能用

> 发生 2026-09-10 21:58 ｜ 修复 2026-09-11 19:35 ｜ 范围：`coach_trim`（coach 循环的上下文裁剪）

一句话：上下文压缩按「消息条数」切，切点落在 `tool` 消息上，把这条 tool 回执与它配对的
`assistant(tool_calls)` 分了家。模型协议要求 tool 消息必须紧跟在它的 assistant 之后，
保留段第一条成了「无主回执」→ 请求非法 → 400。坏状态被 checkpointer 落库，**每次重试都重发同一份坏历史**，会话彻底卡死。

---

## 一、现象

长会话进行到某一轮，用户输入后得到的不再是回复，而是：

```
⚠️ 模型调用暂时不可用（Error code: 400 - {'error': {'type': 'invalid_request_error',
 'code': '400001', 'message': "The request is invalid: Messages with role 'tool' must be a
 response to a preceding message with 'tool_calls'.", 'source': 'client',
 'request_id': '...'}}）。请稍后再试，或输入「结束」退出。
```

这句话把人带偏了两处：

- 说「暂时不可用 / 请稍后再试」，听着像瞬时故障。**实际是确定性错误，等多久都不会好**。
- 报错里 `'source': 'client'` 已经点明问题在请求方，但被"模型不可用"的措辞盖住了。

**关键区别：这不是一次失败的请求，是一份已经存坏的会话记录。** 坏消息列表由 `coach_trim`
写回状态 → checkpointer 落库 → 之后每次请求都从库里读回同一份坏历史，照样 400。
重开进程、输入任何内容都没用。

触发条件只有一条：**消息总量超过 40 条触发过压缩**。短会话永不触发，所以永不出现这个问题。

---

## 二、根因

### 2.1 一份合法的对话在消息层面长什么样

OpenAI 兼容协议里，一次工具调用是**两条互相绑定的消息**：

```
assistant  tool_calls=[call_A]     ← 发起
tool       tool_call_id=call_A     ← 回执，必须紧跟它的发起者
```

两者靠 `tool_call_id` 配对。**回执单独出现就是非法请求**，任何模型都会拒绝。

而 coach 是 agent 循环，一个用户回合（一轮）实际是：

```
user                              ← 这一轮从这里开始
assistant(tool_calls)             ┐
tool                              │  这些都属于同一轮的内部过程
tool                              │
assistant(收尾)                    ┘
user                              ← 下一轮从这里开始
```

**能安全下刀的位置只有一处：`user` 消息。** 在别处切，就可能切开一对 assistant/tool。

### 2.2 旧代码切在哪

旧 `coach_trim`（修复前的版本）：

```python
keep = config.COACH_HISTORY_KEEP * 2          # 注释写着「一轮 ≈ 一问一答两条」
...
old, recent = msgs[:-keep], msgs[-keep:]      # 纯按条数切
```

问题就出在 `* 2` 这个假设上。「一轮 = 两条消息」只在**用户提问、模型直接文本作答**时成立；
在 agent 循环里一轮是 **2～18 条**（每多一次工具调用就多 2 条），所以 `keep = 10 * 2 = 20 条`
既不等于 10 轮，也保证不了切点在轮边界上。切点落在哪条消息上是随机的——而这份消息序列里
**tool 消息约占四成**，撞上的概率就是这个量级。长会话每轮都在掷这个骰子。

### 2.3 事故现场（数据库里的原始记录）

`learn-会话A` 这个会话，压缩发生前后的 checkpoint：

| checkpoint | 消息数 | 无主 tool 消息 |
|---|---|---|
| step 92（压缩前） | **41** | 无 |
| step 93（`coach_trim` 执行后） | **20** | **[0, 1]** |

41 条变成 20 条，就是 `msgs[:-20] / msgs[-20:]` 切的结果。切点附近逐条打出来：

```
[20] assistant  tool_calls = [call_fefc28e…, call_d079667…]   ← 被切走
[21] tool       tool_call_id = call_fefc28e…                  ← 被留下
[22] tool       tool_call_id = call_d079667…                   ← 被留下
```

保留段保留了这两条回执、却把它们的发起者切走了。从此每次请求的第一条都是无主回执，
**这个会话连续 400 了将近一天**，直到修复上线。

### 2.4 不是偶发

库里 4 个有消息的会话，**全部损坏，形态完全一致**——保留段开头挂着 1～2 条无主回执：

| thread_id | 消息数 | 无主 tool 下标 |
|---|---|---|
| `learn-会话A` | 21 | 0, 1 |
| `learn-会话B` | 33 | 0, 1 |
| `learn-会话C` | 25 | 0 |
| `learn-会话D` | 23 | 0 |

「清一色首条是 `tool`、无主回执全在段首」本身就是「切点落在 tool 上」的直接指纹。

### 2.5 为什么三层容错一个都没兜住

事故当时的 `src/adapters/llm.py`：

1. **重试 3 次** —— 请求体结构非法是确定性的，3 次全败
2. **降级去掉 `tools`** —— 问题在 `messages` 的角色序列，跟 `tools` 参数无关，照样失败
3. **抛错** → graph 打印「模型调用暂时不可用」

一次报错白烧 4 次无效调用，还把确定性错误伪装成网络抖动。
（这条链路已在本轮修复中改掉：确定性错误不再重试、也不再走降级——见第六节。）

---

## 三、改前 vs 改后

| 维度 | 改前 | 改后 |
|---|---|---|
| **切点依据** | 下标（`msgs[:-20]`，纯按条数） | 轮起点（从后往前第 `COACH_HISTORY_KEEP` 条 `user` 消息） |
| **保留段首条** | 随机，可能是 `tool` | 恒为 `user` |
| **会不会切散 assistant/tool 对** | 会，每次压缩都在掷骰子 | 不会，物理上不可能 |
| **孤儿如何处置** | 无此概念，切出来就存进库 | `_sanitize_messages` 每轮无条件清理 |
| **存量坏会话** | 只能手改数据库（不可逆，需先备份） | 下次对话自动自愈，**不写库、不需备份** |
| **保留量** | 恒定 20 条（≈2～5 轮，轮被切碎） | 5 个完整轮（实测约 16～26 条，见 4.2） |

新增两个函数（`src/graph.py`）：

- `_turn_cut_index`：从后往前数第 N 条 `user` 消息，切在它前面。数不够 N 轮就不裁。
- `_sanitize_messages`：丢掉 `tool_call_id` 找不到对应 `assistant.tool_calls` 的 tool 消息。
  **只在协议上非法的消息才会被丢**，正常配对的一条不动。

`coach_trim` 本体只改了两处：进入时先净化；压缩时用轮起点当切点。

### 自愈是怎么发生的（存量会话不用动数据库）

`coach_trim` 是 coach 循环的汇合点——入口、`coach_tool`、`coach_kb_retrieve` 三条边都汇入它，
**每轮必走**。所以坏会话下次 `--resume` 输入任意内容时：

```
coach_human → coach_survey → coach_memory_write → coach_kb_retrieve → coach_trim（净化）
                                                                          ↓
                                                                   coach_llm（拿到干净列表）
```

净化发生在调模型之前。四个坏会话已用库内真实状态只读验证：无主回执 `[0,1]/[0,1]/[0]/[0]` → 全部清空，
保留段重新通过配对校验。生产日志也印证了：`learn-会话A` 在 step 98→99 那一步
无主回执消失、**且该步没有调用模型**（没有新内容生成），正是净化的签名；此后连续多轮再无孤儿。

---

## 四、这两个数字怎么定的

改动把「轮」从「2 条消息」纠正为「一条 user 消息起，到下一个 user 之前」，
保留量的含义随之改变。两个数字最终定为 **`COACH_HISTORY_KEEP = 5`（轮）**、
**`COACH_COMPRESS_AT = 40`（条）**。

### 4.1 关键约束：每裁一次就调一次 LLM

`coach_trim` 裁剪时要把被丢掉的消息喂给 `consolidate_memory` 做三舱记忆整理，
**这是一次真实的 LLM 调用**。所以「压缩次数」是硬成本，谈保留窗口必须连它一起谈。

### 4.2 反直觉：窗口留得越大，压缩越频繁

裁剪后窗口剩多少条，决定了还要攒多久才再次超过 40。**留得多 → 离门槛近 → 很快又触发**。
200 轮模拟（每轮均值 3.3 条）实测：

| 保留策略 | 压缩次数 | 每次保留窗口 |
|---|---|---|
| 留 10 轮 | **174** | 20～78 条，且出现「触发却不裁」的空转 |
| 留 6 轮 | 77 | 12～56 条 |
| **留 5 轮** | **57** | **10～51 条** |
| 留 4 轮 | 42 | 8～47 条 |

**这张表不是「和谁比」，而是这个参数本身就长这样**：压缩次数几乎线性地随窗口增大而增大
（每多留 1 轮 ≈ 多 15～35 次压缩）。这里没有"正确值"可对标——旧实现的 20 条窗口只是一个
被拍下来的数字，而且那条路径本身就是坏的（会切出无主 tool 回执），不适合当成本基线。
真正要选的是：**每次给模型留多少上下文 ↔ 愿意为此付多少次压缩调用**。

「留得多 = 更省心」是错的：留 10 轮之所以要压 174 次，一半是因为窗口离 40 太近、
一半是因为它还撞上了空转——41 条的窗口里凑不出 10 条 `user`，于是**每轮触发、每轮不裁**，
窗口一路涨到 78 条。窗口里要凑出 10 条 `user`，得保证平均每 4 条消息就有一条 user；
实测每轮均值 3.3 条，看着够，但单轮最长 9 条会把它拖垮。**降到 4～5 轮后，
200 轮模拟里空转 0 次**——要凑的 user 少了 5 条，容错空间大得多。

### 4.3 为什么是 5 而不是 4

| | 留 4 轮 | 留 5 轮 |
|---|---|---|
| 压缩次数/200 轮 | 42 | 57 |
| 窗口上界（200 轮模拟） | 47 条 | 51 条 |
| 真实会话裁后窗口 | 未测 | 16～26 条 |

留 4 轮更省 LLM 调用（42 次），代价是上下文更薄（按均值 3.3 条/轮推，约 13 条）。取 5 轮，
真实会话实测裁后 16～26 条，上下文更厚一档，压缩 57 次。

**5 轮不是算出来的最优解**，它是"窗口厚度"这一档里选中间：比 4 轮多留一轮上下文，
比 6 轮少花 20 次压缩。真正的结论是**这个参数就是压缩频率的旋钮**——
每次觉得 LLM 调用太贵就往下调一档（4 轮 → 42 次），觉得模型"忘了刚才说过什么"就往上调
一档（6 轮 → 77 次）。没有需要守住的正确值。

### 4.4 token 不成约束（所以这里只谈次数，不谈量）

| 指标 | 实测值 |
|---|---|
| 每轮消息条数 | 均值 **3.3**、中位 2、最大 9 |
| 单会话总量 | 14K～26K 字符，最大 **9.3K token** |
| 固定注入（提示词+工具+路线/画像） | ≈ **1200 token**/次 |
| 模型输入上限 | 实测接受 **331K token** 输入 |

即便按最宽松的留 10 轮，单次请求也才约 11.7K token，占模型窗口不到 4%。
所以保留量不是问题，**成本全在压缩次数上**——这也是为什么这一节的结论是"往小留"。

### 4.5 为什么 COMPRESS_AT 保持 40

阈值 40 的作用是「什么时候开始丢最老的轮」。4 个真实会话各自的最大快照**只有 41～44 条**，
意味着压缩在这些会话里几乎不触发——它们全都放得下，没必要动。调高会削弱压缩的意义；
调低会让旧消息更早离开上下文。**40 不动**，等出现更长的真实会话再据实测调。

### 4.6 副作用

- 保留量不再恒定（在轮边界切，条数随工具密度浮动）：真实会话裁后 16～26 条，
  200 轮模拟里最坏到 51 条（长轮撞在切点上）。
- 语义变了：`COACH_HISTORY_KEEP` 是**轮数**不是条数，且它同时是压缩频率的旋钮。

---

## 五、验证方式

**单测**（`tests/test_coach_loop.py`）：

- `test_coach_trim_cut_lands_on_user_not_mid_tool_calls`：10 轮 × 每轮 2 个工具调用（50 条），
  断言保留段首条是 `user`、且每条 tool 都有主
- `test_coach_trim_heals_existing_orphan_tool_messages`：直接喂坏状态，断言孤儿被清理且不触发摘要
- `test_coach_trim_keeps_all_when_too_few_turns`：超阈值但凑不出 KEEP 条 user → 不裁、不调摘要
- `test_coach_trim_compresses_over_threshold`：断言切在轮起点

**全量**：`pytest -q` 329 passed；`ruff check src tests` 通过（与 CI 一致）。

**真实会话**（把库里 4 个会话的压缩前状态喂给改后的切点，只读、不写库）：

| 会话 | 原条数 | 裁后 | 首条 | 无主 tool |
|---|---|---|---|---|
| `learn-会话A` | 41 | 26 | `user` | 无 |
| `learn-会话D` | 43 | 16 | `user` | 无 |
| `learn-会话B` | 44 | 23 | `user` | 无 |
| `learn-会话C` | 42 | 18 | `user` | 无 |

**压缩次数模拟**（200 轮、每轮均值 3.3 条，直接调用实现里的 `_turn_cut_index` 而非另写一份
逻辑）：留 4/5/6/10 轮分别触发 42/57/77/174 次压缩，留 5 轮时空转 0 次。第 4.2 节那张表的
数字即由此得出——**测的是这个参数本身的性质，不依赖任何旧实现的对照**。

**只读取证**（随时可复核，扫全库的无主 tool 消息；修复后应为空）：

```python
import sys, sqlite3; sys.path.insert(0, '.')
from langgraph.checkpoint.sqlite import SqliteSaver
from src.config import config

def orphans(msgs):
    seen, out = set(), []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []: seen.add(tc.get("id"))
        elif m.get("role") == "tool" and m.get("tool_call_id") not in seen:
            out.append(i)
    return out

with SqliteSaver.from_conn_string(str(config.GRAPH_DB_PATH)) as sv:
    latest = {}
    for tup in sv.list(None):
        tid = (tup.config.get("configurable") or {}).get("thread_id")
        if tid and tid not in latest:            # ⚠️ list 是降序（最新在前），取第一次出现
            latest[tid] = (tup.checkpoint or {}).get("channel_values") or {}
    for tid, vals in latest.items():
        msgs = vals.get("coach_messages") or []
        if msgs:
            print(f"{tid[:24]:26} msgs={len(msgs):4} first={msgs[0].get('role'):9} orphans={orphans(msgs)}")
```

**端到端**：`route <技术名> --resume <thread_id>` 能正常对话即通过（已在
`learn-会话A` 上验证：修复当天该会话恢复对话，连续多轮无异常）。

---

## 六、复盘：为什么定位慢（两项均已修复）

事故本身容易定位，慢在两件事，都与切点逻辑无关：

1. **重试掩盖了错误性质。** 确定性错误（400 结构非法）也重试 3 次 + 降级 1 次 = 白烧 4 次调用，
   还让人以为是网络问题。**已修**：`llm.py` 按状态码分流——瞬时错误（连接/超时/429/5xx）
   退避重试且受总预算约束，确定性错误（key/模型名/请求体不合法）直接失败不重试，
   `ToolCallError.fatal` 标识出来。
2. **提示语指向了错误方向。** 「暂时不可用 / 请稍后再试」对确定性错误是误导性措辞。
   **已修**：`coach_llm` 按 `fatal` 分流文案，确定性错误明确告知「这是确定性错误，重试无用」，
   不再让用户干等。

> **已落地（2026-09-11）**：两项均已实施——错误按状态码分档（瞬时退避重试 + 总预算封顶 / 确定性错误立即失败并带具体原因），降级回复另加用户可见提示、降级提示词显式告知模型工具不可用。详见 `OPTIMIZATION_LOG.md` 的 2026-09-11 条目。

---

## 附录：涉及位置

| 位置 | 内容 |
|---|---|
| `src/graph.py::_turn_cut_index` | 新增：切点退到轮起点（user 消息） |
| `src/graph.py::_sanitize_messages` | 新增：每轮无条件清理无主 tool 消息 |
| `src/graph.py::coach_trim` | 裁剪逻辑本体（净化 + 按轮切） |
| `src/config.py` | `COACH_HISTORY_KEEP=5` / `COACH_COMPRESS_AT=40` 及注释更正；新增 `LLM_REQUEST_TIMEOUT` / `LLM_MAX_ATTEMPTS` / `LLM_RETRY_BUDGET_SECONDS` |
| `src/graph.py` 三条汇入边 | 入口 / `coach_tool` / `coach_kb_retrieve` → `coach_trim`（每轮必走，自愈的前提） |
| `src/adapters/llm.py` | 错误分类重试：瞬时退避重试 / 确定性立即失败（`ToolCallError.fatal`）+ 请求超时与重试预算 |
| `tests/test_coach_loop.py` | 三条新/改单测 |
| `.graph/checkpoints.sqlite` | 会话状态持久化（坏状态的落库点） |

**取证时的两个坑**（都踩过）：

1. `SqliteSaver.list()` 返回 **`ORDER BY checkpoint_id DESC`（最新在前）**。按"循环结束时的
   最后一条是最新"去读，读到的是**最老**的状态，会得出完全相反的结论。
2. `checkpoints.checkpoint` 列是序列化 blob，直接 `json.loads` 会 `UnicodeDecodeError`。
   用 `SqliteSaver(sqlite3.connect("file:....sqlite?mode=ro", uri=True))` 走官方反序列化，
   或自己解 msgpack。
