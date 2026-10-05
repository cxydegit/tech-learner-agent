# 文档索引

`docs/` 下全部公开文档的索引。

这些文档是**当时状态的快照**，不是当前行为的文档——接口、阈值与目录结构以根 `README.md` 和 `src/` 为准。
其中的数字与结论都是**当时实测的历史记录**，评测与标定脚本未随仓库分发。

---

## 规范与流程

- [PROMPT_DESIGN.md](PROMPT_DESIGN.md) —— 提示词优化思路。**设计/修改任何新提示词前必读**（根 CLAUDE.md 已列为高危项）
- [OPTIMIZATION_LOG.md](OPTIMIZATION_LOG.md) —— 体验优化日志，追加式维护，每条为「问题 → 改动 → 效果」

## 事故复盘

不按阶段读，按**你要改的地方**读。

- [incidents/llm-timeout-and-truncation.md](incidents/llm-timeout-and-truncation.md) —— collect 没有业务超时静默 22 分钟，资料报告撞 4096 被硬截断（2026-09-09）
- [incidents/coach-trim-tool.md](incidents/coach-trim-tool.md) —— 上下文裁剪按「消息条数」切，把 tool 回执与配对的 assistant 切散，长会话不可用（2026-09-10）

## 里程碑判定

同一件事的两份：先看归因，再看改法。

- [milestone-advance-claim-mismatch.md](milestone-advance-claim-mismatch.md) —— 「推进意图」与「完成声明」的语义错配分析（2026-09-16，仅归因，未改代码）
- [milestone-advance-claim-fix.md](milestone-advance-claim-fix.md) —— 上者的原逻辑 / 改后逻辑对照（2026-09-18，已实施并全量测试通过）

## 评测报告

- [report/reAct-vs-graph-report.md](report/reAct-vs-graph-report.md) —— 为什么核心流程用「确定性管道 + LangGraph 编排」而不是 ReAct 自主编排
- [report/rag-hybrid-scenario-eval.md](report/rag-hybrid-scenario-eval.md) —— 混合检索量化评估 v1.0：纯评估，发现 3 个问题
- [report/rag-hybrid-scenario-eval-v1.1.md](report/rag-hybrid-scenario-eval-v1.1.md) —— v1.1：分词点号不拆词 + coach 注入门槛换分数
- [report/rag-hybrid-scenario-eval-v1.2.md](report/rag-hybrid-scenario-eval-v1.2.md) —— v1.2：更正 v1.1 的错误诊断 + 零分填充修复
- [report/rag-hybrid-scenario-eval-v1.3.md](report/rag-hybrid-scenario-eval-v1.3.md) —— v1.3：词法一致性软重排（轻量 rerank）
- [report/note-dedup-report.md](report/note-dedup-report.md) —— 笔记去重：确定性确认层被合成压力测试揭穿 → LLM 判定 + 用户确认兜底
- [report/note-sweep-async-report.md](report/note-sweep-async-report.md) —— 记忆沉淀并行化：同步阻塞 → 后台并行 + 确定性反馈 / 确认
- [report/rag-p0-p1-record.md](report/rag-p0-p1-record.md) —— RAG 优化 P0–P1 实施记录（长会话被压缩前的决策依据）

## 基准数据

原始数字，配套论述在上面的评测报告里。

- [benchmarks/rag-p0.md](benchmarks/rag-p0.md) —— 25 条黄金集（含 5 条 hard）的早期检索评估（2026-08-15）
- [benchmarks/eval_retrieval_scenarios.md](benchmarks/eval_retrieval_scenarios.md) —— 56 条场景查询的 dense vs hybrid 检索评估（2026-08-26）
- [benchmarks/reAct-vs-graph.md](benchmarks/reAct-vs-graph.md) —— ReAct 循环 vs 确定性管道 + 图编排基准（2026-08-14，16 次/侧）
