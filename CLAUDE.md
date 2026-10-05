# Tech Learner Agent

## 开发文档地图

完整文档索引见 `docs/INDEX.md`。这里只列「改错了会出事故」的几处。

| 你要改的 | 先读 |
|---|---|
| LLM 调用 / 超时 / max_tokens | `docs/incidents/llm-timeout-and-truncation.md` |
| coach 上下文裁剪 / 压缩 | `docs/incidents/coach-trim-tool.md` |
| route / exit_intent / 里程碑判定 | `docs/milestone-advance-claim-fix.md` |
| 检索排序 | `docs/report/rag-hybrid-scenario-eval-v1.3.md` |
| 笔记去重 / 合并 | `docs/report/note-dedup-report.md` |
| 编排架构（要不要上 ReAct） | `docs/report/reAct-vs-graph-report.md` |
| 写 / 改任何提示词 | `docs/PROMPT_DESIGN.md` |




## 分层架构与开发规范（后续开发必须遵守）

### 分层与依赖方向（禁止向上 import、禁止循环依赖）

```
cli.py → graph.py → pipelines/ → adapters/ → domain/
config.py 为最底层，被所有层引用；pipelines 可直接依赖 domain。
```

各层职责与落层规则：
- **domain/**：纯业务规则，零 I/O、零框架依赖。新增纯函数/解析器放这里，**必配单测**（chunking/dedup/extraction 先例）。
- **adapters/**：封装所有外部 I/O（LLM、搜索、抓取、向量库、文件）。domain **绝不**反向依赖它。
- **pipelines/**：确定性业务管道，prompts 就近存放。**保持纯**：不 print、不交互、无副作用，只返回数据；交互（`input()`/中断确认）一律放 cli.py / graph.py。
- **graph.py**：LangGraph 编排，节点只返回 `last_output`，不做渲染。
- **cli.py**：接口层，只管「解析 → 调管道/图 → 渲染」。
- **baselines/**：ReAct 基线冻结（benchmark 用），**主流程绝不 import**。

### 关键不变量 I1

`import src.cli` 不得把 `chromadb`/`langgraph` 放入 `sys.modules`。新增重依赖一律**函数内 lazy import**，禁止模块顶层引入。

### 其他铁律

- 改动分块逻辑必须递增 `domain/chunking.py` 的 `CHUNKER_VERSION`（版本变更自动全量重切 RAG 索引）。
- 新增配置先加到 `config.py`（环境变量），禁止各模块硬编码。
- LLM 输出解析统一复用 `domain/extraction.py`，不要自造解析器。

## 开发过程规定（**必须遵守**）
### 1.按照计划完成每个阶段的开发后，测试分两类处理：
- **自动化测试（不涉及 LLM 调用花费、无需人工验证效果，如 tests/ 下的单测、零网络测试）：由我直接运行**，跑通后再交付；
- **需要人工验证效果或会产生 LLM 调用花费的测试（真实对话、真实检索、提示词效果评审等）：告知用户代码改动和验证方法**，由用户决定手动验证还是委托给我验证。
### 2.写完代码之后，没有用户的提交指令，不要自动提交git。
## 文档规范
**1.绝不随意覆盖一个（尤其是.claude/plans/ 路径下的计划文档）未被用户确认/弃用的计划文件——要复用前必须先问，或先把旧内容安全落档。**
**2.报告/记录类文档（如 docs/report/）更新一律新建版本文件（`xxx-v1.1.md`），禁止覆盖旧版；旧版保留。**（教训：2026-08-25 把混合检索评估报告第 3 版覆盖到已交付的第 2 版上，旧报告丢失、被迫恢复重建。更新前先确认旧版是否可弃，或先落档旧内容。）



