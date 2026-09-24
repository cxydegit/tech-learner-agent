# 工程记录

这个目录保存项目开发过程中的工程记录：事故复盘、评测报告、优化日志、设计说明。

它们是**当时状态的快照**，不是当前行为的文档——接口、阈值与目录结构以 `README.md` 和 `src/` 为准。



## 评测与基准

- [benchmarks/reAct-vs-graph](benchmarks/reAct-vs-graph.md) —— 自建 ReAct 循环 vs 确定性管道 + 图编排（16 次/侧）
- [report/reAct-vs-graph-report](report/reAct-vs-graph-report.md) —— 上者的完整论述版
- [eval_retrieval_scenarios](eval_retrieval_scenarios.md) —— 56 条场景查询的 dense vs hybrid 检索评估
- [benchmarks/rag-p0](benchmarks/rag-p0.md) —— 25 条黄金集的早期检索评估
- [report/rag-hybrid-scenario-eval](report/rag-hybrid-scenario-eval.md) 及 [v1.1](report/rag-hybrid-scenario-eval-v1.1.md) / [v1.2](report/rag-hybrid-scenario-eval-v1.2.md) / [v1.3](report/rag-hybrid-scenario-eval-v1.3.md) —— 混合检索的迭代评估链（含一次被数据推翻的错误诊断）
- [report/note-dedup-report](report/note-dedup-report.md) —— 笔记去重：确定性确认层被合成压力测试揭穿
- [report/note-sweep-async-report](report/note-sweep-async-report.md) —— 记忆沉淀并行化的完整报告
- [report/rag-p0-p1-record](report/rag-p0-p1-record.md) —— RAG P0-P1 的实施记录




