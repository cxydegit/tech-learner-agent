"""ReAct Agent 核心循环：推理 → 行动 → 观察 → 再推理"""

import re
import json
from datetime import datetime
from typing import Callable, Optional

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .config import config
from .prompts import REACT_SYSTEM_PROMPT
from .tools import execute_tool, search_tool, fetch_tool, save_file_tool, get_tool_descriptions, get_tool_schemas

console = Console()

# 最大 ReAct 循环次数
MAX_LOOP_COUNT = 15


def _current_time_label() -> str:
    """当前系统时间标签（YYYY-MM-DD HH:MM），注入 collect/dig/read 管道防止 LLM 编造历史日期。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _replace_time_line(report: str, label: str, now: str) -> str:
    """确定性兜底：把报告中的时间行（如 `> 生成时间：xxx`）替换为系统时间。

    即使 LLM 忽略"使用我提供的时间"指令，也能保证 报告内时间 === 当前系统日期。
    """
    return re.sub(rf"(?m)^>\s*{label}\s*[:：].*$", f"> {label}：{now}", report)


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
    from .prompts import COLLECT_COMPOSE_PROMPT

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
    now = _current_time_label()
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
    report = _generate_text(COLLECT_COMPOSE_PROMPT.format(now=now), user_content)
    report = _replace_time_line(report, "生成时间", now)

    # 5. 保存（代码直接写入，不经工具参数序列化）
    safe = tech_name.lower().replace(" ", "-")
    save_result = save_file_tool(f"materials/{safe}-materials.md", report)

    return {
        "urls": [r["url"] for r in results],
        "report": report,
        "materials_path": save_result["path"],
    }


def run_collect(tech_name: str, level: str = "入门", session=None) -> None:
    """运行资料收集任务（确定性管道，全面学习，按级别）。

    薄包装：调用 collect_pipeline，负责进度打印与会话状态写入。

    Args:
        tech_name: 要学习的技术名称
        level: 学习级别，入门 或 进阶
        session: 可选 LearnSession；传入时读写会话状态（跨命令共享）
    """
    console.print(Panel(f"📚 开始收集「{tech_name}」的学习资料（{level}级）...", style="bold blue"))

    result = collect_pipeline(tech_name, level, progress=lambda m: console.print(m))
    console.print(f"✅ 共收集到 [bold]{len(result['urls'])}[/bold] 条去重资源")
    console.print(f"├  保存报告: [bold]{result['materials_path']}[/bold]")
    console.print(Panel(Markdown(result["report"][:3000]), title="✅ 资料收集完成", style="green"))

    # 更新会话状态（若在 /learn 会话中）
    if session is not None:
        session.tech = tech_name
        session.level = level
        session.materials_path = result["materials_path"]
        session.add_history("collect", f"收集「{tech_name}」({level}) → {result['materials_path']}")
        session.save()


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
    from .prompts import DIG_COMPOSE_PROMPT

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
    now = _current_time_label()
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
    report = _generate_text(DIG_COMPOSE_PROMPT.format(now=now), user_content)
    report = _replace_time_line(report, "生成时间", now)

    # 5. 保存（代码直接写入，不经工具参数序列化）
    safe_tech = tech_name.lower().replace(" ", "-")
    safe_dir = direction.lower().replace(" ", "-")
    save_result = save_file_tool(f"materials/{safe_tech}-{safe_dir}-dig.md", report)

    return {
        "urls": [r["url"] for r in results],
        "report": report,
        "materials_path": save_result["path"],
    }


def run_dig(tech_name: str, direction: str, session=None) -> None:
    """运行资料深挖任务（确定性管道，定向深挖）。

    薄包装：调用 dig_pipeline，负责进度打印与会话状态写入。

    Args:
        tech_name: 要学习的技术名称
        direction: 具体深挖方向
        session: 可选 LearnSession；传入时读写会话状态（跨命令共享）
    """
    console.print(Panel(f"🔍 开始深挖「{tech_name}」的「{direction}」...", style="bold blue"))

    result = dig_pipeline(tech_name, direction, progress=lambda m: console.print(m))
    console.print(f"✅ 共收集到 [bold]{len(result['urls'])}[/bold] 条去重资源")
    console.print(f"├  保存报告: [bold]{result['materials_path']}[/bold]")
    console.print(Panel(Markdown(result["report"][:3000]), title="✅ 资料深挖完成", style="green"))

    # 更新会话状态（若在 /learn 会话中）
    if session is not None:
        session.tech = tech_name
        session.materials_path = result["materials_path"]
        session.add_history("dig", f"深挖「{tech_name}」的「{direction}」 → {result['materials_path']}")
        session.save()


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


def _parse_classify(raw: str) -> dict:
    """从 LLM 响应中稳健地解析文档分类结果。

    兼容：去掉 ```json 代码块包裹、抽取第一个 JSON 对象。

    Returns:
        {"is_technical": bool, "reason": str}，解析失败返回空 dict
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # 兜底：用花括号配对抽取第一个 JSON 对象
    obj = Agent._extract_json_object(text)
    return obj if isinstance(obj, dict) else {}


def _classify_technical(url: str, title: str, markdown: str) -> tuple[bool, str]:
    """识别文档是否为技术文档（LLM 分类门）。

    Args:
        url: 文档 URL
        title: 文档标题
        markdown: 抓取到的内容

    Returns:
        (is_technical, reason)；解析失败时默认视为技术文档（is_technical=True）避免误拦截
    """
    from .prompts import CLASSIFY_DOC_PROMPT

    raw = _generate_text(
        CLASSIFY_DOC_PROMPT,
        f"文档标题：{title or '未知'}\n链接：{url}\n\n"
        f"===== 内容片段 =====\n{markdown[:3000]}\n===== 内容结束 =====",
    )
    decision = _parse_classify(raw)
    is_tech = str(decision.get("is_technical", "true")).strip().lower() in ("true", "1", "yes")
    reason = str(decision.get("reason", "")).strip()
    return is_tech, reason


def _confirm_reuse() -> bool:
    """询问用户是否复用已有解读报告（默认不复用，避免误跳过新文档）。"""
    try:
        ans = input("是否复用已有解读，跳过抓取？[y/N] ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _reuse_cached_report(cached: dict, url: str, session=None) -> None:
    """把 RAG 缓存命中的已有解读展示出来，并同步到会话状态。"""
    # 优先读取完整报告文件；文件丢失时退回缓存的片段
    report_path = config.BASE_DIR / cached["path"]
    if report_path.exists():
        report = report_path.read_text(encoding="utf-8", errors="replace")
    else:
        report = cached.get("content", "")

    console.print(Panel(
        f"[dim]{cached['path']}[/dim]\n相似度 {cached['similarity']:.2f}",
        title="📄 复用已有解读",
        style="cyan",
    ))
    console.print(Markdown(report))

    if session is not None:
        session.urls.append(url)
        session.visited.add(url)
        session.notes.append({"url": url, "title": cached["path"], "report": report})
        session.add_history("read", f"复用缓存解读 {url} → {cached['path']}")
        session.save()


def read_pipeline(url: str, progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：抓取 → 技术文档分类 → LLM 解读 → 保存 reports/。

    与 run_read 的区别：只返回数据（report / title / notes / error），
    不打印、不写会话、不做"缓存复用"交互（复用决策属调用方职责）。

    Args:
        url: 文档 URL
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"report": str, "title": str, "report_path": str, "notes": list[dict], "error": str}
        - error 为空表示成功；error 非空表示失败 / 非技术文档（此时 report 为空）
        - notes 形如 [{"url", "title", "report"}]，供 LangGraph 状态累积
    """
    from .prompts import READ_SYSTEM_PROMPT
    from .storage import sanitize_filename

    # 1. 抓取文档内容
    fetched = fetch_tool(url)
    if not fetched.get("markdown"):
        err = fetched.get("error") or "抓取文档内容失败，请检查 URL 是否有效。"
        return {"report": "", "title": "", "report_path": "", "notes": [], "error": f"抓取失败：{err}"}
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
        }

    # 2. 生成解读报告（单次 LLM 调用）
    if progress:
        progress("🧠 LLM 生成解读报告...")
    now = _current_time_label()
    report = _generate_text(
        READ_SYSTEM_PROMPT.format(now=now),
        f"当前系统时间：{now}（报告中的「解读时间」必须使用此时间，不要自行推断或编造日期）\n"
        f"请解读以下文档内容，生成结构化解读报告。\n"
        f"原文地址：{url}\n"
        f"文档标题：{fetched.get('title') or '未知'}\n\n"
        f"===== 文档内容开始 =====\n{fetched['markdown']}\n===== 文档内容结束 =====",
    )
    report = _replace_time_line(report, "解读时间", now)

    # 3. 保存报告
    title = fetched.get("title") or "文档"
    filename = f"{sanitize_filename(title) or 'report'}-{datetime.now().strftime('%Y%m%d')}-解读.md"
    save_result = save_file_tool(f"reports/{filename}", report)

    return {
        "report": report,
        "title": title,
        "report_path": save_result["path"],
        "notes": [{"url": url, "title": title, "report": report}],
        "error": "",
    }


def run_read(url: str, session=None) -> None:
    """运行文档阅读辅助任务（确定性管道）。

    薄包装：缓存复用决策在 CLI 层（_confirm_reuse），其余交给 read_pipeline。

    Args:
        url: 文档 URL
        session: 可选 LearnSession；传入时读写会话状态（跨命令共享）
    """
    console.print(Panel(f"📖 开始解读文档...", style="bold blue"))
    console.print(f"[dim]{url}[/dim]")

    # 0. RAG 历史召回：该 URL 已有解读则提示复用（失败静默，不影响抓取）
    try:
        from .rag import check_read_cache
        cached = check_read_cache(url)
    except Exception:  # noqa: BLE001
        cached = None
    if cached:
        console.print(Panel(
            f"📌 检测到该 URL 已有解读报告：\n[bold]{cached['path']}[/bold]"
            f"（相似度 {cached['similarity']:.2f}）",
            title="⏭ RAG 缓存命中",
            style="cyan",
        ))
        if _confirm_reuse():
            _reuse_cached_report(cached, url, session=session)
            return

    # 1-3. 抓取 → 分类 → 解读 → 保存
    result = read_pipeline(url, progress=lambda m: console.print(m))
    if result.get("error"):
        console.print(f"[red]❌ {result['error']}[/red]")
        return
    console.print(f"├  保存报告: [bold]{result['report_path']}[/bold]")
    console.print(Panel(Markdown(result["report"]), title="✅ 文档解读完成", style="green"))

    # 4. 更新会话状态（若在 /learn 会话中）
    if session is not None:
        session.urls.append(url)
        session.visited.add(url)
        session.notes.extend(result["notes"])
        session.add_history("read", f"解读 {url} → {result['report_path']}")
        session.save()


def _parse_entries(raw: str) -> list[dict]:
    """从 LLM 响应中稳健地解析知识点 JSON 数组。

    兼容：去掉 ```json 代码块包裹、从文本中抽取第一个 JSON 数组。

    Returns:
        [{topic, tags, content}, ...]，解析失败返回 []
    """
    text = raw.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    def _as_list(data):
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    # 直接解析整个响应
    try:
        return _as_list(json.loads(text))
    except Exception:
        pass

    # 抽取第一个 JSON 数组块
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return _as_list(json.loads(m.group(0)))
        except Exception:
            pass
    return []


def note_pipeline(tech: str, conversation_log: str,
                  progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：LLM 提取知识点(JSON) → 逐条去重/合并入库 → 更新索引。

    与 run_note 的区别：只返回数据（results / summary / 计数），
    不打印、不写会话 —— 供 LangGraph 节点复用（Stage 3）。

    Args:
        tech: 技术名称
        conversation_log: 本轮学习的对话记录或文档内容
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"results": [persist 结果], "summary": str,
         "new_count": int, "merged_count": int, "raw": str}
        - results 为空表示未提取到可入库知识点
    """
    from .prompts import EXTRACT_SYSTEM_PROMPT
    from .storage import persist_note

    # 1. LLM 提取关键知识点（单次调用，输出 JSON）
    content = conversation_log[:12000]
    raw = _generate_text(
        EXTRACT_SYSTEM_PROMPT,
        f"技术领域：{tech}\n\n===== 学习内容开始 =====\n{content}\n===== 学习内容结束 =====",
    )
    entries = _parse_entries(raw)

    # 2. 逐条去重/合并并入库
    new_count = merged_count = 0
    results: list[dict] = []
    for e in entries:
        topic = (e.get("topic") or "").strip()
        body = (e.get("content") or "").strip()
        if not topic or not body:
            continue
        tags = e.get("tags") or [tech]
        result = persist_note(tech, topic, body, tags)
        results.append(result)
        if result["action"] == "new":
            new_count += 1
        else:
            merged_count += 1

    summary = (
        f"新增 {new_count} 篇，合并更新 {merged_count} 篇"
        if results else "未提取到可入库的知识点"
    )
    return {
        "results": results,
        "summary": summary,
        "new_count": new_count,
        "merged_count": merged_count,
        "raw": raw,
    }


