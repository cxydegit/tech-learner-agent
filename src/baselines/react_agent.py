"""ReAct Agent 基线 —— benchmark 对比用，主流程绝不 import。

自 agent.py / tools.py / prompts.py 原样搬入（冻结的 benchmark 基线）：
Agent 类（含 ``_extract_json_object`` 逐字副本）、MAX_LOOP_COUNT、console、
REACT_SYSTEM_PROMPT、TOOL_REGISTRY 及工具注册相关函数。

冻结约定：Agent 类仅 import 语句可与原版不同，方法体零漂移。
"""

import json
import re
from typing import Optional

from openai import OpenAI
from rich.console import Console

from ..adapters.fetch import fetch_tool
from ..adapters.search import search_tool
from ..adapters.store import list_files_tool, read_file_tool, save_file_tool
from ..config import config

console = Console()

# 最大 ReAct 循环次数
MAX_LOOP_COUNT = 15

# ReAct Agent 通用提示词（原 prompts.py 的 REACT_SYSTEM_PROMPT，冻结）
REACT_SYSTEM_PROMPT = """你是一个智能学习陪练 Agent，帮助用户高效学习新技术。你可以在思考（Thought）和行动（Action）之间循环，直到完成任务。

## 可用工具
{tool_descriptions}

## 输出格式
严格按以下格式输出：

```
Thought: <你的分析推理，用中文>
Action: <工具名>
Action Input: <JSON 格式的参数>
```

收到工具执行结果后，继续下一轮思考。任务完成后，输出：

```
Thought: 任务已完成。
Final Answer: <最终总结，用中文>
```

## 规则
- 每次只调用一个工具
- 必须等工具返回结果后再继续
- 如果工具返回错误，分析原因并尝试其他方法
- 不要编造工具返回的结果
"""


# ============================================================
# 工具注册表（原 tools.py，冻结）
# ============================================================

TOOL_REGISTRY = {
    "search": {
        "function": search_tool,
        "description": "搜索互联网资料。参数: query (str) — 搜索关键词, max_results (int, 可选) — 最大结果数",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "description": "最大返回结果数（可选）"},
            },
            "required": ["query"],
        },
    },
    "fetch": {
        "function": fetch_tool,
        "description": "抓取网页内容为 Markdown。参数: url (str) — 目标网页 URL",
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标网页 URL"},
            },
            "required": ["url"],
        },
    },
    "save_file": {
        "function": save_file_tool,
        "description": "保存内容到本地文件。参数: path (str) — 相对路径, content (str) — 文件内容",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                "content": {"type": "string", "description": "文件内容（Markdown 文本）"},
            },
            "required": ["path", "content"],
        },
    },
    "read_file": {
        "function": read_file_tool,
        "description": "读取本地文件。参数: path (str) — 相对路径",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
            },
            "required": ["path"],
        },
    },
    "list_files": {
        "function": list_files_tool,
        "description": "列出目录结构。参数: directory (str, 可选) — 相对目录路径",
        "schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "相对于项目根目录的目录路径（可选）"},
            },
            "required": [],
        },
    },
}


def get_tool_descriptions() -> str:
    """生成工具描述文本，注入到系统提示词中。"""
    lines = []
    for name, info in TOOL_REGISTRY.items():
        lines.append(f"- **{name}**: {info['description']}")
    return "\n".join(lines)


def get_tool_schemas() -> list[dict]:
    """生成 OpenAI function calling 的 tools 参数列表。

    Returns:
        [{"type": "function", "function": {name, description, parameters}}, ...]
    """
    schemas = []
    for name, info in TOOL_REGISTRY.items():
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["schema"],
            },
        })
    return schemas


def execute_tool(name: str, params: dict) -> str:
    """执行指定工具并返回 JSON 字符串结果。

    Args:
        name: 工具名称
        params: 工具参数

    Returns:
        JSON 字符串格式的结果
    """
    if name not in TOOL_REGISTRY:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

    try:
        func = TOOL_REGISTRY[name]["function"]
        result = func(**params)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ============================================================
# ReAct Agent 类（原 agent.py，冻结；_extract_json_object 为逐字副本）
# ============================================================

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

        # 是否启用原生 function calling（默认开启，可用 AGENT_USE_FUNCTION_CALLING=false 关闭）
        self.use_function_calling = config.AGENT_USE_FUNCTION_CALLING
        self.tool_schemas = get_tool_schemas() if self.use_function_calling else None

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

            # 调用 LLM（带 tools 时会走原生 function calling）
            message = self._call_llm()

            # 情况一：原生 function calling —— 模型返回结构化 tool_calls
            if getattr(message, "tool_calls", None):
                for tc in message.tool_calls:
                    name = tc.function.name
                    try:
                        params = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        params = {}
                    console.print(f"🔧 [bold cyan]调用工具: {name}[/bold cyan]")
                    result = execute_tool(name, params)
                    # 以 role="tool" 回填结果，SDK 会按 tool_call_id 关联
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                continue

            # 情况二：文本解析 fallback（非 function calling 模型）
            response_text = message.content or ""

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
        message = self._call_llm()
        return self._extract_final_answer(message.content or "") or (message.content or "")

    def _call_llm(self) -> object:
        """
        调用 LLM（OpenAI 兼容接口，如阿里云百炼），返回响应 message 对象。

        Returns:
            response.choices[0].message：含 content 与 tool_calls 字段
        """
        kwargs = {
            "model": config.LLM_MODEL,  # 从 .env 读取，例如 "qwen-plus"
            "max_tokens": config.LLM_MAX_TOKENS,  # 最大输出长度
            "messages": self.conversation,  # 包含 system + 全部历史对话
            "temperature": 0.7,  # 可选项，控制随机性（也可以从 config 读）
        }
        if self.tool_schemas:
            kwargs["tools"] = self.tool_schemas
            kwargs["tool_choice"] = "auto"

        response = self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        # 将 assistant 回复追加到对话历史，供下一轮使用
        assistant_msg: dict = {"role": "assistant", "content": message.content or ""}
        if getattr(message, "tool_calls", None):
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ]
        self.conversation.append(assistant_msg)

        return message

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

        # 匹配 Action Input: 之后的 JSON 对象
        input_match = re.search(r"Action Input:\s*", text)
        if not input_match:
            return action_name, {}

        params = self._extract_json_object(text[input_match.end():])
        return action_name, params

    @staticmethod
    def _extract_json_object(s: str) -> dict:
        """从文本中提取第一个完整的 JSON 对象。

        用花括号配对 + 字符串状态机定位 JSON 边界，避免非贪婪正则
        在内容里的 `}`（如 markdown 代码块）处提前截断。

        Args:
            s: 从 "Action Input:" 之后开始的文本

        Returns:
            解析出的 dict；解析失败返回 {}
        """
        s = s.lstrip()
        if not s.startswith("{"):
            return {}

        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(s):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[: i + 1])
                        except json.JSONDecodeError:
                            return {}
        return {}

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
