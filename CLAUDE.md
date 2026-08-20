# Tech Learner Agent

## 项目概述

本项目是一个基于 ReAct 模式的智能学习陪练 Agent，旨在帮助开发者系统性地学习新技术。核心流程为：资料收集（Search）→ 文档解读（Read）→ 知识沉淀（Note）。

## 开发文档地图（对应阶段必读）


| 文档 | 内容 | 对应阶段                           |
|------|------|--------------------------------| 
| `docs/PROMPT_DESIGN.md` | 提示词优化思路 | **设计/修改任何新提示词前必读** |


## 核心架构与约定

### 1. LLM 调用规范（⚠️ 极其重要！）

- **底层大模型**：本项目**不**使用 Anthropic (Claude) 官方 API，而是使用**阿里云百炼 (DashScope)** 提供的 OpenAI 兼容接口。
- **Python 依赖**：必须使用 `openai` 库（版本 \>= 1.0.0），**严禁**导入或使用 `anthropic` 库。
- **客户端初始化**：

  `python`

  from openai import OpenAI

  client = OpenAI(

      api*key=config.OPENAI*API*KEY,*

      base*url=config.OPENAI*BASE*URL,  # 必须从环境变量读取*

  )

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
### 1.按照计划完成每个阶段的开发后，不要直接测试，而是告知用户代码改动和测试方法，由用户决定手动测试还是委托给claude code测试。
### 2.写完代码之后，没有用户的提交指令，不要自动提交git。
## 文档规范
**1.绝不随意覆盖一个（尤其是.claude/plans/ 路径下的计划文档）未被用户确认/弃用的计划文件——要复用前必须先问，或先把旧内容安全落档。**




