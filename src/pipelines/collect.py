"""资料收集：确定性管道（搜索去重 → 抓取 → LLM 合成 → 保存）。

自 agent.py 迁出：collect_pipeline + 就近携带 COLLECT_PROMPT_DEFAULT / COLLECT_PROMPT_FOCUS。
纯数据进出（返回 dict），不打印、不写会话；进度交给 progress 回调。
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime

from ..adapters.fetch import fetch_many
from ..adapters.github import fetch_star_count
from ..adapters.llm import current_time_label, generate_text, replace_time_line
from ..adapters.search import search_tool
from ..adapters.store import save_file_tool
from ..config import config
from ..domain.quality import screen_results

# ============================================================
# 合成报告提示词
#
# 两套提示词按 focus 切换：
# - 无 focus：固定模板（结构化学习资料清单，用户没提侧重点，给默认铺路）
# - 有 focus：非固定模板（只保「核心资料」表硬骨架，其余围绕用户关注点自由输出）
# focus 本身作为「用户提示词」放进 user_content，这里只做结构性切换。
# ============================================================

COLLECT_PROMPT_DEFAULT = """你是一个专业的技术资料整理助手。用户想系统性地学习一个新技术，我会给你一组搜索到的资源（标题+链接+摘要）和几篇抓取到的文档内容。请你基于这些信息，整理成一份结构化、能直接照着学的学习资料清单。

**不要调用任何工具**，直接输出最终 Markdown 报告。

## 最终输出格式（严格按此结构）
```markdown
# <技术名> 学习资料清单

> 生成时间：{now}

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

## 四、学习路线建议
- 按建议的推进顺序分阶段说明：先学什么、再学什么、每阶段看哪些资源
- 预估总耗时：XX 小时
```

## 原则
- 只基于我提供的资料，**不要编造链接**；没有的信息诚实标注"待补充"
- 每条资料标注来源，按优先级排序
- 核心必读只保留真正值得优先看的：官方文档、知名教程、GitHub 高星项目，宁可少而精
- 示例项目没有合适的就省略该小节，不要用"无。"占位
- 若用户输入中提供「已排除的低质量链接」统计，在报告末尾用一行注明"已排除 N 条低质量链接（原因）"；没有则不写
- 报告中的「生成时间」必须使用我提供的当前系统时间（{now}），不要自行推断或编造日期
"""


COLLECT_PROMPT_FOCUS = """你是一个专业的技术资料整理助手。用户想学习某个技术，并提出了明确的关注点（用户输入中标注「用户关注点」）。我会给你一组围绕该关注点搜索到的资源（标题+链接+摘要）和几篇抓取到的文档内容。请你基于这些信息，整理成一份能真正解决用户关注点的深度资料。

**不要调用任何工具**，直接输出最终 Markdown 报告。

## 输出要求
- 围绕「用户关注点」组织内容：它是什么、怎么学、关键点与常见坑、资料怎么挑。
- **不要套用「一、核心必读」「二、扩展阅读」「三、可运行示例」「四、学习路线」这类固定小节模板**，按内容需要自由安排章节与标题。
- **唯一必须保留的硬骨架是「核心资料」表**（其余结构完全自由）：
  | 优先级 | 资料名称 | 来源 | 链接 | 为什么推荐 |
  按优先级降序排列；优先能讲清"为什么"和"底层机制"的内容，官方文档/高星项目排前；用 ★ 标优先级（★★★ 最核心）。
- 「为什么推荐」要写具体（如"官方文档，讲透了 XX 的底层机制"），不要写"推荐阅读"这类空话。
- 其余内容只保留对用户关注点真正有用的部分；以内容充实为准，不要为凑模板硬塞无关章节。

