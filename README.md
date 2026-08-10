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

> 说明：本项目底层大模型走**阿里云百炼（DashScope）的 OpenAI 兼容接口**（`openai` 库），不使用 Anthropic 官方 API。

### 3. 开始使用

```bash
# 收集学习资料（全面，可按级别）
python -m src.cli collect "Spring Boot 3"

# 定向深挖某个方向
python -m src.cli dig "Spring Boot" 底层原理

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
| `collect <技术名> [入门\|进阶]` | 搜索并筛选学习资料（按级别） | `materials/<技术>-materials.md` |
| `dig <技术名> <方向>` | 定向深挖具体方向 | `materials/<技术>-<方向>-dig.md` |
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

## 架构分层

项目采用**依赖倒置的分层架构**，核心原则：**高层依赖低层、低层绝不向上 import、禁止循环依赖**。依赖方向为：

```
cli → graph → pipelines → adapters → domain
```

| 层 | 职责 | 关键文件 |
|----|------|---------|
| **⓪ 配置** `config.py` | 从 `.env` 加载全部配置：API Keys、LLM/嵌入模型、RAG 阈值、路径 | `config.py` |
| **① 接口** `cli.py` | 用户入口：Click 命令薄壳、`/learn` REPL、Rich 渲染、交互确认（`input()`） | `cli.py` |
| **② 编排** `graph.py` | LangGraph 状态机：节点路由、interrupt 中断/恢复、SqliteSaver 跨会话持久化 | `graph.py` |
| **③ 应用** `pipelines/` | 确定性业务管道：纯数据进出、无交互、prompts 就近存放 | `collect.py` `read.py` `note.py` |
| **④ 领域** `domain/` | 纯业务规则：零 I/O、零框架依赖，可独立单测 | `chunking.py` `dedup.py` `extraction.py` |
| **⑤ 基础设施** `adapters/` | 所有外部 I/O：LLM / 搜索 / 抓取 / 向量化 / 向量库 / 文件存储 | `llm.py` `search.py` `fetch.py` `embedding.py` `vector.py` `store.py` |
| **研究层** `baselines/` | 冻结的旧 ReAct 基线，仅作 benchmark 对比，**主流程不 import** | `react_agent.py` |

### 各层代码文件职责

**`src/config.py`（配置层）**
全局唯一配置入口，`Config` 单例 + `config` 实例。集中管理环境变量（API Keys、模型名）、路径（`materials/` `reports/` `knowledge/` `.chroma/` `.graph/`）、RAG 阈值（分块大小、去重/读缓存相似度阈值、批量上限）。`config.validate()` 在 CLI 启动时校验必要变量，`config.ensure_dirs()` 确保输出目录存在。

**`src/cli.py`（接口层）**
Click 命令薄壳，不做业务，只做「解析参数 → 调管道/图 → 渲染结果」。独立命令 `collect` / `dig` / `read` / `note` / `index` 直接调用对应管道；`learn` 进入 LangGraph 图驱动的 REPL（`/help` `/status` `/done` `/quit`）。交互确认（如 RAG 缓存复用 `_confirm_reuse`）和 `_drive` 图驱动循环也在这里——**交互性只活在接口层，管道保持纯**。

**`src/graph.py`（编排层）**
编译 LangGraph 状态机 `LearnState`（`tech`/`level`/`urls`/`visited`/`notes`/`last_output` 等字段），节点是管道的薄包装（`collect_node`/`dig_node`/`read_node`/`note_node`），`ask_level_node` 用 interrupt 实现级别交互探测。路由函数 `_route_command`/`_route_by_level` 把用户命令分发到对应节点。`open_graph()` 用 SqliteSaver 把状态持久化到 `.graph/checkpoints.sqlite`，支持跨会话恢复。

**`src/pipelines/`（应用层）**
确定性管道是业务核心，三段主流程各一个：
- `collect.py` — `collect_pipeline`（按级别搜索→抓取→单次 LLM 合成资料清单→保存 `materials/`）与 `dig_pipeline`（定向深挖变体），提示词 `COLLECT_COMPOSE_PROMPT`/`DIG_COMPOSE_PROMPT` 就近存放。
- `read.py` — `read_pipeline`：抓取 → `_classify_technical` 分类门（非技术文档拦截）→ LLM 生成解读报告 → 保存 `reports/`。
- `note.py` — `note_pipeline`（差量提取，Step 3）：召回已有笔记 → LLM 只输出**新增**知识点（可输出 `[]`）→ 逐条匹配生成 `merge_candidates` → 返回 `{new_points, merge_candidates, empty_reason, suggestion}`。入库在 `persist_points`（用户确认后调 `persist_note`）。

管道只返回数据（dict），不 print、不交互、无副作用——交互性在 `cli.py` / `graph.py` 层完成。

**`src/domain/`（领域层）**
纯业务规则，零 I/O、零框架依赖，可独立单测：
- `chunking.py` — Markdown 感知切块（chunker v2）：长表格 / 长代码围栏原子成块、标题章节前缀、`CHUNKER_VERSION` 版本号（改动分块逻辑必须递增，版本变更自动全量重切索引）。
- `dedup.py` — 文件名清洗 `sanitize_filename`、主题重叠判断 `_topics_overlap`、笔记头生成 `_with_header`、剥头部 `strip_note_header`（差量合并喂 LLM 前用）。
- `extraction.py` — LLM 输出解析：`extract_json_object`（花括号配对 + 字符串状态机）、`parse_entries` / `parse_classify`（兼容代码块包裹与夹杂文本）。

**`src/adapters/`（基础设施层）**
封装所有外部 I/O，供 pipelines / cli / baselines 调用：
- `llm.py` — `generate_text`（DashScope OpenAI 兼容调用）+ `current_time_label` / `replace_time_line`（报告生成时间注入，确定性兜底防 LLM 编造历史日期）。
- `search.py` / `fetch.py` — Tavily 搜索 / Firecrawl 网页抓取的薄封装。
- `embedding.py` — `DashScopeEmbeddingFunction`（自定义 Chroma EmbeddingFunction）+ 批量嵌入（`_BATCH_SIZE=10`，百炼单请求上限）。
- `vector.py` — Chroma 向量库全部逻辑：客户端/集合管理、`index_paths`/`index_documents` 索引（含变更检测）、`semantic_search`/`semantic_search_knowledge` 检索、`check_read_cache` 读缓存；提供 `python -m src.adapters.vector` CLI 直跑。
- `store.py` — 知识库文件存储：`persist_note`（**不再静默追加**，仅新建 / 按 `replace_path` 覆盖合并）、`find_note_match`（语义去重，返回相似度）、`recall_existing_notes`（差量上下文召回）、`update_index`（INDEX.md）、`get_existing_notes`/`get_knowledge_summary`，以及 `save_file_tool`/`read_file_tool`/`list_files_tool` 文件工具。

**`src/baselines/react_agent.py`（研究层）**
Step 2 重构前 `agent.py` 的 ReAct Agent 类逐字冻结副本（含 `REACT_SYSTEM_PROMPT`、`TOOL_REGISTRY`、`_extract_json_object`），只作 Stage 4 benchmark 的对比基线。**主流程任何代码不得 import 它**，保持与生产路径的完全隔离。

### 关键不变量 I1

`import src.cli` 不得把 `chromadb` / `langgraph` 放入 `sys.modules`（保证接口层轻量、冷启动快）。实现方式：`cli.py` 的 `index` / `_try_reuse_cached_report` / `learn` 与 `store.py` 的 `find_note_match` / `recall_existing_notes` / `_update_rag_index` 保持 **lazy import**（函数内 import，模块顶层不引入重依赖）。

### 完整调用链

各命令的逐步调用链（含具体行号）见 **`docs/PROJECT_FLOW.md`**；已踩的坑与决策见 **`docs/RISKS.md`**（开发新阶段前必读）。

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
