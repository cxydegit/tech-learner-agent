"""LangGraph 有状态编排（Stage 3）：/learn 会话的状态图 + 人机交互点。

架构定位：确定性管道（collect/dig/read/note）是图的"叶子节点"，图负责编排——
有状态、可中断、跨会话：
- ``StateGraph(LearnState)``：按 ``command`` 条件路由到对应管道节点
- ``ask_level`` 节点用 ``interrupt()`` 实现模块 2 水平探测的交互点
  （图暂停 → CLI 询问 → ``Command(resume=...)`` 恢复）
- ``SqliteSaver`` checkpointer 跨会话/跨进程持久化（替换 session.py 的 JSON 落盘）

节点不再 print：进度回调传 None，输出只经 ``last_output`` 返回，由 CLI 层渲染。
已知取舍：/learn 失去分步进度行（后续可用 ``status: Annotated[list[str], operator.add]``
状态字段补，本阶段不做）。

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

from .config import config
from .pipelines.collect import collect_pipeline, dig_pipeline
from .pipelines.note import format_merge_candidates, note_pipeline, parse_merge_decision, persist_points
from .pipelines.read import read_pipeline

# langgraph 1.0 的 v3 流式协议是实验性的，会打 LangChainBetaWarning；CLI 里主动过滤
warnings.filterwarnings("ignore", message="The v3 streaming protocol on Pregel is experimental.")

VALID_LEVELS = ("入门", "进阶")


class LearnState(TypedDict):
    """跨命令会话状态（字段定义对应 session.LearnSession，持久化交给 checkpointer）。

    用 reducer 的字段（urls / visited / notes）跨命令累加；普通字段直接覆盖。
    """

    tech: str
    level: str
    materials_path: str  # 最近一次 collect/dig 的 materials 报告（note 无新内容时推荐方向用）
    urls: Annotated[list[str], operator.add]
    visited: Annotated[list[str], operator.add]
    notes: Annotated[list[dict], operator.add]
    # note 差量提取的游标：已处理过的 report 条数（普通字段，被 note_node 覆盖更新）。
    # 作用是让第二次 note 只处理上次 note 之后新 read 的 report，避免重复提取已沉淀内容。
    noted_count: int
    # CLI 命令路由输入
    command: str  # collect / dig / read / note / ask_level
    args: list[str]
    last_output: str  # 节点输出，CLI 展示用


# ============================================================
# 节点：薄包装确定性管道（只读状态 → 调管道 → 返回增量）
# ============================================================


def collect_node(state: LearnState) -> dict:
    """按 tech + level 运行资料收集管道。"""
    tech = state["tech"]
    level = state.get("level") or "入门"
    result = collect_pipeline(tech, level, progress=None)
    return {
        "urls": result["urls"],
        "tech": tech,
        "level": level,
        "materials_path": result["materials_path"],
        "last_output": result["report"],
    }


def dig_node(state: LearnState) -> dict:
    """按 tech + direction 运行资料深挖管道。"""
    tech = state["tech"]
    direction = " ".join(state.get("args") or [])
    result = dig_pipeline(tech, direction, progress=None)
    return {
        "urls": result["urls"],
        "tech": tech,
        "materials_path": result["materials_path"],
        "last_output": result["report"],
    }


def read_node(state: LearnState) -> dict:
    """运行文档解读管道；结果写入 visited / notes。"""
    url = state["args"][0]
    result = read_pipeline(url, progress=None)
    if result.get("error"):
        return {"last_output": f"❌ {result['error']}"}
    return {"visited": [url], "notes": result["notes"], "last_output": result["report"]}


def note_node(state: LearnState) -> dict:
    """把「上次 note 之后新解读的 report」差量沉淀为知识笔记；有合并候选时用 interrupt 让用户确认。

    游标 noted_count 记录已处理过的 report 条数，notes 里 `n.get("report")` 为真的条目
    才计入（persist 结果不含 report 键，天然被排除）。每次 note 只取 reports[noted_count:]，
    处理完无论是否新增，游标都推进到当前 report 总数，避免下一轮重复提取已沉淀内容。
    """
    tech = state.get("tech") or ""
    if not tech:
        return {"last_output": "⚠ 会话还没有技术主题，先 collect <技术名>"}

    notes = state.get("notes") or []
    reports = [n for n in notes if n.get("report")]
    start = state.get("noted_count") or 0
    if start >= len(reports):
        if not reports:
            return {"last_output": "⚠ 没有可沉淀的内容，先 read 一些文档"}
        return {"last_output": "ℹ 已 read 的内容都已沉淀过，先 read 新文档或 dig 新方向"}

    content = _notes_to_content(reports[start:])
    result = note_pipeline(tech, content, materials_path=state.get("materials_path"), progress=None)

    # 无新内容：不沉淀 + 可选方向推荐（游标照常前进，避免反复重试同一批 report）
    if result["empty_reason"]:
        out = f"ℹ {result['empty_reason']}，未沉淀"
        if result.get("suggestion"):
            out += f"\n\n📌 建议继续学习的方向：\n{result['suggestion']}"
        return {"noted_count": len(reports), "last_output": out}

    # 有合并候选：interrupt 汇总展示，用户统一决定 全合并/逐条/跳过
    merge_indices: set[int] = set(range(len(result["merge_candidates"])))
    if result["merge_candidates"]:
        answer = interrupt(format_merge_candidates(result["merge_candidates"]))
        merge_indices = parse_merge_decision(answer, len(result["merge_candidates"]))

    # 用户确认后入库
    persisted = persist_points(tech, result["new_points"], result["merge_candidates"], merge_indices)
    summary = (
        f"新增 {persisted['new_count']} 篇，合并更新 {persisted['merged_count']} 篇"
        if persisted["results"] else "未沉淀任何知识点"
    )
    return {"notes": persisted["results"], "noted_count": len(reports), "last_output": summary}


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