def run_note(tech: str, conversation_log: str, session=None) -> None:
    """运行知识沉淀任务（确定性管道）。

    薄包装：调用 note_pipeline，负责打印与会话状态写入。

    Args:
        tech: 技术名称
        conversation_log: 本轮学习的对话记录或文档内容
        session: 可选 LearnSession；传入时读写会话状态（跨命令共享）
    """
    console.print(Panel(f"📝 开始整理「{tech}」的学习笔记...", style="bold blue"))
    console.print("🧠 [bold cyan]LLM 提取知识点...[/bold cyan]")

    result = note_pipeline(tech, conversation_log, progress=lambda m: console.print(m))

    if not result["results"]:
        console.print("[yellow]⚠ 未提取到可入库的知识点。[/yellow]")
        console.print(Panel(Markdown(result["raw"]), title="LLM 原始返回", style="dim"))
        return

    console.print(f"✅ 本次沉淀：新增 [bold]{result['new_count']}[/bold] 篇，"
                  f"合并更新 [bold]{result['merged_count']}[/bold] 篇")
    for r in result["results"]:
        label = "🆕 新增" if r["action"] == "new" else "🔗 合并"
        console.print(f"  {label} [bold]{r['topic']}[/bold] → knowledge/{r['path']}")

    console.print(Panel(Markdown(f"知识沉淀完成，共 {len(result['results'])} 个知识点已写入 `knowledge/`。"
                                  f"详见 knowledge/INDEX.md"), title="✅ 学习成果沉淀完成", style="green"))

    # 3. 更新会话状态（若在 /learn 会话中）
    if session is not None:
        session.notes.extend(result["results"])
        session.add_history("note", f"沉淀 {len(result['results'])} 个知识点")
        session.save()