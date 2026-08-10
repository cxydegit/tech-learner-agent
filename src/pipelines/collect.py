"""资料收集 / 深挖：确定性管道（搜索去重 → 抓取 → LLM 合成 → 保存）。

自 agent.py 迁出：collect_pipeline, dig_pipeline + 就近携带 COLLECT/DIG_COMPOSE_PROMPT。
纯数据进出（返回 dict），不打印、不写会话；进度交给 progress 回调。
"""

from typing import Callable

from ..adapters.fetch import fetch_tool
from ..adapters.llm import current_time_label, generate_text, replace_time_line
from ..adapters.search import search_tool
from ..adapters.store import save_file_tool
from ..config import config


# ============================================================
# 合成报告提示词
# ============================================================

COLLECT_COMPOSE_PROMPT = """你是一个专业的技术资料整理助手。我会给你一组搜索到的资源（标题+链接+摘要）和几篇抓取到的文档内容，请你基于这些信息，整理成一份结构化的学习资料清单。

**不要调用任何工具**，直接输出最终 Markdown 报告。

## 最终输出格式
```markdown
# <技术名> 学习资料清单（<级别>级）

> 生成时间：{now}
> 目标用户级别：<入门/进阶>

## 一、核心必读资源（按阅读顺序）
| 优先级 | 资源名称 | 链接 | 核心看点 |
|--------|---------|------|---------|
| 1 | ... | ... | ... |

## 二、扩展阅读（按需选读）
| 资源名称 | 链接 | 适用场景 |
|---------|------|---------|
| ... | ... | ... |

## 三、可运行的示例项目
| 项目名称 | 来源 | 运行方式 |
|---------|------|---------|
| ... | ... | ... |

## 四、学习路线建议（基于当前级别）
- 针对【<级别>】级别，建议按以下顺序推进：...
- 预估总耗时：XX 小时

```

## 原则
- 只基于我提供的资料，**不要编造链接**；没有的信息诚实标注"待补充"
- 每条资料标注来源，按优先级排序
- 优先官方文档、知名教程、GitHub 高星项目
- 报告中的「生成时间」必须使用我提供的当前系统时间（{now}），不要自行推断或编造日期
"""


DIG_COMPOSE_PROMPT = """你是一个专业的技术资料深挖助手。我会给你一组围绕某个具体方向搜索到的资源（标题+链接+摘要）和几篇抓取到的文档内容，请你基于这些信息，整理成一份深度资料报告。

**不要调用任何工具**，直接输出最终 Markdown 报告。

## 最终输出格式
```markdown
# <技术名> · <具体方向> 深度资料

> 生成时间：{now}

## 一、方向说明
用 2-3 句话说清楚这个方向是什么、通常涉及哪些内容，帮助用户理解搜索结果的背景。

## 二、核心资料（按优先级排序）
| 优先级 | 资料名称 | 来源 | 链接 | 为什么推荐 |
|--------|---------|------|------|-----------|
| ★★★ | ... | 官方文档 | ... | 最权威，讲透了核心概念 |
| ★★☆ | ... | GitHub Issue | ... | 高星项目，有深度细节 |
| ★☆☆ | ... | 技术博客 | ... | 补充视角，可参考 |

## 三、补充资料（按需选读）
| 资料名称 | 链接 | 适用场景 |
|---------|------|---------|
| ... | ... | 适合进一步扩展阅读 |

## 四、资料现状说明（可选）
- 如果该方向资料丰富：标注"该方向资料充足，建议从 ★★★ 开始阅读"
- 如果该方向资料较少：标注"该方向公开资料较少，以下内容已尽量覆盖"
- 如果技术不开源：标注"该项目未开源，以下为官方文档和社区讨论"
```

## 原则
- 只基于我提供的资料，**不要编造链接**；没有的信息诚实标注"待补充"
- 深度优先，优先能讲清"为什么"和"底层机制"的内容
- 报告中的「生成时间」必须使用我提供的当前系统时间（{now}），不要自行推断或编造日期
"""


# ============================================================
# collect_pipeline
# ============================================================

