/* ============================================================
   原型 mock 数据 —— 纯前端演示用，后续接入真实后端时移除
   ============================================================ */

/* 场景卡片配置（与 PLAN_FEATURES A1 的契约一致） */
const CARDS = [
  {
    cmd: "collect", icon: "📚", label: "学习新技术", desc: "搜集资料清单",
    accent: "#4A7A78", soft: "#E6EFED",
    fields: [
      { name: "tech", label: "技术名", ph: "如 FastAPI", req: true },
      { name: "focus", label: "关注点", ph: "如 异步编程（可选）", req: false },
    ],
    btn: "开始收集",
  },
  {
    cmd: "read", icon: "📖", label: "解读文档", desc: "读懂一篇文档",
    accent: "#8A6B4F", soft: "#F2EAE1",
    fields: [
      { name: "url", label: "链接", ph: "https://…", req: true },
    ],
    btn: "开始解读",
  },
  {
    cmd: "ask", icon: "💬", label: "问我的笔记", desc: "联想检索回答",
    accent: "#4A628A", soft: "#E7ECF4",
    fields: [
      { name: "question", label: "问题", ph: "如 笔记里提到过异步和协程吗？", req: true },
    ],
    btn: "提问",
  },
  {
    cmd: "route", icon: "🧭", label: "定制路线", desc: "敬请期待",
    accent: "#A0A69B", soft: "#EEEFEA", disabled: true, fields: [], btn: "",
  },
];

/* 校验文案（与 domain/card_input.py 保持一致） */
const VALIDATE_TEXT = {
  collect: "请输入技术名",
  read: "请输入链接",
  ask: "请输入问题",
};

/* 静态 mock 文档（阅读器用） */
const MOCK_DOCS = {
  "docs/fastapi-materials.md": {
    type: "资料", title: "FastAPI 异步编程 · 资料清单",
    content: `# FastAPI 异步编程 · 学习资料清单

> 生成时间：2026-08-13 10:31 · 关注点：异步编程

## 核心必读资源

| 优先级 | 资料名称 | 来源 | 链接 | 为什么推荐 |
| ------ | ------- | ---- | ---- | ---------- |
| ★★★ | FastAPI 官方文档 · 异步 | 官方 | [fastapi.tiangolo.com](https://fastapi.tiangolo.com/async/) | 权威、示例可运行 |
| ★★★ | 理解 Python 的 async/await | Python 官方 | [docs.python.org](https://docs.python.org/3/library/asyncio.html) | 语言层原理 |
| ★★ | FastAPI + SQLAlchemy 异步实战 | GitHub | [github.com/…/async-sqlalchemy](https://github.com/) | 可运行示例项目 |

## 扩展阅读

- Uvicorn 的并发模型与事件循环
- 同步函数用 \`def\` 与异步用 \`async def\` 的取舍

## 学习路线建议

1. 先读官方文档 async 章节 → 2. 用 async SQLAlchemy 写一个 CRUD → 3. 压测对比同步/异步吞吐`,
  },
  "docs/fastapi-report.md": {
    type: "报告", title: "FastAPI 异步支持 · 解读报告",
    content: `# FastAPI 异步支持解读报告

> 原文链接：https://fastapi.tiangolo.com/async/
> 解读时间：2026-08-13 10:34

## 核心概念

FastAPI 会**自动判断**你的路径函数是同步还是异步：

\`\`\`python
@app.get("/")            # 同步 → 放到线程池执行
def read_sync(): ...

@app.get("/async")       # 异步 → 直接跑在事件循环
async def read_async(): ...
\`\`\`

## 关键结论

- 需要 I/O 等待用 \`async def\`；CPU 密集仍用普通 \`def\`（避免阻塞事件循环）。
- **不要**在异步函数里调用阻塞库（如 requests），要改用 httpx 异步版。
- 并发受益点：同时等待多个外部请求（并发 fetch），吞吐显著提升。

## 待深入

- 事件循环调度细节、uvloop 加速`,
  },
  "docs/async-note.md": {
    type: "笔记", title: "2026-08-13 · 异步与协程",
    content: `# 异步与协程

## 要点

- **协程**是语法层面的"可暂停函数"，\`async def\` 声明、\`await\` 挂起。
- **事件循环**是调度器：一个线程轮流跑多个协程。
- 异步 ≠ 多线程并行，是**单线程协作式并发**。

## 易错

- \`time.sleep\` 会阻塞事件循环，应换 \`asyncio.sleep\`。
- 协程不会自动执行，必须被 await 或交给 \`asyncio.run\`。

## 一句话

"遇到 I/O 就 await，把让出的时间片交给别人。"`,
  },
  "docs/spring-materials.md": {
    type: "资料", title: "Spring Boot 3 · 学习资料清单",
    content: `# Spring Boot 3 · 学习资料清单

> 生成时间：2026-08-12 20:15

## 核心必读资源

| 优先级 | 资料名称 | 来源 | 链接 |
| ------ | ------- | ---- | ---- |
| ★★★ | Spring Boot 官方文档 | 官方 | [spring.io](https://spring.io/guides) |
| ★★ | Spring Boot Reference | 官方 | [docs.spring.io](https://docs.spring.io/spring-boot/index.html) |

## 学习路线

1. 起步依赖 → 2. 自动配置 → 3. Starter 与 Actuator → 4. 部署`,
  },
};

