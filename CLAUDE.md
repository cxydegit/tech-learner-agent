# Tech Learner Agent

## 项目概述

本项目是一个基于 ReAct 模式的智能学习陪练 Agent，旨在帮助开发者系统性地学习新技术。核心流程为：资料收集（Search）→ 文档解读（Read）→ 知识沉淀（Note）。



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

  



