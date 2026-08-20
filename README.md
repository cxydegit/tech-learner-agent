# Tech Learner Agent

技术学习陪练 Agent —— 帮你省时间的资料收集 + 文档解读 + 笔记整理工具。

核心流程：**Search（资料收集）→ Read（文档解读）→ Note（知识沉淀）**。

项目提供**两种访问方式**，共用同一套图状态与知识库，行为一致：

| 方式 | 入口 | 适合场景                                         |
|------|------|----------------------------------------------|
| **CLI 命令行** | `python -m src.cli <命令>` | 单步快速执行（收集 / 解读 / 沉淀 / 建索引），或 `learn` 交互式学习会话 |
| **Web 前端** | `python -m src.web.server` 后浏览器访问 | 图形界面：会话管理、对话流、文档阅读器、资料库浏览、一键知识沉淀             |

---

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

---

## 使用方式一：CLI 命令行

```bash
# 收集学习资料（可选关注点，多词直接拼）
python -m src.cli collect "Spring Boot 3"
python -m src.cli collect FastAPI 异步编程

# 解读技术文档
python -m src.cli read "https://docs.spring.io/spring-boot/documentation/"

# 整理学习笔记
note 有三种输入方式：
1. -f 文件 — 从本地文件读取；
python -m src.cli note "Spring Boot" -f materials/spring-boot-3-materials.md
2. -t 文本 — 直接给文本；
3. 都没有 → 从 stdin 读取，如：
cat conversation.txt | python -m src.cli note "FastAPI"
这条命令把 conversation.txt 的内容通过管道喂给 CLI 的 note 命令，作为「学习内容」进行知识沉淀。(conversation.txt 通常是一段学习对话/笔记文本,比如和 AI 的问答记录、复制粘贴的讲解）

# 建立 / 增量更新 RAG 语义索引
python -m src.cli index

# 进入交互式学习会话（LangGraph 图驱动，中断可恢复）
python -m src.cli learn
```

| 命令 | 功能 | 输出 |
|------|------|------|
| `collect <技术名> [关注点]` | 搜索并筛选学习资料（质量预筛 + GitHub star 加分，有关注点则聚焦生成） | `materials/<技术>-materials-<时间>.md` |
| `read <URL>` | 解读技术文档（含 RAG 缓存复用） | `reports/<标题>-解读.md` |
| `note <技术名> [-f 文件\|-t 文本]` | 提取知识点到知识库（语义去重，相似笔记交你确认合并/跳过） | `knowledge/<技术>/<日期>-<主题>.md` |
| `index [--force]` | 建立 / 增量更新 RAG 索引（含孤儿分块对账） | `.chroma/` 向量库 |
| `learn [会话ID]` | 交互式学习会话：`collect` / `read` / `note` / `ask` 全流程，中断可恢复 | `.graph/` 状态持久化 |

---

## 使用方式二：Web 前端

```bash
python -m src.web.server
# 打开浏览器访问 http://127.0.0.1:8000 （默认只绑本机，个人工具不进局域网/公网）
```

### 功能

- **会话管理**：左侧会话列表，支持新建 / 切换 / 删除；历史会话经 LangGraph checkpointer 持久化，关闭刷新不丢。
- **对话流**：中间对话区 + 四张场景卡片——
  - 📚 **学习新技术**（collect）：填写技术名与关注点，收集资料清单；
  - 📖 **解读文档**（read）：粘贴链接，生成解读报告，消息里带「阅读全文」chip；
  - 💬 **问我的笔记**（ask）：跨笔记联想检索，答案内联标注来源笔记；
  - 🧭 定制路线：敬请期待（暂未开放）。
- **实时进度**：collect / read 长任务经 SSE 推送流式进度，对话区实时展示执行过程。
- **一键沉淀（note）**：read 完成提醒区出现「📝 一键沉淀」入口；遇到与已有笔记相似的候选，弹出决策面板（**全部合并 / 输入编号逐条 / 全部跳过**），确认后差量合并入库。
- **后台任务不阻塞**：切走会话不会中断正在跑的任务；note 待确认的合并决策跨会话 / 刷新保留，切回来恢复决策面板继续确认。
- **资料库浏览**：顶部入口按 `materials`（资料）/ `reports`（解读报告）/ `knowledge`（知识笔记）三类浏览全部文档。
- **文档阅读器**：点击「阅读全文」或资料库条目打开右侧阅读器（Markdown 渲染），打开时左侧会话栏自动隐藏、对话与阅读器 1:1 分栏。
- **防误关**：有任务在跑 / 有待确认沉淀时关闭或刷新页面会弹系统确认框。

