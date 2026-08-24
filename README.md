# Tech Learner Agent

技术学习陪练 Agent —— 帮你省时间的资料收集 + 文档解读 + 笔记整理工具。

核心流程：**Collect（资料收集）→ Read（文档解读）→ Note（知识沉淀）**。

项目提供**两种访问方式**，共用同一套图状态与知识库，行为一致：

| 方式 | 入口 | 适合场景                                         |
|------|------|----------------------------------------------|
| **CLI 命令行** | `python -m src.cli <命令>` | 单步快速执行（收集 / 解读 / 沉淀 / 建索引），或 `learn` 交互学习 / `route` 定制学习路线 |
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

# 定制化学习路线（问卷 → 生成路线 → 陪练执行，agent 多轮对话，中断可恢复）
python -m src.cli route "Spring Boot"
# 继续上次的陪练会话；已有路线时也会提示「继续 / 重新规划」
python -m src.cli route --resume
```

| 命令 | 功能 | 输出 |
|------|------|------|
| `collect <技术名> [关注点]` | 搜索并筛选学习资料（质量预筛 + GitHub star 加分，有关注点则聚焦生成） | `materials/<技术>-materials-<时间>.md` |
| `read <URL>` | 解读技术文档（含 RAG 缓存复用） | `reports/<标题>-解读.md` |
| `note <技术名> [-f 文件\|-t 文本]` | 提取知识点到知识库（语义去重，相似笔记交你确认合并/跳过） | `knowledge/<技术>/<日期>-<主题>.md` |
| `index [--force]` | 建立 / 增量更新 RAG 索引（含孤儿分块对账） | `.chroma/` 向量库 |
| `learn [会话ID]` | 交互式学习会话：`collect` / `read` / `note` / `ask` 全流程，中断可恢复 | `.graph/` 状态持久化 |
| `route <技术名> [--resume]` | 定制化学习路线：问卷收集画像 → 生成分阶段路线 → 陪练执行（agent 自主调 collect / read / note / update_roadmap），中断可恢复 | `learner/profile.json` 画像 + `roadmaps/<技术>-roadmap.md` 路线 |

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
  - 🧭 **定制路线**（route）：填技术名开始，agent 多轮问卷收集画像 → 生成分阶段学习路线 → 陪练执行（自主 collect / read / note 推进学习计划）。
- **实时进度**：collect / read 长任务经 SSE 推送流式进度，对话区实时展示执行过程。
- **一键沉淀（note）**：read 完成提醒区出现「📝 一键沉淀」入口；遇到与已有笔记相似的候选，弹出决策面板（**全部合并 / 输入编号逐条 / 全部跳过**），确认后差量合并入库。
- **后台任务不阻塞**：切走会话不会中断正在跑的任务；note 待确认的合并决策跨会话 / 刷新保留，切回来恢复决策面板继续确认。
- **资料库浏览**：顶部入口按 `materials`（资料）/ `reports`（解读报告）/ `knowledge`（知识笔记）三类浏览全部文档。
- **文档阅读器**：点击「阅读全文」或资料库条目打开右侧阅读器（Markdown 渲染），打开时左侧会话栏自动隐藏、对话与阅读器 1:1 分栏。
- **防误关**：有任务在跑 / 有待确认沉淀时关闭或刷新页面会弹系统确认框。

---

## 定制化学习路线（route）

agentic 多轮学习陪练：问卷收集画像 → 生成个性化学习路线 → 陪练执行，全程对话驱动，把 collect / read / note / ask 作为 agent 工具，由模型自主编排调用。

### 整体流程（单图三模式）

```
问卷(survey) → 路线规划(planning) → 陪练执行(coaching)
```

- **问卷（survey）**：确定性收集四类字段（自评 0-10 / 相关技术 / 学习目标 / 时间预算，格式错自动重问）+ 基于画像动态出 2-3 道诊断题，完成后推导用户画像（小白 / 开发者）。
- **路线规划（planning）**：模型调 `generate_roadmap` 生成 3-5 个阶段、每阶段含可检验里程碑的路线 → 落盘 → 呈现给你确认 / 提出修改 → 确认后进入执行。
- **陪练执行（coaching）**：agent 自主选择工具推进：collect（收集资料）/ read（解读）/ note（沉淀笔记）/ ask（问已学笔记）/ update_roadmap（勾选里程碑）；你随时可说「停 / 结束」退出。

模式切换由**确定性代码**判定（问卷完成 → planning；用户确认路线 → coaching），模型的自由度只在「对话 + 选工具」。

### 持久化与恢复

| 数据 | 位置 | 说明 |
|------|------|------|
| 用户画像 | `learner/profile.json` | 按技术归档：自评 / 目标 / 时间预算 / 画像分桶 / 路线路径 |
| 学习路线 | `roadmaps/<技术>-roadmap.md` + `.json` | Markdown 给人看可编辑，JSON 存机器态（当前阶段 / 里程碑完成） |
| 会话状态 | `.graph/checkpoints.sqlite` | 模式、画像、路线、消息全量快照，中断 / 刷新 / 重启可恢复 |

- **恢复**：`route --resume` 直接回到上次线程；新开路线时检测到已有路线，会提示「继续上次陪练 / 重新规划」。
- agent 每轮看到的路线来自**状态**（checkpointer 还原）；文件是外部归档，两者同步写入。

### 上下文管理

- 模型上下文有界：每次调用只带最近 10 轮 + 画像 / 路线上下文块。
- 消息超 40 条触发压缩：旧消息经 LLM 摘要进 `coach_summary`（叠加既有摘要），只留最近 10 轮；摘要失败降级为直接丢弃（保上下文有界）。
- 工具结果天然短（`{status, path, summary}`），大内容全部写文件，模型从不接触大文本。

### 出错恢复（六层防御）

1. 工具执行异常 → 错误回喂模型自行修正重试；
2. LLM 调用瞬时故障 → 静默重试 3 次；
3. 工具调用通道持续失败 → 去掉 tools 纯文本降级；
4. 持续失败 → interrupt 问用户方向；
5. 死循环护栏：每回合工具预算 8 次 / 连续 2 次相同调用强制打断 / 图级 recursion_limit 50；
6. 你随时可说「停 / 结束」确定性退出；standalone 的 collect / read / note 始终可用。

### 相关配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ROUTE_MAX_TOOL_CALLS_PER_TURN` | `8` | 每用户回合工具调用预算 |
| `ROUTE_RECURSION_LIMIT` | `50` | 图级执行硬上限 |
| `COACH_HISTORY_KEEP` | `10` | 每轮保留最近对话轮数 |
| `COACH_COMPRESS_AT` | `40` | 消息数压缩阈值 |
| `COACH_SUMMARY_MAX_TOKENS` | `800` | 摘要长度上限 |
| `ROUTE_FALLBACK_TO_TEXT` | `true` | 工具通道失败时纯文本降级 |

