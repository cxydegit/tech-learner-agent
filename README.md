# Tech Learner Agent

技术学习陪练 Agent —— 帮你省时间的资料收集 + 文档解读 + 笔记整理工具。

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
- `ANTHROPIC_API_KEY` — 从 [console.anthropic.com](https://console.anthropic.com) 获取
- `TAVILY_API_KEY` — 从 [tavily.com](https://tavily.com) 获取
- `FIRECRAWL_API_KEY` — 从 [firecrawl.dev](https://firecrawl.dev) 获取

### 3. 开始使用

```bash
# 收集学习资料
python -m src.cli collect "Spring Boot 3"

# 解读技术文档
python -m src.cli read "https://docs.spring.io/spring-boot/documentation/"

# 整理学习笔记（从文件）
python -m src.cli note "Spring Boot" -f materials/spring-boot-3-materials.md

# 整理学习笔记（从管道输入）
cat conversation.txt | python -m src.cli note "FastAPI"
```

## 功能模块

| 命令 | 功能 | 输出 |
|------|------|------|
| `collect <技术名>` | 搜索并筛选学习资料 | `materials/<技术>-materials.md` |
| `read <URL>` | 解读技术文档 | `reports/<标题>-解读.md` |
| `note <技术名>` | 提取知识点到知识库 | `knowledge/<技术>/<日期>-<主题>.md` |

## 项目结构

```
tech-learner-agent/
├── src/
│   ├── cli.py          # 命令行入口
│   ├── agent.py        # ReAct Agent 核心循环
│   ├── tools.py        # 工具函数（搜索/抓取/文件）
│   ├── prompts.py      # 系统提示词
│   ├── storage.py      # 知识库管理
│   └── config.py       # 配置管理
├── materials/          # 资料收集输出
├── reports/            # 文档解读报告
├── knowledge/          # 个人知识库
│   └── INDEX.md
├── docs/
│   └── PRD.md          # 产品需求文档
├── requirements.txt
├── .env.example
└── README.md
```

## 技术栈

- **语言**: Python 3.11+
- **LLM**: Claude API (Anthropic)
- **Agent 循环**: 自建 ReAct Loop
- **搜索**: Tavily API
- **网页抓取**: Firecrawl
- **知识存储**: Markdown 文件
- **CLI**: Click + Rich