---

## 项目结构（代码结构）

```
src/
├── cli.py                  # CLI 入口：Click 命令 + /learn REPL + 渲染
├── graph.py                # 编排层：LangGraph 状态机（collect/read/qa + note 两段式）
├── config.py               # 配置层：环境变量、路径、阈值
├── web/                    # Web 服务：FastAPI + 原生模块化 SPA（零构建链）
│   ├── server.py           #   FastAPI 应用 + /api 端点 + SSE 流
│   ├── sessions.py         #   会话列表 / 详情 / 删除（读 SqliteSaver checkpoint）
│   ├── runner.py           #   图后台执行线程 + 事件队列（run / resume / job_info）
│   ├── docs.py             #   资料/报告/笔记文件浏览 API（路径白名单防穿越）
│   └── static/             #   前端 SPA：index.html + css/ + js/
│       ├── js/main.js      #     入口：初始化 + hash 路由 + beforeunload
│       ├── js/store.js     #     共享状态 + 发布订阅
│       ├── js/api.js       #     fetch + EventSource 封装
│       ├── js/cards.js     #     场景卡片 + 表单校验
│       ├── js/markdown.js  #     轻量 markdown 渲染器（零依赖）
│       ├── js/router.js    #     hash 路由：#/chat/:id、#/docs/:type
│       └── js/views/       #     对话流 chat / 阅读器 reader / 资料库 docs
├── pipelines/              # 应用层：确定性业务管道（纯数据，无 I/O 副作用）
│   ├── collect.py          #   资料收集 / 定向深挖管道 + 提示词
│   ├── read.py             #   文档解读管道 + 分类门 + 提示词
│   ├── note.py             #   笔记差量提取 + 合并确认 + 入库管道 + 提示词
│   └── qa.py               #   联想检索问答管道 + 提示词
├── domain/                 # 领域层：纯业务规则（零 I/O、零框架，可独立单测）
│   ├── chunking.py         #   Markdown 感知切块（版本变更自动全量重切索引）
│   ├── dedup.py            #   文件名清洗 / 主题重叠 / 笔记头 / 去重判定
│   ├── extraction.py       #   LLM 输出解析（JSON 提取 / 列表 / 分类）
│   └── card_input.py       #   场景卡片输入解析与校验（CLI / Web 共用契约）
├── adapters/               # 基础设施层：外部 I/O（domain 绝不反向依赖）
│   ├── llm.py              #   LLM 调用（DashScope OpenAI 兼容）+ 时间标签注入
│   ├── search.py           #   Tavily 搜索
│   ├── fetch.py            #   Firecrawl 抓取
│   ├── embedding.py        #   DashScope 向量化
│   ├── vector.py           #   Chroma 向量库（索引 / 混合检索 / 缓存）
│   └── store.py            #   知识库文件存储 + 文件读写工具
└── baselines/              # 研究层：ReAct 基线（benchmark 用，主流程绝不 import）
    └── react_agent.py      #   旧 ReAct Agent 冻结副本
```

## 技术栈

- **语言**: Python 3.11+
- **LLM**: 阿里云百炼 DashScope（OpenAI 兼容接口，`openai` 库；模型由 `MODEL_NAME` 配置）
- **向量化**: DashScope `text-embedding-v3`（单请求批量上限 10）
- **编排**: LangGraph + SqliteSaver checkpointer（中断 / 恢复 / 跨会话持久化）
- **语义检索**: Chroma（本地 `.chroma/`，cosine 度量）+ BM25 混合检索（RRF 融合）
- **搜索**: Tavily API
- **网页抓取**: Firecrawl
- **知识存储**: Markdown 文件 + 语义索引
- **Web**: FastAPI + uvicorn，原生模块化 SPA（无 node 构建链）
- **CLI**: Click + Rich