---

## 项目结构（代码结构）

```
src/
├── cli.py                  # CLI 入口：Click 命令 + /learn REPL + route 定制路线 + 渲染
├── graph.py                # 编排层：LangGraph 状态机（collect/read/qa + note 两段式 + coach 陪练循环）
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
│   ├── qa.py               #   联想检索问答管道 + 提示词
│   └── route.py            #   coach 陪练工具实现 + 三模式提示词 + 摘要压缩
├── domain/                 # 领域层：纯业务规则（零 I/O、零框架，可独立单测）
│   ├── chunking.py         #   Markdown 感知切块（版本变更自动全量重切索引）
│   ├── dedup.py            #   文件名清洗 / 主题重叠 / 笔记头 / 去重判定
│   ├── extraction.py       #   LLM 输出解析（JSON 提取 / 列表 / 分类）
│   ├── roadmap.py          #   学习路线 schema / 校验 / 里程碑推进（route 模块）
│   ├── survey.py           #   问卷解析 / 画像推导 / 完成判定（route 模块）
│   ├── exit_intent.py      #   退出意图确定性识别（route 模块）
│   └── card_input.py       #   场景卡片输入解析与校验（CLI / Web 共用契约）
├── adapters/               # 基础设施层：外部 I/O（domain 绝不反向依赖）
│   ├── llm.py              #   LLM 调用（DashScope OpenAI 兼容）+ 时间标签注入 + chat_with_tools
│   ├── search.py           #   Tavily 搜索
│   ├── fetch.py            #   Firecrawl 抓取
│   ├── embedding.py        #   DashScope 向量化
│   ├── vector.py           #   Chroma 向量库（索引 / 混合检索 / 缓存）
│   ├── learner.py          #   用户画像 / 学习路线文件读写（route 模块）
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
