"""文档解读管道：抓取 → 技术文档分类 → LLM 解读 → 保存。

自 agent.py 迁出：read_pipeline, _classify_technical + 就近携带
CLASSIFY_DOC_PROMPT / READ_SYSTEM_PROMPT。纯数据进出，不打印、不写会话、
不做"缓存复用"交互（复用决策属调用方职责）。
"""

from datetime import datetime
from typing import Callable

from ..adapters.fetch import fetch_tool
from ..adapters.llm import current_time_label, generate_text, replace_time_line
from ..adapters.store import index_file_lazy, save_file_tool
from ..domain.dedup import sanitize_filename
from ..domain.extraction import parse_classify


# ============================================================
# 提示词
# ============================================================

CLASSIFY_DOC_PROMPT = """你是一个文档类型判断助手。用户会给你一份抓取到的文档（标题 + 链接 + 内容片段），请判断它是否是"技术文档"。

## 什么是技术文档
- 官方文档 / API 参考 / 手册 / 教程 / 技术博客 / 框架或库的介绍 / 源码解读 / 工程实践
- 核心特征：目的是"教授、说明、解释技术知识或技术实现"

## 什么不算技术文档
- 非技术新闻资讯、产品营销落地页、购物、娱乐、个人生活随笔
- 抓取失败、内容为空或几乎无正文的页面
- 无法判断内容主题的页面（如纯图片页、登录墙）

## 输出要求
只输出一个 JSON 对象，不要有任何解释或 ```json 代码块标记：
{"is_technical": true|false, "reason": "一句话原因"}
"""


READ_SYSTEM_PROMPT = """你是一个专业的技术文档解读助手。你的任务是将用户提供的技术文档源内容转化为易于理解的结构化解读报告。

用户会直接给你文档的抓取内容（Markdown），你只需要基于这份内容生成报告，**不要输出 Thought / Action / Final Answer 等任何额外包裹，直接输出 Markdown 报告本身**。

## 解读报告格式
```markdown
# <文档标题> 解读报告

> 原文链接：<url>
> 解读时间：{now}
> 文档版本：<版本号>

## 一、概要
本文档讲什么？解决什么问题？（2-3 句话概括）

## 二、核心术语
| 术语 | 原文 | 本质解释 | 通俗类比 |
|------|------|---------|---------|
| ... | ... | ... | ... |

## 三、核心流程
（用 Mermaid 流程图表示关键流程）

```mermaid
flowchart TD
    A[开始] --> B[步骤1]
    ...
```

## 四、重点章节标注
- 🔴 **必读核心**：第 X 章 — 原因
- 🟡 **推荐阅读**：第 Y 章 — 原因
- 🟢 **可跳过**：第 Z 章 — 原因

## 五、最小可运行示例
**注意，如果文档中没有包含与最小可运行示例有关的内容，可跳过这个部分，输出"没有发现相关内容"**
```<语言>
// 可直接运行的代码
```
运行方式：...

## 六、踩坑预警
1. **常见错误**：...（原因 + 解决方案）
2. **版本兼容**：...

## 七、延伸阅读
- 相关文档：...
- 前置知识：...
```

## 重要原则
- 直接输出最终报告 Markdown，不要像 ReAct 那样输出 Thought / Action / Final Answer
- 术语解释要通俗易懂，用生活中的类比
- 代码示例必须完整可运行，不要省略 import 和配置
- 优先解读文档核心内容，标注内容的原文来源（章节标题）以方便对照
- 如果内容不完整或抓取失败，诚实标注"待验证"
- 如果内容过长需要取舍，优先保留核心概念、关键步骤和可运行代码
- 报告中的「解读时间」必须使用我提供的当前系统时间（{now}），不要自行推断或编造日期
"""


# ============================================================
# _classify_technical
# ============================================================

