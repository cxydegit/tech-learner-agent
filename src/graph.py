"""LangGraph 有状态编排（Stage 3）：/learn 会话的状态图 + 人机交互点。

架构定位：确定性管道（collect/read/note）是图的"叶子节点"，图负责编排——
有状态、可中断、跨会话：
- ``StateGraph(LearnState)``：按 ``command`` 条件路由到对应管道节点
- note 两段式（``note_extract`` → ``note_confirm``）：提取只跑一次，合并确认的 ``interrupt()``
  在 ``note_confirm`` 暂停图，等 CLI 用 ``Command(resume=...)`` 恢复 —— resume 只重跑确认节点，不重跑昂贵提取
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
import threading
import warnings
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import config
from .pipelines.collect import collect_pipeline
from .pipelines.note import format_merge_candidates, note_pipeline, parse_merge_decision, persist_points
from .pipelines.qa import qa_pipeline
from .pipelines.read import read_pipeline

# langgraph 1.0 的 v3 流式协议是实验性的，会打 LangChainBetaWarning；CLI 里主动过滤
warnings.filterwarnings("ignore", message="The v3 streaming protocol on Pregel is experimental.")

# Web 流式进度注入：按 thread_id 的全局注册表，节点从 config 的 thread_id 反查进度回调。
# ⚠️ 不用 ContextVar：实测 langgraph stream_events(v3) 在 ThreadPoolExecutor 线程执行节点，
# 新线程不继承调用线程的 contextvars，节点里读不到 → progress 静默。注册表全局可读，任何线程都能取到。
# CLI 不注册 → _get_progress() 返回 None，进度保持静默（行为不变）。
_progress_registry: dict[str, Callable[[str], None]] = {}
_progress_lock = threading.Lock()


@contextmanager
def web_progress(thread_id: str, progress: Callable[[str], None] | None):
    """把 Web 的流式进度回调按 thread_id 注册进节点（Web 用；CLI 不调用保持静默）。"""
    with _progress_lock:
        _progress_registry[thread_id] = progress
    try:
        yield
    finally:
        with _progress_lock:
            _progress_registry.pop(thread_id, None)


def _get_progress() -> Callable[[str], None] | None:
    """节点内读取当前 run 的进度回调（从 config 的 thread_id 反查注册表）。"""
    try:
        from langgraph.config import get_config
        tid = (get_config().get("configurable") or {}).get("thread_id")
        if tid:
            with _progress_lock:
                return _progress_registry.get(tid)
    except Exception:  # noqa: BLE001 —— CLI / 非图上下文取不到即返回 None
        pass
    return None


def _user_message(state: "LearnState", *, decision: str | None = None) -> str:
    """从图状态重建本轮的用户输入显示文本（写进 conversation，历史会话重载用）。

    qa 节点对应卡片命令名 ask（graph 路由 key 是 qa），显示为 `ask <问题>` 与前端卡片一致。
    note 节点在中断恢复后可带上用户的合并决策（all / 编号 / skip）。
    """
    cmd = state.get("command") or ""
    if cmd == "collect":
        text = f"collect {state.get('tech') or ''}".rstrip()
        if state.get("focus"):
            text += f" {state['focus']}"
        return text
    if cmd == "read":
        return f"read {state['args'][0] if state.get('args') else ''}".rstrip()
    if cmd == "qa":
        return f"ask {state['args'][0] if state.get('args') else ''}".rstrip()
    if cmd == "note":
        text = "note"
        if decision:
            text += f"（合并决策：{decision.strip()}）"
        return text
    return cmd or "note"


def _rel_doc(path: str | None) -> str | None:
    """把产出文档的绝对路径转成相对 BASE_DIR 的 posix 路径（供 Web 阅读器白名单读取）。"""
    if not path:
        return None
    try:
        from pathlib import Path
        rel = Path(path).resolve().relative_to(config.BASE_DIR.resolve())
        return rel.as_posix()
    except Exception:  # noqa: BLE001 —— 非 BASE_DIR 下的路径返回 None（前端不显示阅读全文）
        return None


def _conversation(state: "LearnState", assistant_content: str, node_type: str,
                  *, decision: str | None = None, doc: str | None = None,
                  sources: list[dict] | None = None) -> list[dict]:
    """构造一对话轮次的「用户输入 + AI 回复」两条记录。

    conversation 是 operator.add reducer，节点返回的两条会被 append 累加；
    Web/CLI 共用同一状态，CLI 不读 conversation，行为零变化。ts 同轮取同一时间戳，
    前端按列表顺序渲染即可。doc 为 collect/read 产出的文档相对路径（Web「阅读全文」chip 用）；
    sources 为 qa 的来源笔记（Web「查看来源笔记」卡片用）。
    """
    now = datetime.now().isoformat(timespec="seconds")
    assistant: dict = {"role": "assistant", "type": node_type, "content": assistant_content, "ts": now}
    if doc:
        assistant["doc"] = doc
    if sources:
        assistant["sources"] = sources
    return [
        {"role": "user", "type": "command", "content": _user_message(state, decision=decision), "ts": now},
        assistant,
    ]


class LearnState(TypedDict):
    """跨命令会话状态（字段定义对应 session.LearnSession，持久化交给 checkpointer）。

    用 reducer 的字段（urls / visited / notes）跨命令累加；普通字段直接覆盖。
    """

    tech: str
    # collect 的自由文本关注点（用户提示词）：无 → 固定模板；有 → 非固定模板。
    # CLI 由 domain/card_input.parse_card_input 产出；未来 Web 卡片输入同样落到此字段。
    focus: str
    materials_path: str  # 最近一次 collect 的 materials 报告（note 无新内容时推荐方向用）
    urls: Annotated[list[str], operator.add]
    visited: Annotated[list[str], operator.add]
    notes: Annotated[list[dict], operator.add]
    # qa 多轮对话记录：每轮 /ask 的 {question, answer, sources, no_hit}。
    # 跨命令累加 + checkpointer 持久化 → 多轮上下文靠它，web 化后即会话记录。
    qa_history: Annotated[list[dict], operator.add]
    # note 差量提取的游标：已处理过的 report 条数（普通字段，被 note_node 覆盖更新）。
    # 作用是让第二次 note 只处理上次 note 之后新 read 的 report，避免重复提取已沉淀内容。
    noted_count: int
    # note 两段式的中间产物：note_extract 跑完昂贵 LLM 提取后暂存管道结果，note_confirm 据此
    # 做确认（interrupt）+ 入库。存状态是为了让 resume 只重跑 confirm、不重跑提取（普通字段覆盖写）。
    note_result: dict | None
    # CLI 命令路由输入
    command: str  # collect / read / note / qa
    args: list[str]
    last_output: str  # 节点输出，CLI 展示用
    # 会话标题：首次 collect 时固化为技术名，之后不再随动作改变（Web 会话列表用）
    title: str
    # Web 对话流：{role: user|assistant, type, content, ts} 累加记录，跨命令持久化。
    # Web 按轮渲染「用户输入 + AI 回复」，历史会话重载直接读它（§4-①）。
    # CLI 不读此字段，纯增量不破坏现有渲染。
    conversation: Annotated[list[dict], operator.add]


# ============================================================
# 节点：薄包装确定性管道（只读状态 → 调管道 → 返回增量）
# ============================================================


def collect_node(state: LearnState) -> dict:
    """按 tech + focus（可选）运行资料收集管道。"""
    tech = state["tech"]
    focus = state.get("focus")
    result = collect_pipeline(tech, focus, progress=_get_progress())
    report = result["report"]
    return {
        "urls": result["urls"],
        "tech": tech,
        "materials_path": result["materials_path"],
        "last_output": report,
        "title": state.get("title") or tech,  # 首次 collect 固化标题，之后不变
        "conversation": _conversation(state, report, "collect", doc=_rel_doc(result["materials_path"])),
    }


def read_node(state: LearnState) -> dict:
    """运行文档解读管道；结果写入 visited / notes。"""
    url = state["args"][0]
    result = read_pipeline(url, progress=_get_progress())
    if result.get("error"):
        out = f"❌ {result['error']}"
        return {"last_output": out, "conversation": _conversation(state, out, "read")}
    report = result["report"]
    return {"visited": [url], "notes": result["notes"], "last_output": report,
            "conversation": _conversation(state, report, "read", doc=_rel_doc(result["report_path"]))}


def note_extract_node(state: LearnState) -> dict:
    """差量提取阶段：召回 → LLM 提取 → 匹配，把可沉淀内容暂存进 note_result。

    与 note_confirm_node 拆分的关键：note_pipeline 是昂贵 LLM 调用，只应在首轮执行一次。
    若 interrupt 与提取放同一节点，resume 会重跑整节点 → LLM 重复提取、候选可能漂移、
    且慢（Web 表现为反复"执行中"）。拆分后 resume 只重跑 note_confirm（快），提取不重跑。

    游标 noted_count 记录已处理过的 report 条数，notes 里 `n.get("report")` 为真的条目
    才计入（persist 结果不含 report 键，天然被排除）。每次 note 只取 reports[noted_count:]，
    处理完无论是否新增，游标都推进到当前 report 总数，避免下一轮重复提取已沉淀内容。

    **不要求先 collect**：只要还有未沉淀的解读 report，即可沉淀（tech 为空时笔记落入知识库根目录）。
    """
    notes = state.get("notes") or []
    reports = [n for n in notes if n.get("report")]
    start = state.get("noted_count") or 0
    if start >= len(reports):
        if not reports:
            out = "⚠ 没有可沉淀的内容，先 read 一些文档"
        else:
            out = "ℹ 已 read 的内容都已沉淀过，先 read 新文档或 collect 新方向"
        # 不返回 noted_count：游标保持原值，避免无 report 时误重置；同时清掉过期 note_result
        return {"note_result": None, "last_output": out, "conversation": _conversation(state, out, "note")}

    content = _notes_to_content(reports[start:])
    result = note_pipeline((state.get("tech") or "").strip(), content,
                           materials_path=state.get("materials_path"), progress=_get_progress())

    # 无新内容：不沉淀 + 可选方向推荐（游标照常前进，避免反复重试同一批 report）
    if result["empty_reason"]:
        out = f"ℹ {result['empty_reason']}，未沉淀"
        if result.get("suggestion"):
            out += f"\n\n📌 建议继续学习的方向：\n{result['suggestion']}"
        return {"note_result": None, "noted_count": len(reports), "last_output": out,
                "conversation": _conversation(state, out, "note")}

    # 有可沉淀内容：交给 note_confirm 做确认 + 入库（interrupt 只发生在确认节点）
    return {"note_result": result}


def note_confirm_node(state: LearnState) -> dict:
    """确认 + 入库阶段：有合并候选则 interrupt 征求用户决定，随后差量合并入库。

    本节点是图唯一的 interrupt 点。resume 时 langgraph 只重跑本节点，读取已算好的
    note_result（状态持久化），`interrupt()` 直接返回用户决策 → 解析 → 入库，不触发 LLM 重复提取。
    decision 记录进 conversation 的用户消息（历史会话重载可见合并决策）。
    """
    tech = (state.get("tech") or "").strip()
    result = state["note_result"]  # note_extract 已算出（普通字段覆盖写，必存在）
    reports = [n for n in (state.get("notes") or []) if n.get("report")]

    # 有合并候选：interrupt 汇总展示，用户统一决定 全合并/逐条/跳过
    merge_indices: set[int] = set(range(len(result["merge_candidates"])))
    decision: str | None = None
    if result["merge_candidates"]:
        decision = interrupt(format_merge_candidates(result["merge_candidates"]))
        merge_indices = parse_merge_decision(decision, len(result["merge_candidates"]))

    # 用户确认后入库
    persisted = persist_points(tech, result["new_points"], result["merge_candidates"], merge_indices)
    summary = (
        f"新增 {persisted['new_count']} 篇，合并更新 {persisted['merged_count']} 篇"
        if persisted["results"] else "未沉淀任何知识点"
    )
    return {"notes": persisted["results"], "noted_count": len(reports), "last_output": summary,
            "conversation": _conversation(state, summary, "note", decision=decision)}


def qa_node(state: LearnState) -> dict:
    """跨笔记联想检索 Q&A：问知识库 → LLM 综合回答 + 来源标注。

    不依赖会话 tech 主题：恒以 tech=None 跨全部笔记检索（"闭包"类问题天生跨笔记），
    全新会话也能直接 /ask。结果 append 进 qa_history（checkpointer 持久化）。
    """
    args = state.get("args") or []
    question = args[0] if args else ""
    history = (state.get("qa_history") or [])[-config.QA_HISTORY_ROUNDS:]
    result = qa_pipeline(question, tech=None, history=history, progress=_get_progress())
    exchange = {
        "question": question,
        "answer": result["answer"],
        "sources": result["sources"],
        "no_hit": result["no_hit"],
    }
    out = _render_qa(result)
    # sources 精简版写进 conversation（含 path/topic/similarity，供前端来源卡片），去 snippet 控体积
    src = [{"path": s.get("path"), "topic": s.get("topic"), "similarity": s.get("similarity")}
           for s in result["sources"]]
    return {"qa_history": [exchange], "last_output": out,
            "conversation": _conversation(state, out, "qa", sources=src)}


def _render_qa(result: dict) -> str:
    """把 qa_pipeline 结果渲染成面向用户的 Markdown。

    只渲染 AI 答案（答案已按 QA_PROMPT 要求逐条内联标注来源）；检索零命中时如实告知。
    no_hit 也可能是「模型明确说笔记里没有记录」——此时 answer 非空且有信息量
    （如「笔记里没有记录 Redis 分布式锁的实现原理。」），直接展示模型原话，不用通用提示替换。
    sources 数据仍保留在 qa_history 中（供未来 Web 端做来源卡片），不在此展示原文片段。
    """
    if result["no_hit"] and not result.get("answer"):
        return "未在笔记库中找到相关内容。"
    return result.get("answer") or "（无回答）"


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
    """START 条件边：按 command 路由到对应节点。当图被意外触发且未指明命令时，默认执行 note 提取。"""
    return state.get("command", "note")


def _route_note(state: LearnState) -> str:
    """note_extract 后路由：有可沉淀内容（note_result 已算出）→ note_confirm 确认入库；否则收尾。"""
    return "confirm" if state.get("note_result") else "end"


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
    builder.add_node("read", read_node)
    builder.add_node("note_extract", note_extract_node)
    builder.add_node("note_confirm", note_confirm_node)
    builder.add_node("qa", qa_node)

    builder.add_conditional_edges(START, _route_command, {
        "collect": "collect",
        "read": "read",
        "note": "note_extract",
        "qa": "qa",
    })

    # note 两段式：提取（昂贵 LLM，只跑一次）→ 确认入库（interrupt 点，resume 只重跑它）
    builder.add_conditional_edges("note_extract", _route_note,
                                  {"confirm": "note_confirm", "end": END})
    builder.add_edge("note_confirm", END)

    for n in ("collect", "read", "qa"):
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
