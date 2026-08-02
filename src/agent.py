"""ReAct Agent 核心循环：推理 → 行动 → 观察 → 再推理"""

import re
import json
from typing import Optional

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .config import config
from .prompts import REACT_SYSTEM_PROMPT
from .tools import execute_tool, fetch_tool, save_file_tool, get_tool_descriptions

console = Console()

# 最大 ReAct 循环次数
MAX_LOOP_COUNT = 15


class Agent:
    """ReAct Agent —— 在思考与行动之间循环直到完成任务。"""

    def __init__(self, task_prompt: str):
        """
        Args:
            task_prompt: 任务专用的系统提示词（如 COLLECT_SYSTEM_PROMPT）
        """
        self.client = OpenAI(
    api_key=config.OPENAI_API_KEY,
    base_url=config.OPENAI_BASE_URL   # 把 config 里的地址传进来！
)
        self.conversation: list[dict] = []

        # 组合系统提示词
        tool_descriptions = get_tool_descriptions()
        system_prompt = REACT_SYSTEM_PROMPT.format(tool_descriptions=tool_descriptions)
        full_system = f"{task_prompt}\n\n{system_prompt}"

        self.conversation.append({
            "role": "user",
            "content": full_system,
        })

    def run(self, user_input: str) -> str:
        """执行 Agent 的主循环，返回最终结果。

        Args:
            user_input: 用户的任务描述

        Returns:
            Agent 的最终回答
        """
        self.conversation.append({
            "role": "user",
            "content": user_input,
        })

        loop_count = 0
        while loop_count < MAX_LOOP_COUNT:
            loop_count += 1

            # 调用 LLM
            response_text = self._call_llm()

            # 检查是否完成
            final_answer = self._extract_final_answer(response_text)
            if final_answer:
                return final_answer

            # 解析 Action
            action_name, action_params = self._parse_action(response_text)
            if action_name is None:
                # 无法解析，可能是格式错误，提示 LLM 重试
                console.print("[yellow]⚠ 无法解析动作，要求 LLM 重试...[/yellow]")
                self.conversation.append({
                    "role": "user",
                    "content": "请严格按格式输出。先输出 Thought，然后 Action 和 Action Input。"
                })
                continue

            # 执行工具
            console.print(f"🔧 [bold cyan]调用工具: {action_name}[/bold cyan]")
            result = execute_tool(action_name, action_params)

            # 将结果反馈给 LLM
            self.conversation.append({
                "role": "user",
                "content": f"Observation (工具执行结果):\n{result}",
            })

        # 超时，强制要求总结
        console.print("[yellow]⚠ 达到最大循环次数，强制总结...[/yellow]")
        self.conversation.append({
            "role": "user",
            "content": "已达到最大循环次数。请基于目前收集到的信息，给出 Final Answer。"
        })
        response_text = self._call_llm()
        return self._extract_final_answer(response_text) or response_text

    def _call_llm(self) -> str:
        """
        调用 LLM（OpenAI 兼容接口，如阿里云百炼），返回响应文本。
        """
        # 直接使用 self.conversation 作为 messages
        # self.conversation 已经是 [{"role": "system", ...}, {"role": "user", ...}, ...] 格式
        response = self.client.chat.completions.create(
            model=config.LLM_MODEL,  # 从 .env 读取，例如 "qwen-plus"
            max_tokens=config.LLM_MAX_TOKENS,  # 最大输出长度
            messages=self.conversation,  # 包含 system + 全部历史对话
            temperature=0.7,  # 可选项，控制随机性（也可以从 config 读）
        )

        # 提取回复内容（OpenAI 标准格式）
        text = response.choices[0].message.content

        # 将 assistant 的回复追加到对话历史，供下一轮使用
        self.conversation.append({
            "role": "assistant",
            "content": text,
        })

        return text

    def _parse_action(self, text: str) -> tuple[Optional[str], dict]:
        """从 LLM 响应中解析 Action 和 Action Input。

        Args:
            text: LLM 响应文本

        Returns:
            (tool_name, params_dict) 或 (None, {})
        """
        # 匹配 Action: xxx
        action_match = re.search(r"Action:\s*(\w+)", text)
        if not action_match:
            return None, {}

        action_name = action_match.group(1).strip()

        # 匹配 Action Input: {...}
        # 尝试提取 JSON 块
        input_match = re.search(r"Action Input:\s*(\{.*?\})", text, re.DOTALL)
        if input_match:
            try:
                params = json.loads(input_match.group(1))
                return action_name, params
            except json.JSONDecodeError:
                pass

        return action_name, {}

    def _extract_final_answer(self, text: str) -> Optional[str]:
        """从 LLM 响应中提取 Final Answer。

        Args:
            text: LLM 响应文本

        Returns:
            最终答案文本，或 None（表示未完成）
        """
        match = re.search(r"Final Answer:\s*(.*)", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return None


def run_collect(tech_name: str) -> None:
    """运行资料收集任务。

    Args:
        tech_name: 要学习的技术名称
    """
    from .prompts import COLLECT_SYSTEM_PROMPT

    console.print(Panel(f"📚 开始收集「{tech_name}」的学习资料...", style="bold blue"))
    agent = Agent(COLLECT_SYSTEM_PROMPT)
    result = agent.run(
        f"请帮我收集「{tech_name}」的学习资料。\n"
        f"搜索关键词建议：{tech_name} tutorial, {tech_name} official documentation, "
        f"{tech_name} getting started, {tech_name} best practices.\n"
        f"最终将结果保存到 materials/{tech_name.lower().replace(' ', '-')}-materials.md"
    )
    console.print(Panel(Markdown(result), title="✅ 资料收集完成", style="green"))


def _generate_text(system_prompt: str, user_content: str) -> str:
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


def run_read(url: str) -> None:
    """运行文档阅读辅助任务（确定性管道）。

    流程：Firecrawl 抓取 → LLM 解读 → 保存 reports/ 报告。

    Args:
        url: 文档 URL
    """
    from datetime import datetime
    from .prompts import READ_SYSTEM_PROMPT
    from .storage import sanitize_filename

    console.print(Panel(f"📖 开始解读文档...", style="bold blue"))
    console.print(f"[dim]{url}[/dim]")

    # 1. 抓取文档内容
    fetched = fetch_tool(url)
    if not fetched.get("markdown"):
        console.print("[red]❌ 抓取文档内容失败，请检查 URL 是否有效。[/red]")
        if fetched.get("error"):
            console.print(f"[dim]{fetched['error']}[/dim]")
        return
    console.print(f"✅ 抓取成功，内容 {len(fetched['markdown'])} 字符"
                  f"{'（已截断，仅截取片段）' if fetched.get('truncated') else ''}")

    # 2. 生成解读报告（单次 LLM 调用）
    console.print("🧠 [bold cyan]LLM 生成解读报告...[/bold cyan]")
    report = _generate_text(
        READ_SYSTEM_PROMPT,
        f"请解读以下文档内容，生成结构化解读报告。\n"
        f"原文地址：{url}\n"
        f"文档标题：{fetched.get('title') or '未知'}\n\n"
        f"===== 文档内容开始 =====\n{fetched['markdown']}\n===== 文档内容结束 =====",
    )

    # 3. 保存报告
    title = fetched.get("title") or "文档"
    filename = f"{sanitize_filename(title) or 'report'}-{datetime.now().strftime('%Y%m%d')}-解读.md"
    save_result = save_file_tool(f"reports/{filename}", report)
    console.print(f"├  保存报告: [bold]{save_result['path']}[/bold]")
    console.print(Panel(Markdown(report), title="✅ 文档解读完成", style="green"))


def run_note(tech: str, conversation_log: str) -> None:
    """运行知识沉淀任务。

    Args:
        tech: 技术名称
        conversation_log: 本轮学习的对话记录或文档内容
    """
    from .prompts import NOTE_SYSTEM_PROMPT

    console.print(Panel(f"📝 开始整理「{tech}」的学习笔记...", style="bold blue"))
    agent = Agent(NOTE_SYSTEM_PROMPT)
    result = agent.run(
        f"请从以下学习内容中提取关键知识点，整理到知识库中。\n"
        f"技术领域：{tech}\n\n"
        f"学习内容：\n{conversation_log[:6000]}"
    )
    console.print(Panel(Markdown(result), title="✅ 知识沉淀完成", style="green"))