## 原则
- 只基于我提供的资料，**不要编造链接**；没有的信息诚实标注"待补充"
- 报告以一个 Markdown 标题开头（如 `# <技术名> · <关注点> 深度资料`），并包含一行 `> 生成时间：` 用我提供的系统时间（{now}）
- 若用户输入中提供「已排除的低质量链接」统计，在报告末尾用一行注明"已排除 N 条低质量链接（原因）"；没有则不写
- 报告中的「生成时间」必须使用我提供的当前系统时间（{now}），不要自行推断或编造日期
"""


# ============================================================
# collect_pipeline
# ============================================================

def collect_pipeline(tech_name: str, focus: str | None = None,
                     progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：搜索去重 → 抓取 → LLM 合成 → 保存。

    与 run_collect 的区别：只返回数据（urls / report / materials_path），
    不打印、不写会话 —— 供 LangGraph 节点复用。

    Args:
        tech_name: 技术名称
        focus: 可选，用户的关注点（自由文本）。有则追加 focus 搜索词 + 走非固定模板；
            无则用默认搜索词 + 固定模板。
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"urls": list[str], "report": str, "materials_path": str}
    """
    # 1. 生成搜索词：默认三组 + focus 时追加一条（纯增量，无 focus 零变化）
    base = tech_name.strip()
    queries = [
        f"{base} official documentation",
        f"{base} getting started",
        f"{base} github examples",
    ]
    if focus:
        queries.append(f"{base} {focus.strip()}")

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

    # 2.5 质量预筛：丢明显垃圾（内容农场/低分），只抓通过的高质量结果
    if progress:
        progress("🛡️ 预筛低质量链接...")
    kept, excluded = screen_results(
        results,
        fetch_stars=(lambda u: fetch_star_count(u, config.GITHUB_TOKEN) if config.GITHUB_TOKEN else None),
        official_domains=set(config.QUALITY_OFFICIAL_DOMAINS),
        platform_domains=set(config.QUALITY_PLATFORM_DOMAINS),
        content_farms=set(config.QUALITY_CONTENT_FARMS),
        min_score=config.QUALITY_MIN_SCORE,
        star_tiers=config.QUALITY_STAR_TIERS,
        domain_bonus_official=config.QUALITY_DOMAIN_BONUS_OFFICIAL,
        domain_bonus_platform=config.QUALITY_DOMAIN_BONUS_PLATFORM,
        url_bonus_official_docs=config.QUALITY_URL_BONUS_OFFICIAL_DOCS,
        url_penalty_blog=config.QUALITY_URL_PENALTY_BLOG,
        url_penalty_source=config.QUALITY_URL_PENALTY_SOURCE,
    )

    # 3. 并发抓取通过预筛、排名靠前的文档（顺序归并保持确定性；单个超时/失败记空，不拖垮整批）
    fetched_blocks: list[str] = []
    targets = kept[: config.MAX_FETCH_PAGES]
    if targets:
        if progress:
            progress(f"🛰️ 并发抓取 {len(targets)} 个页面（超时 {config.FETCH_TIMEOUT_SECONDS:.0f}s）...")
        for r, f in zip(targets, fetch_many([r["url"] for r in targets])):
            if f.get("markdown"):
                fetched_blocks.append(
                    f"### {f.get('title') or r['url']}\n来源：{r['url']}\n\n{f['markdown'][:4000]}"
                )

    # 4. 单次 LLM 生成报告（无工具，无循环）；focus 作为用户提示词进 user 消息
    if progress:
        progress("🧠 LLM 生成学习资料...")
    now = current_time_label()
    prompt = (COLLECT_PROMPT_FOCUS if focus else COLLECT_PROMPT_DEFAULT).format(now=now)
    resource_lines = [
        f"- {r.get('title', '')} | {r.get('url', '')} | {r.get('content', '')[:200]}"
        for r in kept[:10]
    ]
    focus_line = f"用户关注点：{focus}\n" if focus else ""
    excluded_line = _excluded_summary(excluded)
    user_content = (
        f"当前系统时间：{now}（报告中的「生成时间」必须使用此时间，不要自行推断或编造日期）\n"
        f"技术名称：{tech_name}\n"
        f"{focus_line}"
        f"\n===== 搜索结果（标题 | 链接 | 摘要）=====\n"
        f"{''.join(resource_lines)}"
        f"{excluded_line}"
        f"\n\n===== 抓取的文档内容 =====\n"
        f"{''.join(fetched_blocks)}"
    )
    report = generate_text(prompt, user_content)
    report = replace_time_line(report, "生成时间", now)

    # 5. 保存（代码直接写入，不经工具参数序列化）；文件名带时间版本号区分多次询问
    save_result = save_file_tool(materials_filename(tech_name), report)

    return {
        "urls": [r["url"] for r in kept],
        "report": report,
        "materials_path": save_result["path"],
    }


def materials_filename(tech_name: str) -> str:
    """collect 报告文件名：``materials/{tech}-materials-{MMDD-HHMM}.md``。

    时间版本号让同一技术的多次询问各留一份，避免互相覆盖（无 focus 与有 focus
    的两次 collect 若都写 `{tech}-materials.md`，后者会覆盖前者）。
    """
    safe = tech_name.lower().replace(" ", "-")
    stamp = datetime.now().astimezone().strftime("%m%d-%H%M")
    return f"materials/{safe}-materials-{stamp}.md"


def _excluded_summary(excluded: list[dict]) -> str:
    """把剔除项压缩成提示词里的一行统计（供报告"已排除 N 条低质量链接"透明汇报）。"""
    if not excluded:
        return ""
    counts = Counter(e.get("reason", "其他") for e in excluded)
    detail = "、".join(f"{reason} {n} 条" for reason, n in counts.items())
    return (
        "\n\n===== 已排除的低质量链接（不需要在报告主体引用，只用于末尾一行说明）=====\n"
        f"共排除 {len(excluded)} 条：{detail}"
    )
