"""LLM 基础设施：一次性非循环生成 + 系统时间标签注入（确定性兜底）。

自 agent.py 迁出：generate_text（原 _generate_text）。另含 collect/dig/read
管道共用的 current_time_label / replace_time_line（Step 1 修复时间编造的产物）。
"""

import re
from datetime import datetime

from openai import OpenAI

from ..config import config


def current_time_label() -> str:
    """当前系统时间标签（YYYY-MM-DD HH:MM），注入 collect/dig/read 管道防止 LLM 编造历史日期。"""
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