def _classify_technical(url: str, title: str, markdown: str) -> tuple[bool, str]:
    """识别文档是否为技术文档（LLM 分类门）。

    Args:
        url: 文档 URL
        title: 文档标题
        markdown: 抓取到的内容

    Returns:
        (is_technical, reason)；解析失败时默认视为技术文档（is_technical=True）避免误拦截
    """
    raw = generate_text(
        CLASSIFY_DOC_PROMPT,
        f"文档标题：{title or '未知'}\n链接：{url}\n\n"
        f"===== 内容片段 =====\n{markdown[:3000]}\n===== 内容结束 =====",
    )
    decision = parse_classify(raw)
    is_tech = str(decision.get("is_technical", "true")).strip().lower() in ("true", "1", "yes")
    reason = str(decision.get("reason", "")).strip()
    return is_tech, reason


# ============================================================
# read_pipeline
# ============================================================

def read_pipeline(url: str, progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：抓取 → 技术文档分类 → LLM 解读 → 保存 reports/。

    与 run_read 的区别：只返回数据（report / title / notes / error），
    不打印、不写会话、不做"缓存复用"交互（复用决策属调用方职责）。

    Args:
        url: 文档 URL
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"report": str, "title": str, "report_path": str, "notes": list[dict], "error": str,
         "index_ok": bool, "index_error": str | None（仅失败时）}
        - error 为空表示成功；error 非空表示失败 / 非技术文档（此时 report 为空）
        - notes 形如 [{"url", "title", "report"}]，供 LangGraph 状态累积
        - index_ok 表示报告是否已写入 RAG 索引（失败不阻断保存，缺口由对账补齐）
    """
    # 1. 抓取文档内容
    fetched = fetch_tool(url)
    if not fetched.get("markdown"):
        err = fetched.get("error") or "抓取文档内容失败，请检查 URL 是否有效。"
        return {"report": "", "title": "", "report_path": "", "notes": [], "error": f"抓取失败：{err}",
                "index_ok": True, "index_error": None}
    if progress:
        progress(f"✅ 抓取成功，内容 {len(fetched['markdown'])} 字符"
                 f"{'（已截断，仅截取片段）' if fetched.get('truncated') else ''}")

    # 1.5 技术文档识别（LLM 分类门）：非技术文档则中止，不进入解读
    if progress:
        progress("🔍 识别是否为技术文档...")
    is_technical, reason = _classify_technical(url, fetched.get("title") or "", fetched["markdown"])
    if not is_technical:
        return {
            "report": "", "title": fetched.get("title") or "", "report_path": "", "notes": [],
            "error": f"该文档似乎不是技术文档，跳过解读（{reason or '未提供原因'}）",
            "index_ok": True, "index_error": None,
        }

    # 2. 生成解读报告（单次 LLM 调用）
    if progress:
        progress("🧠 LLM 生成解读报告...")
    now = current_time_label()
    report = generate_text(
        READ_SYSTEM_PROMPT.format(now=now),
        f"当前系统时间：{now}（报告中的「解读时间」必须使用此时间，不要自行推断或编造日期）\n"
        f"请解读以下文档内容，生成结构化解读报告。\n"
        f"原文地址：{url}\n"
        f"文档标题：{fetched.get('title') or '未知'}\n\n"
        f"===== 文档内容开始 =====\n{fetched['markdown']}\n===== 文档内容结束 =====",
    )
    report = replace_time_line(report, "解读时间", now)

    # 3. 保存报告 + 写后单文件立即索引（read 缓存命中即时生效；失败不阻断，对账兜底）
    title = fetched.get("title") or "文档"
    filename = f"{sanitize_filename(title) or 'report'}-{datetime.now().strftime('%Y%m%d')}-解读.md"
    save_result = save_file_tool(f"reports/{filename}", report)
    index_status = index_file_lazy(save_result["path"])

    return {
        "report": report,
        "title": title,
        "report_path": save_result["path"],
        "notes": [{"url": url, "title": title, "report": report}],
        "index_ok": index_status.get("index_ok", True),
        "index_error": index_status.get("index_error"),
        "error": "",
    }
