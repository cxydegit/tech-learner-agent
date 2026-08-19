# Tech Learner Agent

技术学习陪练 Agent —— 帮你省时间的资料收集 + 文档解读 + 笔记整理工具。

核心流程：**Search（资料收集）→ Read（文档解读）→ Note（知识沉淀）**。

## 快速开始

### 1. 安装依赖

```bash
# 创建虚拟环境
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置 API Keys

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 API Keys：
- `OPENAI_API_KEY` — 阿里云百炼 DashScope API Key（[bailian.console.aliyun.com](https://bailian.console.aliyun.com)）
- `OPENAI_BASE_URL` — DashScope OpenAI 兼容端点（`https://dashscope.aliyuncs.com/compatible-mode/v1`）
- `MODEL_NAME` — 百炼模型名（如 `glm-5`）
- `TAVILY_API_KEY` — 从 [tavily.com](https://tavily.com) 获取
- `FIRECRAWL_API_KEY` — 从 [firecrawl.dev](https://firecrawl.dev) 获取
- `GITHUB_TOKEN` — **可选**，GitHub Personal Access Token（collect 质量预筛查 star 数用，不填自动跳过；[github.com/settings/tokens](https://github.com/settings/tokens) 获取）

> 说明：本项目底层大模型走**阿里云百炼（DashScope）的 OpenAI 兼容接口**（`openai` 库），不使用 Anthropic 官方 API。

### 3. 开始使用

```bash
# 收集学习资料（可选关注点，多词直接拼）
python -m src.cli collect "Spring Boot 3"
python -m src.cli collect FastAPI 异步编程

# 解读技术文档
python -m src.cli read "https://docs.spring.io/spring-boot/documentation/"

# 整理学习笔记（从文件）
python -m src.cli note "Spring Boot" -f materials/spring-boot-3-materials.md

# 整理学习笔记（从管道输入）
cat conversation.txt | python -m src.cli note "FastAPI"

# 建立 / 增量更新 RAG 语义索引
python -m src.cli index

# 进入交互式学习会话（LangGraph 图驱动）
python -m src.cli learn
```

## 功能模块

| 命令 | 功能 | 输出 |
|------|------|------|
| `collect <技术名> [关注点]` | 搜索并筛选学习资料（有关注点则聚焦生成） | `materials/<技术>-materials-<时间>.md` |
| `read <URL>` | 解读技术文档（含 RAG 缓存复用） | `reports/<标题>-解读.md` |
| `note <技术名> [-f 文件\|-t 文本]` | 提取知识点到知识库（语义去重） | `knowledge/<技术>/<日期>-<主题>.md` |
| `index [--force]` | 建立 / 增量更新 RAG 索引 | `.chroma/` 向量库 |
| `learn [会话ID]` | 交互式学习会话（中断可恢复） | `.graph/` 状态持久化 |

## 项目结构

```
tech-learner-agent/
├── src/
│   ├── cli.py                  # ① 接口层：Click 命令 + /learn REPL + 渲染
│   ├── graph.py                # ② 编排层：LangGraph 状态机
│   ├── config.py               # ⓪ 配置层：环境变量、路径、阈值
│   ├── pipelines/              # ③ 应用层：确定性业务管道
│   │   ├── collect.py          #   资料收集 / 定向深挖管道 + 提示词
│   │   ├── read.py             #   文档解读管道 + 分类门 + 提示词
│   │   └── note.py             #   笔记提取管道 + 提示词
│   ├── domain/                 # ④ 领域层：纯业务规则（零 I/O，可独立单测）
│   │   ├── chunking.py         #   Markdown 感知切块（chunker v2）
│   │   ├── dedup.py            #   文件名清洗 / 主题重叠 / 笔记头
│   │   └── extraction.py       #   LLM 输出解析（JSON 提取 / 列表 / 分类）
│   ├── adapters/               # ⑤ 基础设施层：外部 I/O
│   │   ├── llm.py              #   LLM 调用 + 时间标签注入
│   │   ├── search.py           #   Tavily 搜索
│   │   ├── fetch.py            #   Firecrawl 抓取
│   │   ├── embedding.py        #   DashScope 向量化
│   │   ├── vector.py           #   Chroma 向量库（索引/检索/读缓存）
│   │   └── store.py            #   知识库文件存储 + 文件读写工具
│   └── baselines/              # 研究层：ReAct 基线（主流程不 import）
│       └── react_agent.py      #   旧 ReAct Agent 冻结副本（benchmark 用）
├── materials/                  # 资料收集输出
├── reports/                    # 文档解读报告
├── knowledge/                  # 个人知识库（含 INDEX.md）
├── .chroma/                    # Chroma 向量库持久化（运行时生成）
├── .graph/                     # LangGraph checkpointer sqlite（运行时生成）
├── tests/                      # 单测（domain 纯函数 + 分块器）
├── docs/                       # 设计文档（PRD / 优化计划 / 风险决策）
├── requirements.txt
├── .env.example
└── README.md
```



## 技术栈

- **语言**: Python 3.11+
- **LLM**: 阿里云百炼 DashScope（OpenAI 兼容接口，`openai` 库；模型由 `MODEL_NAME` 配置）
- **向量化**: DashScope `text-embedding-v3`（单请求批量上限 10）
- **编排**: LangGraph + SqliteSaver checkpointer
- **语义检索**: Chroma（本地 `.chroma/`，cosine 度量）
- **搜索**: Tavily API
- **网页抓取**: Firecrawl
- **知识存储**: Markdown 文件 + 语义索引
- **CLI**: Click + Rich