def collect_pipeline(tech_name: str, level: str = "入门",
                     progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：按级别生成搜索词 → 搜索去重 → 抓取 → LLM 合成 → 保存。

    与 run_collect 的区别：只返回数据（urls / report / materials_path），
    不打印、不写会话 —— 供 LangGraph 节点复用（Stage 3）。

    Args:
        tech_name: 技术名称
        level: 学习级别，入门 或 进阶
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"urls": list[str], "report": str, "materials_path": str}
    """
    # 1. 按级别生成搜索词（对应 COLLECT_PROMPT 的搜索策略）
    base = tech_name.strip()
    if level == "进阶":
        queries = [
            f"{base} advanced guide",
            f"{base} best practices",
            f"{base} performance tuning",
        ]
    else:  # 入门
        queries = [
            f"{base} official documentation",
            f"{base} getting started",
            f"{base} github examples",
        ]

    # 2. 逐条搜索并去重
    raw_results: list[dict] = []
    for q in queries:
        if progress:
            progress(f"🔍 搜索: {q}")
        r = search_tool(q)
        raw_results.extend(r.get("results", []))

    seen: set[str] = set()
    results: list[dict] = []
    for r in raw_results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            results.append(r)

    # 3. 抓取排名靠前的文档
    fetched_blocks: list[str] = []
    for r in results[: config.MAX_FETCH_PAGES]:
        url = r["url"]
        if progress:
            progress(f"📄 抓取: {url}")
        f = fetch_tool(url)
        if f.get("markdown"):
            fetched_blocks.append(
                f"### {f.get('title') or url}\n来源：{url}\n\n{f['markdown'][:4000]}"
            )

    # 4. 单次 LLM 生成报告（无工具，无循环）
    if progress:
        progress("🧠 LLM 生成学习资料清单...")
    now = current_time_label()
    resource_lines = [
        f"- {r.get('title', '')} | {r.get('url', '')} | {r.get('content', '')[:200]}"
        for r in results[:10]
    ]
    user_content = (
        f"当前系统时间：{now}（报告中的「生成时间」必须使用此时间，不要自行推断或编造日期）\n"
        f"技术名称：{tech_name}\n级别：{level}\n\n"
        f"===== 搜索结果（标题 | 链接 | 摘要）=====\n"
        + "\n".join(resource_lines)
        + f"\n\n===== 抓取的文档内容 =====\n"
        + "\n".join(fetched_blocks)
    )
    report = generate_text(COLLECT_COMPOSE_PROMPT.format(now=now), user_content)
    report = replace_time_line(report, "生成时间", now)

    # 5. 保存（代码直接写入，不经工具参数序列化）
    safe = tech_name.lower().replace(" ", "-")
    save_result = save_file_tool(f"materials/{safe}-materials.md", report)

    return {
        "urls": [r["url"] for r in results],
        "report": report,
        "materials_path": save_result["path"],
    }


# ============================================================
# dig_pipeline
# ============================================================

def dig_pipeline(tech_name: str, direction: str,
                 progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：按方向生成搜索词 → 搜索去重 → 抓取 → LLM 合成 → 保存。

    与 run_dig 的区别：只返回数据（urls / report / materials_path），
    不打印、不写会话 —— 供 LangGraph 节点复用（Stage 3）。

    Args:
        tech_name: 要学习的技术名称
        direction: 具体深挖方向
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"urls": list[str], "report": str, "materials_path": str}
    """
    # 1. 按方向生成搜索词（对应 DIG_PROMPT 的搜索策略）
    base = tech_name.strip()
    direction = direction.strip()
    queries = [
        f"{base} {direction}",
        f"{base} {direction} github",
        f"{base} {direction} internals",
    ]

    # 2. 逐条搜索并去重
    raw_results: list[dict] = []
    for q in queries:
        if progress:
            progress(f"🔍 搜索: {q}")
        r = search_tool(q)
        raw_results.extend(r.get("results", []))

    seen: set[str] = set()
    results: list[dict] = []
    for r in raw_results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            results.append(r)

    # 3. 抓取排名靠前的文档
    fetched_blocks: list[str] = []
    for r in results[: config.MAX_FETCH_PAGES]:
        url = r["url"]
        if progress:
            progress(f"📄 抓取: {url}")
        f = fetch_tool(url)
        if f.get("markdown"):
            fetched_blocks.append(
                f"### {f.get('title') or url}\n来源：{url}\n\n{f['markdown'][:4000]}"
            )

    # 4. 单次 LLM 生成报告（无工具，无循环）
    if progress:
        progress("🧠 LLM 生成深度资料...")
    now = current_time_label()
    resource_lines = [
        f"- {r.get('title', '')} | {r.get('url', '')} | {r.get('content', '')[:200]}"
        for r in results[:10]
    ]
    user_content = (
        f"当前系统时间：{now}（报告中的「生成时间」必须使用此时间，不要自行推断或编造日期）\n"
        f"技术名称：{tech_name}\n具体方向：{direction}\n\n"
        f"===== 搜索结果（标题 | 链接 | 摘要）=====\n"
        + "\n".join(resource_lines)
        + f"\n\n===== 抓取的文档内容 =====\n"
        + "\n".join(fetched_blocks)
    )
    report = generate_text(DIG_COMPOSE_PROMPT.format(now=now), user_content)
    report = replace_time_line(report, "生成时间", now)

    # 5. 保存（代码直接写入，不经工具参数序列化）
    safe_tech = tech_name.lower().replace(" ", "-")
    safe_dir = direction.lower().replace(" ", "-")
    save_result = save_file_tool(f"materials/{safe_tech}-{safe_dir}-dig.md", report)

    return {
        "urls": [r["url"] for r in results],
        "report": report,
        "materials_path": save_result["path"],
    }
