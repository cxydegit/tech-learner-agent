# 工程记录

这个目录保存项目开发过程中的工程记录：事故复盘、评测报告、优化日志、设计说明。

它们是**当时状态的快照**，不是当前行为的文档——接口、阈值与目录结构以 `README.md` 和 `src/` 为准。

## 事故复盘

- [coach_trim-tool-accident](coach_trim-tool-accident.md) —— 上下文裁剪把 tool 回执和它的 assistant 切散，长会话再也不能用
- [llm-timeout-and-truncation-accident](llm-timeout-and-truncation-accident.md) —— collect 静默 22 分钟 + 报告撞 token 上限被硬截断
- [milestone-advance-claim-mismatch](milestone-advance-claim-mismatch.md) —— 里程碑「推进意图」与「完成声明」的语义错配（归因分析）
- [milestone-advance-claim-fix](milestone-advance-claim-fix.md) —— 同一问题的修复记录（改前 vs 改后）

## 评测与基准

- [benchmarks/reAct-vs-graph](benchmarks/reAct-vs-graph.md) —— 自建 ReAct 循环 vs 确定性管道 + 图编排（16 次/侧）
- [report/reAct-vs-graph-report](report/reAct-vs-graph-report.md) —— 上者的完整论述版
- [eval_retrieval_scenarios](eval_retrieval_scenarios.md) —— 56 条场景查询的 dense vs hybrid 检索评估
- [benchmarks/rag-p0](benchmarks/rag-p0.md) —— 25 条黄金集的早期检索评估
- [report/rag-hybrid-scenario-eval](report/rag-hybrid-scenario-eval.md) 及 [v1.1](report/rag-hybrid-scenario-eval-v1.1.md) / [v1.2](report/rag-hybrid-scenario-eval-v1.2.md) / [v1.3](report/rag-hybrid-scenario-eval-v1.3.md) —— 混合检索的迭代评估链（含一次被数据推翻的错误诊断）
- [report/note-dedup-report](report/note-dedup-report.md) —— 笔记去重：确定性确认层被合成压力测试揭穿
- [report/note-sweep-async-report](report/note-sweep-async-report.md) —— 记忆沉淀并行化的完整报告
- [report/rag-p0-p1-record](report/rag-p0-p1-record.md) —— RAG P0-P1 的实施记录

## 设计与日志

- [PROMPT_DESIGN](PROMPT_DESIGN.md) —— 提示词设计准则
- [memory_plan](memory_plan.md) —— 记忆系统设计
- [OPTIMIZATION_LOG](OPTIMIZATION_LOG.md) —— 追加式优化日志

## 关于复跑

评测与阈值标定脚本（`eval_retrieval.py`、`calibrate_inject_threshold.py`、`gen_dedup_synth.py` 等）**未随仓库分发**——它们绑定了开发者个人的知识库语料，单独分发没有意义。

因此文中出现的运行命令无法直接执行，**所有数字都是当时实测的记录**。结论与参数已经落在 `src/` 里（阈值、超时、切块参数都带实测依据的注释），但数字本身不可复现，阅读时请按「历史记录」看待。

## 隐私说明

公开前做过一轮脱敏：个人知识库的笔记路径替换为匿名标签（`笔记 A`～`笔记 V`），真实会话 ID 替换为占位符。原始内容保留在本地 `docs/internal/`（未随仓库分发）。
