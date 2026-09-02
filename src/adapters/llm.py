"""LLM 基础设施：一次性非循环生成 + 系统时间标签注入（确定性兜底）。

自 agent.py 迁出：generate_text（原 _generate_text）。另含 collect/read
管道共用的 current_time_label / replace_time_line（修复时间编造的产物）。
"""

import json
import re
from datetime import datetime

from openai import OpenAI

from ..config import config
from ..domain.extraction import parse_json_object


def current_time_label() -> str:
    """当前系统时间标签（YYYY-MM-DD HH:MM），注入 collect/read 管道防止 LLM 编造历史日期。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


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
# 工具调用通道（coach agent 循环用）
# 与 generate_text 的区别：向模型暴露 tools 定义，模型可返回 tool_calls。
# RISKS 教训的回应：大内容走文件、工具出入参短、失败回退——由 graph 层护栏兜底。
# ============================================================


class ToolCallError(Exception):
    """工具调用通道持久失败（重试 + 回退后仍失败），由 graph 层降级处理。"""


def _parse_chat_response(msg) -> dict:
    """把 openai 响应消息转成统一 dict：{content, tool_calls:[{id,name,arguments}]}。

    arguments 是 JSON 字符串，解析失败兜底为 {}（graph 层护栏会拦截异常参数）。
    """
    tool_calls = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        args = tc.function.arguments or ""
        try:
            args_obj = json.loads(args) if args.strip() else {}
        except Exception:  # noqa: BLE001 —— 模型给的 arguments 不合法 JSON，兜底空 dict
            args_obj = {}
        tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args_obj})
    return {"content": msg.content, "tool_calls": tool_calls}


def chat_with_tools(system_prompt: str, messages: list[dict], tools: list[dict],
                    *, max_tokens: int | None = None) -> dict:
    """执行一次带原生工具定义的对话补全。

    与 generate_text 的定位不同：generate_text 是"单次生成"，适用于确定性管道；
    chat_with_tools 暴露工具，模型可返回 tool_calls，供 coach 循环反复调用。

    Args:
        system_prompt: 系统提示词（mode 相关，由调用方按模式挑选）
        messages: 历史消息（dict 列表，含 role/content/tool_calls/tool_call_id），
            格式对齐 openai 兼容接口（DashScope 支持原生 tool_calls）
        tools: OpenAI function 定义列表
        max_tokens: 可选，覆盖 config.LLM_MAX_TOKENS

    Returns:
        {"content": str | None, "tool_calls": [{id, name, arguments(dict)}], "fallback": bool}
        - 模型决定调用工具：content 可为 None、tool_calls 非空
        - 模型直接回复文本：tool_calls 为空
        - 持久失败且 config.ROUTE_FALLBACK_TO_TEXT：去掉 tools 再问一次，返回纯文本
          （fallback=True，调用方可感知降级）
        - 全部失败：抛 ToolCallError
    """
    messages_payload = [{"role": "system", "content": system_prompt}, *messages]
    kwargs: dict = {
        "model": config.LLM_MODEL,
        "temperature": 0.5,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "messages": messages_payload,
    }
    last_err: Exception | None = None
    for attempt in range(3):  # 瞬时故障（网络/400）重试
        try:
            kwargs["tools"] = tools
            client = OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)
            response = client.chat.completions.create(**kwargs)
            return _parse_chat_response(response.choices[0].message)
        except Exception as e:  # noqa: BLE001 —— 网络 / 400 / 解析，尝试重试或降级
            last_err = e
    if config.ROUTE_FALLBACK_TO_TEXT:
        try:
            kwargs.pop("tools", None)
            client = OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)
            response = client.chat.completions.create(**kwargs)
            parsed = _parse_chat_response(response.choices[0].message)
            parsed["fallback"] = True
            return parsed
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ToolCallError(str(last_err)) from last_err


# ============================================================
# 去重 LLM 判定
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
