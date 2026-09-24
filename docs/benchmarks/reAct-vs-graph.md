# Benchmark 结果：ReAct Loop (agentic) vs Graph 确定性管道

> 运行时间：2026-08-14 18:19
> 脚本未随仓库分发；以下为 2026-08-14 的历史记录，完整论述见 [report/reAct-vs-graph-report.md](../report/reAct-vs-graph-report.md)。

## 成功率判定

- 产物 = graph 侧 collect 报告文本 / agent 侧 Final Answer 文本
- 成功 = 产物非空 且 含 >= 2 个 http(s) 链接（纯规则，无额外 LLM 调用）

## 结果

| 任务 | 实现 | 次数 | 成功率 | 平均耗时(s) | 平均LLM调用 | 平均工具调用 | 平均token(入/出) |
|------|------|-----:|------:|------:|------:|------:|------:|
| collect-fastapi | agent | 8 | 100% | 42.0 | 3.1 | 2.1 | 12971/3056 |
| collect-fastapi | graph 确定性 | 8 | 100% | 45.6 | 1.0 | 9.0 | 6421/4027 |
| collect-springboot3 | agent | 8 | 100% | 130.6 | 3.5 | 2.2 | 18496/4440 |
| collect-springboot3 | graph 确定性 | 8 | 100% | 108.3 | 1.0 | 8.9 | 5374/4277 |

| 合计 | 实现 | 次数 | 成功率 | 平均耗时(s) | 平均LLM调用 | 平均工具调用 | 平均token(入/出) |
|------|------|-----:|------:|------:|------:|------:|------:|
| 合计 | agent | 16 | 100% | 86.3 | 3.3 | 2.2 | 15734/3748 |
| 合计 | graph 确定性 | 16 | 100% | 76.9 | 1.0 | 8.9 | 5898/4152 |

## 分布（中位数 [min~max]）

耗时与 token 受网络 + LLM 抖动影响，均值易被长尾带偏，附中位数与极差。N<5 时只判断量级方向。

| 任务 | 实现 | 耗时s 中位[min~max] | LLM调用 中位[min~max] | 工具调用 中位[min~max] | token总数 中位[min~max] |
|------|------|------:|------:|------:|------:|
| collect-fastapi | agent | 44.6 [20~60] | 3 [2~5] | 2 [1~4] | 14562 [5559~30308] |
| collect-fastapi | graph 确定性 | 45.0 [35~55] | 1 [1~1] | 9 [9~9] | 10307 [9651~12110] |
| collect-springboot3 | agent | 49.5 [23~704] | 3 [2~6] | 2 [1~4] | 18636 [6882~57997] |
| collect-springboot3 | graph 确定性 | 88.0 [84~238] | 1 [1~1] | 9 [8~10] | 9442 [9022~11125] |

## 说明

- agent 侧走 `baselines/react_agent.py` 冻结基线（`AGENT_USE_FUNCTION_CALLING` 依 `.env`，默认 false 文本解析）。
- graph 侧走生产 `src/graph.py` 的 LangGraph 图（InMemorySaver），`collect_node` → `collect_pipeline`。
- 工具调用数：graph 侧为 搜索(3) + 抓取(≤5) + 保存(1) 的固定组合；agent 侧由模型自主决定。GitHub star 查询不计入。
- 两侧共用同一 LLM 入口，token 成本为 `usage` 累计（含循环历史累积）。
- 成功率是二项分布，N 小时置信区间宽；「graph≈100% vs agent 明显更低」的量级差异 N≥5 即可看出方向。
