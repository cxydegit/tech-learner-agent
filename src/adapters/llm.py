"""LLM 基础设施：一次性非循环生成 + 系统时间标签注入（确定性兜底）。

自 agent.py 迁出：generate_text（原 _generate_text）。另含 collect/read
管道共用的 current_time_label / replace_time_line（Step 1 修复时间编造的产物）。
"""

import re
from datetime import datetime

from openai import OpenAI

from ..config import config
from ..domain.extraction import parse_json_object


def current_time_label() -> str:
    """当前系统时间标签（YYYY-MM-DD HH:MM），注入 collect/read 管道防止 LLM 编造历史日期。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def replace_time_line(report: str, label: str, now: str) -> str:
    """确定性兜底：把报告中的时间行（如 `> 生成时间：xxx`）替换为系统时间。

    即使 LLM 忽略"使用我提供的时间"指令，也能保证 报告内时间 === 当前系统日期。
    """
    return re.sub(rf"(?m)^>\s*{label}\s*[:：].*$", f"> {label}：{now}", report)


def generate_text(system_prompt: str, user_content: str) -> str:
    """执行一次（非循环的）LLM 生成，返回响应文本。

    适用于"URL → 抓取 → 生成 → 保存"这类确定性管道任务，
    不需要 Agent 自主选择工具，因而跳过 ReAct 循环以降低开销和失败率。

    Args:
        system_prompt: 系统提示词
        user_content: 用户内容（已抓取的文档等）

    Returns:
        LLM 生成的文本
    """
    client = OpenAI(
        api_key=config.OPENAI_API_KEY,
        base_url=config.OPENAI_BASE_URL,
    )
    response = client.chat.completions.create(
        model=config.LLM_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        temperature=0.5,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content


# ============================================================
# 去重 LLM 判定（RAG_OPTIMIZATION P0 压力测试后重构）
# 旧确定性确认层（标题/标签/内容 overlap）对真正措辞不同的同义改写确认率仅 9%，
# 新方案把「是否同一知识点」交给 LLM 判定；标题 fast-path（domain/dedup）先挡掉
# 「标题基本同一句」的平凡情况省一次调用。判定输出 same/diff + 理由，理由供
# merge_candidates 展示（用户确认时看到为什么建议合并）。
# ============================================================

DEDUP_JUDGE_SYSTEM_PROMPT = """你是一个知识库去重助手。给你一篇**新提取的知识点**和一篇**已有笔记**，判断它们是否应该**归入同一篇笔记**（新知识点是否可以合并进已有笔记，而不是独立成篇）。

## 判定标准
- "same"：新知识点与已有笔记是**同一主题**——属于同一条知识线（如都是缓存问题、都是持久化问题）。同一主题下的不同子问题（如缓存穿透、缓存雪崩）也判 same：合并进同一篇笔记补充细节即可，不追求拆成更细的独立笔记。
- "diff"：新知识点与已有笔记**讲的完全不是同一件事**——只是看起来相似（同领域、用词相近），但主题根本不同，合并会污染已有笔记。

## 反面示例（不要犯）
- 「Redis 数据结构选型」与「Redis 五大核心角色」：一个讲选型、一个讲角色分工，主题不同 → diff
- 「Redis 缓存问题」与「Redis 持久化」：同属 Redis 但主题不同 → diff
- 同属 Redis 领域、都提到"高性能"这类泛泛内容，不算同一主题

## 正面示例
- 「Redis 持久化原理」与「Redis 持久化机制（RDB 与 AOF）」→ same（新知识点是对已有笔记的展开）
- 「缓存穿透」与「缓存雪崩」→ same（同一主题：缓存问题，合并进同一篇补充）
- 「用少量内存统计海量数据」与「概率数据类型（布隆过滤器 / HyperLogLog）」→ same

## 输出
只输出一个 JSON 对象，不要任何解释或 ```json 代码块标记：
{"verdict": "same" 或 "diff", "reason": "一句话中文理由（20 字内）"}
"""


def judge_same_knowledge_point(topic: str, tags: list[str] | None, content: str | None,
                               existing: dict) -> tuple[str, str]:
    """LLM 判定新知识点与已有笔记是否同一主题（可归入同一篇笔记）。

    粒度对齐知识库习惯：**按主题聚合**——同一主题下的不同子问题（缓存穿透 vs
    缓存雪崩）判 same 合并；只有「看起来相似但讲的完全不是同一件事」才判 diff。
    判定是**建议**不是决定：判定 same 后仍送 merge_candidates 由用户确认；
    判定失败（网络/解析异常）由调用方降级为不合并（安全侧）。

    Args:
        topic: 新知识点标题
        tags: 新知识点标签（判定上下文）
        content: 新知识点正文（判定上下文）
        existing: 已有笔记 dict（需含 topic / tags / content 字段）

    Returns:
        (verdict, reason)：verdict ∈ {"same", "diff"}，reason 为 LLM 给出的一句话理由
        （可能为空字符串）。
    """
    tag_str = " ".join(f"#{t}" for t in (tags or []))
    old_tag_str = " ".join(f"#{t}" for t in (existing.get("tags") or []))
    user_content = (
        f"===== 新提取的知识点 =====\n"
        f"标题：{topic}\n标签：{tag_str}\n"
        f"正文：{(content or '').strip()[:2000]}\n\n"
        f"===== 已有笔记 =====\n"
        f"标题：{existing.get('topic') or ''}\n标签：{old_tag_str}\n"
        f"正文：{(existing.get('content') or '').strip()[:2000]}"
    )
    raw = generate_text(DEDUP_JUDGE_SYSTEM_PROMPT, user_content)
    obj = parse_json_object(raw)
    verdict = obj.get("verdict")
    if verdict not in ("same", "diff"):
        verdict = "diff"  # 解析失败 / 模型输出异常 → 安全侧：不合并
    return verdict, str(obj.get("reason") or "").strip()
