"""LangGraph 有状态编排（Stage 3）：/learn 会话的状态图 + 人机交互点。

架构定位：确定性管道（collect/dig/read/note）是图的"叶子节点"，图负责编排——
有状态、可中断、跨会话：
- ``StateGraph(LearnState)``：按 ``command`` 条件路由到对应管道节点
- ``ask_level`` 节点用 ``interrupt()`` 实现模块 2 水平探测的交互点
  （图暂停 → CLI 询问 → ``Command(resume=...)`` 恢复）
- ``SqliteSaver`` checkpointer 跨会话/跨进程持久化（替换 session.py 的 JSON 落盘）

用法：
    with open_graph() as graph:
        config = {"configurable": {"thread_id": "..."}}
        graph.stream_events({...}, config, version="v3")
"""

import operator
import warnings
from contextlib import contextmanager
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from rich.console import Console

from .agent import collect_pipeline, dig_pipeline, note_pipeline, read_pipeline
from .config import config

# langgraph 1.0 的 v3 流式协议是实验性的，会打 LangChainBetaWarning；CLI 里主动过滤
warnings.filterwarnings("ignore", message="The v3 streaming protocol on Pregel is experimental.")

console = Console()

VALID_LEVELS = ("入门", "进阶")


class LearnState(TypedDict):
    """跨命令会话状态（字段定义对应 session.LearnSession，持久化交给 checkpointer）。

    用 reducer 的字段（urls / visited / notes）跨命令累加；普通字段直接覆盖。
    """

    tech: str
    level: str
    urls: Annotated[list[str], operator.add]
    visited: Annotated[list[str], operator.add]
    notes: Annotated[list[dict], operator.add]
    rag_hits: list[dict]
    # CLI 命令路由输入
    command: str  # collect / dig / read / note / ask_level
    args: list[str]
    last_output: str  # 节点输出，CLI 展示用


# ============================================================
# 节点：薄包装确定性管道（复用 src/agent.py 的纯函数）
# ============================================================


def _progress(msg: str) -> None:
    """节点内进度回调：缩进打印，区别于 REPL 提示符。"""
    console.print(f"  [dim]{msg}[/dim]")


def collect_node(state: LearnState) -> dict:
    """按 tech + level 运行资料收集管道。"""
    tech = state["tech"]
    level = state.get("level") or "入门"
    console.print(f"🔧 [bold cyan]graph.collect[/bold cyan] {tech}（{level}级）")
    result = collect_pipeline(tech, level, progress=_progress)
    return {
        "urls": result["urls"],
        "tech": tech,
        "level": level,
        "last_output": result["report"],
    }


def dig_node(state: LearnState) -> dict:
    """按 tech + direction 运行资料深挖管道。"""
    tech = state["tech"]
    direction = " ".join(state.get("args") or [])
    console.print(f"🔧 [bold cyan]graph.dig[/bold cyan] {tech} · {direction}")
    result = dig_pipeline(tech, direction, progress=_progress)
    return {"urls": result["urls"], "tech": tech, "last_output": result["report"]}


def read_node(state: LearnState) -> dict:
    """运行文档解读管道；结果写入 visited / notes。"""
    url = state["args"][0]
    console.print(f"🔧 [bold cyan]graph.read[/bold cyan] {url}")
    result = read_pipeline(url, progress=_progress)
    if result.get("error"):
        return {"last_output": f"❌ {result['error']}"}
    return {"visited": [url], "notes": result["notes"], "last_output": result["report"]}


def note_node(state: LearnState) -> dict:
    """把已解读的 report 汇总后沉淀为知识笔记。"""
    tech = state.get("tech") or ""
    content = _notes_to_content(state.get("notes") or [])
    if not tech:
        return {"last_output": "⚠ 会话还没有技术主题，先 collect <技术名>"}
    if not content.strip():
        return {"last_output": "⚠ 没有可沉淀的内容，先 read 一些文档"}
    console.print(f"🔧 [bold cyan]graph.note[/bold cyan] {tech}")
    result = note_pipeline(tech, content, progress=_progress)
    return {"notes": result["results"], "last_output": result["summary"]}


def ask_level_node(state: LearnState) -> dict:
    """水平探测交互点：图暂停，等 CLI 用 Command(resume=...) 恢复。

    ``interrupt()`` 的返回值即用户回答，写入 ``level`` 供后续 collect 使用。
    这是模块 2 完整自适应问卷（连续多次 interrupt）的最小种子。
    """
    answer = interrupt("请选择学习级别：入门 / 进阶")
    return {"level": str(answer).strip()}


def _notes_to_content(notes: list[dict]) -> str:
    """把已解读的 report 汇总成 note 管道的输入文本。"""
    parts = []
    for n in notes:
        if n.get("report"):
            parts.append(f"来源：{n.get('url')}\n{n['report']}")
    return "\n\n".join(parts)


# ============================================================
# 路由
# ============================================================


def _route_command(state: LearnState) -> str:
    """START 条件边：按 command 路由到对应节点。"""
    return state.get("command", "note")


def _route_by_level(state: LearnState) -> str:
    """ask_level 之后按 level 分支：合法级别进 collect，否则回问。"""
    return "collect" if state.get("level") in VALID_LEVELS else "ask_level"


# ============================================================
# 构建
# ============================================================


def build_graph(checkpointer):
    """用给定 checkpointer 编译学习会话图。

    Args:
        checkpointer: 任意 LangGraph checkpointer（InMemorySaver / SqliteSaver 等）。

    Returns:
        编译后的 StateGraph，可直接 stream_events / get_state / update_state。
    """
    builder = StateGraph(LearnState)
    builder.add_node("collect", collect_node)
    builder.add_node("dig", dig_node)
    builder.add_node("read", read_node)
    builder.add_node("note", note_node)
    builder.add_node("ask_level", ask_level_node)

    builder.add_conditional_edges(START, _route_command, {
        "collect": "collect",
        "dig": "dig",
        "read": "read",
        "note": "note",
        "ask_level": "ask_level",
    })
    # 按 level 分支：询问后进 collect；非法输入回 ask_level 再问
    builder.add_conditional_edges("ask_level", _route_by_level)

    for n in ("collect", "dig", "read", "note"):
        builder.add_edge(n, END)

    return builder.compile(checkpointer=checkpointer)


@contextmanager
def open_graph(checkpointer=None):
    """打开学习会话图：管理 SqliteSaver 连接生命周期（CLI 用）。

    Args:
        checkpointer: 可选；None 时用 SqliteSaver 持久化到
            ``config.GRAPH_DB_PATH``（.graph/checkpoints.sqlite）。

    Yields:
        编译后的图；with 块结束后关闭 checkpointer 连接。
    """
    if checkpointer is None:
        config.GRAPH_DB_DIR.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(config.GRAPH_DB_PATH)) as saver:
            saver.setup()
            yield build_graph(saver)
    else:
        yield build_graph(checkpointer)