/* 静态 mock 会话 */
const MOCK_SESSIONS = [
  {
    id: "s1",
    title: "FastAPI 异步编程",
    tech: "FastAPI",
    updatedAt: "今天 10:30",
    messages: [
      { role: "user", type: "collect", content: "FastAPI 异步编程", ts: "10:30" },
      {
        role: "assistant", type: "collect", ts: "10:31",
        doc: "docs/fastapi-materials.md",
        content: `# FastAPI 异步编程 · 学习资料清单

> 生成时间：2026-08-13 10:31 · 关注点：异步编程

## 核心必读资源

| 优先级 | 资料名称 | 来源 | 链接 | 为什么推荐 |
| ------ | ------- | ---- | ---- | ---------- |
| ★★★ | FastAPI 官方文档 · 异步 | 官方 | [fastapi.tiangolo.com](https://fastapi.tiangolo.com/async/) | 权威、示例可运行 |
| ★★★ | 理解 Python 的 async/await | Python 官方 | [docs.python.org](https://docs.python.org/3/library/asyncio.html) | 语言层原理 |
| ★★ | FastAPI + SQLAlchemy 异步实战 | GitHub | [github.com/…](https://github.com/) | 可运行示例项目 |

共收集到 **6 条去重资源**。完整清单见右侧文档。`,
      },
      { role: "user", type: "read", content: "https://fastapi.tiangolo.com/async/", ts: "10:32" },
      {
        role: "assistant", type: "read", ts: "10:34",
        doc: "docs/fastapi-report.md",
        content: `# FastAPI 异步支持解读报告

> 原文链接：https://fastapi.tiangolo.com/async/

## 核心概念

FastAPI 会**自动判断**路径函数是同步还是异步，同步函数放进线程池、异步函数直接跑在事件循环上。

## 关键结论

- 需要 I/O 等待 → \`async def\`；CPU 密集 → 普通 \`def\`。
- 不要在异步函数里用 requests，换 httpx 异步版。
- 并发受益点：同时等待多个外部请求时吞吐显著提升。`,
      },
      { role: "user", type: "ask", content: "我之前笔记里提到过异步和协程的区别吗？", ts: "10:35" },
      {
        role: "assistant", type: "ask", ts: "10:36",
        doc: "docs/async-note.md",
        content: `笔记里记录的是**概念分工**，和你问的"区别"略有不同：

## 答案

- **协程**是语法层面的"可暂停函数"（\`async def\` + \`await\`）。
- **异步**是编程范式：把协程交给**事件循环**调度，实现单线程协作式并发。
- 一句话：协程是工具，异步是用法；异步 ≠ 多线程并行。

> 来源：[2026-08-13 · 异步与协程](docs/async-note.md)，相关度 0.71

笔记里没有记录的部分（如 uvloop 加速细节）我已如实标注，未编造。`,
      },
    ],
  },
  {
    id: "s2",
    title: "Spring Boot 3",
    tech: "Spring Boot",
    updatedAt: "昨天 20:15",
    messages: [
      { role: "user", type: "collect", content: "Spring Boot 3", ts: "20:12" },
      {
        role: "assistant", type: "collect", ts: "20:15",
        doc: "docs/spring-materials.md",
        content: `# Spring Boot 3 · 学习资料清单

> 生成时间：2026-08-12 20:15

## 核心必读资源

| 优先级 | 资料名称 | 来源 | 链接 |
| ------ | ------- | ---- | ---- |
| ★★★ | Spring Boot 官方指南 | 官方 | [spring.io](https://spring.io/guides) |
| ★★ | Spring Boot Reference | 官方 | [docs.spring.io](https://docs.spring.io/spring-boot/index.html) |

共收集到 **4 条去重资源**。完整清单见右侧文档。`,
      },
    ],
  },
  {
    id: "s3",
    title: "Redis 持久化",
    tech: "Redis",
    updatedAt: "08-11 09:40",
    messages: [
      { role: "user", type: "ask", content: "Redis 的持久化机制有哪几种？", ts: "09:40" },
      {
        role: "assistant", type: "ask", ts: "09:42",
        content: `笔记中记录的持久化机制主要有两种：

## RDB 快照

- 按时间点把全量数据 dump 到磁盘，文件小、恢复快，但两次快照间的数据可能丢失。

## AOF 追加日志

- 记录每一条写命令，数据更安全，但文件大、恢复慢；可配置 fsync 策略。

> 来源：knowledge/redis/2026-08-10-持久化选型决策.md，相关度 0.66`,
      },
    ],
  },
  {
    id: "s4",
    title: "RAG 与向量检索",
    tech: "RAG",
    updatedAt: "08-10 12:28",
    messages: [
      { role: "user", type: "collect", content: "RAG 入门", ts: "12:20" },
      {
        role: "assistant", type: "collect", ts: "12:28",
        content: `# RAG 学习资料清单

## 核心必读

| 优先级 | 资料名称 | 来源 |
| ------ | ------- | ---- |
| ★★★ | 检索增强生成：综述 | arXiv |
| ★★ | LangChain 官方 RAG 教程 | LangChain |

共收集到 **5 条去重资源**。`,
      },
    ],
  },
];
