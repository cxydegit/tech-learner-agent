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

import json
import operator
import threading
import warnings
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .adapters.llm import ToolCallError, chat_with_tools
from .config import config
from .domain import exit_intent, survey
from .pipelines.collect import collect_pipeline
from .pipelines.note import format_merge_candidates, note_pipeline, parse_merge_decision, persist_points
from .pipelines.qa import qa_pipeline
from .pipelines.read import read_pipeline
from .pipelines.route import (COACH_TOOLS_BY_MODE, CoachCtx, coach_system_prompt,
                              run_coach_tool, run_kb_retrieve, run_memory_sweep,
                              summarize_conversation)

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


def _coach_thread_id() -> str | None:
    """读取当前运行配置的 thread_id（coach 工具把生成路线的会话记进 roadmap，供恢复定位）。"""
    try:
        from langgraph.config import get_config
        return (get_config().get("configurable") or {}).get("thread_id")
    except Exception:  # noqa: BLE001 —— 非图上下文取不到即返回 None
        return None


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
    if cmd == "route":
        return f"route {state.get('tech') or ''}".rstrip()
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

    # ---- 定制化学习路线（模块 2）：coach agent 循环 ----
    # 模式状态机：survey（问卷）→ planning（路线生成/确认）→ coaching（执行陪练，Step 4 接工具）。
    # 切换由确定性代码判定（_route_after_survey / confirm_roadmap 工具），模型只负责对话 + 选工具。
    mode: str
    # 模型上下文消息（openai 兼容 dict 列表，有界、覆盖写）：与 conversation（展示用）分离——
    # conversation 无界保留完整对话供前端渲染，coach_messages 只留最近几轮 + 摘要。
    coach_messages: list
    coach_summary: str  # 压缩摘要（后续阶段落地 LLM 摘要，当前为空字符串）
    # 问卷状态：answers 见 domain/survey；survey_field 当前待收集字段，None 表示进入动态诊断题
    survey_answers: dict
    survey_field: str | None
    learner_profile: dict  # 问卷完成后推导的用户画像（注入 planning/coaching 提示词）
    roadmap: dict | None  # 路线机器态（planning 生成）
    roadmap_path: str  # 路线 JSON 文件路径
    # 护栏：本用户回合已用工具调用数 + 最近几轮工具签名（重复检测）
    coach_turn_tool_count: int
    last_tool_signatures: list
    # note 工具暂存的相似笔记候选（note 提取后待用户决定，note_commit 提交后清空）
    coach_note_pending: dict | None
    # collect/read 工具最近一次产出的文档 {path, type}；coach_human 把它附到对话记录（查看完整文档 chip），用后即清
    coach_doc: dict | None
    # 记忆系统 Step 1：自上次沉淀以来的对话消息对 [{role, content}]（coach_human 填充，
    # coach_memory_write 达阈值触发 note 沉淀后清空；checkpointer 持久化，中断恢复不丢）
    memory_sweep_buffer: list
    # 记忆系统 Step 2：确定性读路由——当前用户回合命中知识库的相关片段 [{path, snippet}]
    # （coach_kb_retrieve 每用户回合覆盖；coaching 提示词注入作答上下文）
    kb_context: list | None


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
    # P3.1：索引失败不阻断沉淀，但要在产出里可见（8-19 事故：静默失败留缺口 6 天）
    index_failed = [r["topic"] for r in persisted["results"] if r.get("index_ok") is False]
    if index_failed:
        summary += (f"\n⚠️ RAG 索引更新失败：{'、'.join(index_failed)}"
                    f"（笔记已保存，下次运行 index 自动补齐）")
    # 记忆系统 Step 3：合并时发现的矛盾以新内容为准修正，报告透出给用户复核
    conflict_reports = persisted.get("conflict_reports") or []
    if conflict_reports:
        summary += "\n" + "\n".join(f"⚠️ 合并发现矛盾：{c['report']}" for c in conflict_reports)
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


# ============================================================
# coach 循环（模块 2 定制化学习路线）
# LangGraph canonical agent 结构：coach_llm ↔ (coach_tool | coach_human) ↔ coach_survey。
# 三模式（survey/planning/coaching）共用这一套节点，靠 mode 字段换提示词 + 工具集 +
# 边界路由；模式切换由确定性代码判定。护栏：工具预算 / 重复检测 / 退出意图 / recursion_limit。
# ============================================================


def coach_trim(state: LearnState) -> dict:
    """上下文管理 + 首次初始化。

    - 初始化：仅新线程兜底——mode 缺省置 survey、问卷起始字段置 self_level
      （跨会话恢复时两者已在 checkpoint 持久化，本节点不重复初始化）。
    - 压缩：消息总量超 COACH_COMPRESS_AT 时，旧消息经 LLM 摘要进 coach_summary，
      只保留最近 COACH_HISTORY_KEEP 轮。摘要失败降级为直接丢弃旧消息（保上下文有界）。

    ⚠️ 初始化判断要用「带 survey 默认值的有效 mode」，不能用裸 state.mode：
    新线程时它是 None；且本节点写入的 mode 要等返回后才生效。
    """
    msgs = state.get("coach_messages") or []
    keep = config.COACH_HISTORY_KEEP * 2  # 一轮 ≈ 一问一答两条
    updates: dict = {"coach_messages": msgs}
    mode = state.get("mode") or "survey"
    updates["mode"] = mode
    if mode == "survey" and not state.get("survey_field") and not state.get("survey_answers"):
        updates["survey_field"] = survey.SURVEY_FIELDS[0]  # 从第一问开始

    # 上下文压缩：超阈值 → 旧消息摘要压缩进 coach_summary，只留最近 N 轮
    if len(msgs) > config.COACH_COMPRESS_AT:
        old, recent = msgs[:-keep], msgs[-keep:]
        updates["coach_summary"] = summarize_conversation(
            state.get("coach_summary") or "", old, state.get("tech") or "")
        updates["coach_messages"] = recent
    return updates


def coach_llm(state: LearnState) -> dict:
    """调用模型（按 mode 注入提示词 + 工具集），追加一条 assistant 消息。"""
    msgs = state.get("coach_messages") or []
    try:
        result = chat_with_tools(
            coach_system_prompt(state),
            msgs,
            COACH_TOOLS_BY_MODE.get(state.get("mode") or "survey", []),
        )
    except ToolCallError as e:
        msg = f"⚠️ 模型调用暂时不可用（{e}）。请稍后再试，或输入「结束」退出。"
        return {"coach_messages": [*msgs, {"role": "assistant", "content": msg}],
                "last_output": msg}

    content = result.get("content")
    tool_calls = result.get("tool_calls") or []
    if not content and not tool_calls:
        # 模型空输出：给一条内部提示让它重新回复（recursion_limit 兜底防无限空转）
        return {"coach_messages": [*msgs, {"role": "system",
                                           "content": "（内部）你上一条输出为空，请直接回复用户或调用工具。"}]}
    assistant: dict = {"role": "assistant", "content": content}
    if tool_calls:
        assistant["tool_calls"] = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls
        ]
    return {"coach_messages": [*msgs, assistant]}


def _assistant_tool_calls(assistant_msg: dict) -> list[dict]:
    """把 assistant 消息里的 OpenAI 格式 tool_calls 归一化为 [{id, name, arguments(dict)}]。

    coach_llm 写入 coach_messages 时用 OpenAI 兼容格式（function.arguments 是 JSON 字符串），
    统一在读取侧归一化，避免两套解析逻辑（且 arguments 非法时兜底 {}，护栏兜住异常参数）。
    """
    out = []
    for tc in assistant_msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except Exception:  # noqa: BLE001 —— 非法 JSON 兜底空 dict
            args = {}
        out.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
    return out


def coach_tool(state: LearnState) -> dict:
    """执行模型请求的工具调用；工具异常回喂模型修正（LLM 可恢复错误，官方推荐模式）。"""
    tool_calls = _assistant_tool_calls(state["coach_messages"][-1])
    ctx = CoachCtx(state, progress=_get_progress(), thread_id=_coach_thread_id())
    results = []
    signatures = []
    for tc in tool_calls:
        args = tc["arguments"]
        signatures.append(f"{tc['name']}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}")
        try:
            out = run_coach_tool(tc["name"], args, ctx)
        except Exception as e:  # noqa: BLE001 —— 工具异常回喂模型修正
            out = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        results.append({"role": "tool", "tool_call_id": tc["id"],
                        "name": tc["name"], "content": json.dumps(out, ensure_ascii=False)})
    updates: dict = {
        "coach_messages": state["coach_messages"] + results,
        "last_tool_signatures": (state.get("last_tool_signatures") or []) + [signatures],
        "coach_turn_tool_count": (state.get("coach_turn_tool_count") or 0) + len(tool_calls),
    }
    for k, v in ctx.updates.items():  # 工具请求的状态变更（如 confirm_roadmap → mode=coaching）
        updates[k] = v
    return updates


def coach_human(state: LearnState) -> dict:
    """把模型消息展示给用户（interrupt），收集用户回答后回写 coach_messages。

    每个用户回合重置工具预算与重复检测计数。conversation 记录「AI 提问 + 用户回答」
    两条（chronological 顺序，供 Web 渲染；CLI 不读）。
    """
    msgs = state.get("coach_messages") or []
    content = (msgs[-1].get("content") or "") if msgs else ""
    reply = interrupt({
        "type": "coach_question",
        "mode": state.get("mode"),
        "tech": state.get("tech") or "",
        "message": content,
    })
    reply = (reply or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    updates: dict = {
        "coach_messages": [*msgs, {"role": "user", "content": reply}],
        "coach_turn_tool_count": 0,
        "last_tool_signatures": [],
        "last_output": "",
    }
    # collect/read 工具产出文档时，给本条 assistant 记录附上 doc chip（相对路径，前端白名单读取）
    assistant_rec: dict = {"role": "assistant", "type": "coach", "content": content, "ts": now}
    doc = state.get("coach_doc")
    if doc:
        rel = _rel_doc(doc.get("path"))
        if rel:
            assistant_rec["doc"] = rel
            assistant_rec["doc_type"] = doc.get("type") or "read"
        updates["coach_doc"] = None  # 只附着一次，避免给后续普通消息误挂 chip
    updates["conversation"] = [
        assistant_rec,
        {"role": "user", "type": "chat", "content": reply, "ts": now},
    ]
    # 记忆系统 Step 1：仅 coaching 模式且非待确认笔记流时，把本回合（assistant 讲解 + 用户回复）
    # 压入沉淀缓冲（coach_memory_write 达阈值触发 note 沉淀后清空）
    if state.get("mode") == "coaching" and not state.get("coach_note_pending"):
        buf = list(state.get("memory_sweep_buffer") or [])
        if content:
            buf.append({"role": "assistant", "content": content})
        if reply:
            buf.append({"role": "user", "content": reply})
        updates["memory_sweep_buffer"] = buf
    return updates


def coach_survey(state: LearnState) -> dict:
    """问卷回答处理：按当前字段确定性解析推进；解析失败给模型一条校验提示重问。

    问卷完成后在**节点内**确定性写 mode=planning（langgraph 1.2.10 条件边不支持返回
    Command，模式切换统一在节点内完成）。非 survey 模式直接透传（所有模式的
    post-human 汇聚点）。
    """
    if state.get("mode") != "survey":
        return {}
    answers = dict(state.get("survey_answers") or {})
    reply = state["coach_messages"][-1].get("content") or ""
    field = state.get("survey_field")
    updates: dict = {}
    if field is not None:
        new_answers, err = survey.apply_answer(answers, field, reply)
        if err:
            note = (f"[问卷校验] 用户对「{survey.FIELD_QUESTIONS.get(field, field)}」的回答"
                    f"『{reply[:50]}』无法解析：{err}。请重新问同一个问题，并提示输入格式。")
            updates["coach_messages"] = state["coach_messages"] + [
                {"role": "system", "content": note}]
            return updates
        answers = new_answers
        updates["survey_answers"] = answers
        updates["survey_field"] = survey.next_field(answers)
    else:
        # 动态诊断题阶段：自由文本，直接收集
        diagnostics = list(answers.get("diagnostics") or [])
        diagnostics.append(reply)
        answers["diagnostics"] = diagnostics
        updates["survey_answers"] = answers
    if survey.is_fixed_done(answers) and not state.get("learner_profile"):
        updates["learner_profile"] = survey.derive_profile(answers, state.get("tech") or "")
    if survey.is_survey_complete(answers):
        updates["mode"] = "planning"  # 问卷完成 → 确定性进入路线规划
    return updates


def coach_memory_write(state: LearnState) -> dict:
    """确定性写触发（记忆系统 Step 1）：coach 对话积累超阈值 → 自动沉淀学习内容进知识库。

    仅 coaching 模式生效；agent 的 note 工具流进行中（coach_note_pending 非空）时跳过。
    - 未达阈值 → 保留 memory_sweep_buffer 继续积累；
    - 触发后清空 buffer：
      - 无新内容 → 无动作（闲聊/过程消息被差量提取过滤）；
      - 新知识点无候选 → 自动落库 + system 提示（agent 可自然提及）；
      - 有相似候选 → 暂存 coach_note_pending，复用 note_commit 工具流由用户确认。

    本节点只做编排，管道逻辑在 pipelines.route.run_memory_sweep。
    """
    if state.get("mode") != "coaching":
        return {}
    buffer = state.get("memory_sweep_buffer") or []
    if not buffer or state.get("coach_note_pending"):
        return {}
    turns = sum(1 for m in buffer if m.get("role") == "user")
    chars = sum(len(m.get("content") or "") for m in buffer)
    if turns < config.ROUTE_MEMORY_SWEEP_TURNS and chars < config.ROUTE_MEMORY_SWEEP_CHARS:
        return {}  # 未达阈值：保留 buffer 继续积累
    sweep = run_memory_sweep(state.get("tech") or "", buffer, progress=_get_progress())
    updates: dict = {"memory_sweep_buffer": []}  # 触发后清空，下一窗口重新积累
    if sweep["action"] == "skip":
        return updates
    msgs = state.get("coach_messages") or []
    updates["coach_messages"] = [*msgs, {"role": "system", "content": sweep["message"]}]
    if sweep["action"] == "pending":
        updates["coach_note_pending"] = sweep["pending"]
    return updates


def coach_kb_retrieve(state: LearnState) -> dict:
    """确定性读路由（记忆系统 Step 2）：coach 用户每回合提问先查库，命中注入 kb_context。

    仅 coaching 模式生效；取最后一条用户消息过两级闸门（廉价元问题闸门 + 质量相似度闸门），
    命中片段写入 kb_context（coaching 提示词注入作答上下文），无命中 / 低于阈值 → 清空。
    每用户回合覆盖（下轮问题自动替换）；检索异常在 run_kb_retrieve 内优雅降级为空。
    """
    if state.get("mode") != "coaching":
        return {}
    msgs = state.get("coach_messages") or []
    question = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            question = m.get("content") or ""
            break
    ctx = run_kb_retrieve(state.get("tech") or "", question)
    return {"kb_context": ctx or None}


# ---------- coach 路由 ----------


def coach_guard(state: LearnState) -> dict:
    """护栏节点：把诊断消息作为 assistant 消息插入，随后 coach_human 展示并等用户输入。"""
    msg = _coach_guard(state)
    if not msg:
        return {}  # 理论不可达（只在护栏触发时路由到本节点）
    return {"coach_messages": state["coach_messages"] + [{"role": "assistant", "content": msg}]}


def _coach_guard(state: LearnState) -> str | None:
    """工具护栏判定：预算耗尽 / 连续重复调用 → 返回诊断消息；无违规返回 None。"""
    count = state.get("coach_turn_tool_count") or 0
    if count >= config.ROUTE_MAX_TOOL_CALLS_PER_TURN:
        return ("本轮工具调用次数已达上限，我暂时停一下。请告诉我下一步方向，"
                "或直接说「结束」。")
    last_tc = _assistant_tool_calls(state["coach_messages"][-1])
    cur = [f"{tc['name']}:{json.dumps(tc['arguments'], sort_keys=True, ensure_ascii=False)}"
           for tc in last_tc]
    sigs = state.get("last_tool_signatures") or []
    if len(sigs) >= 2 and sigs[-1] == cur and sigs[-2] == cur:
        return ("我连续在重复做同一件事，可能卡住了。请帮我确认接下来该怎么做"
                "（或说「结束」退出）。")
    return None


def _route_coach(state: LearnState) -> str:
    """coach_llm 条件边：模型产出后决定下一步（assistant 消息才需要分流）。

    - assistant 带 tool_calls → 过护栏：违规去 coach_guard（问用户），否则 coach_tool
    - assistant 纯内容 → coach_human（interrupt 展示给用户）
    - tool / system / user 消息 → coach_trim 继续（用户消息已由 coach_survey 预处理）
    """
    msgs = state.get("coach_messages") or []
    if not msgs:
        return "coach_trim"
    last = msgs[-1]
    if last.get("role") == "assistant":
        if last.get("tool_calls"):
            if _coach_guard(state):
                return "coach_guard"
            return "coach_tool"
        return "coach_human"
    return "coach_trim"


def _route_after_human(state: LearnState) -> str:
    """coach_human 条件边：用户刚回答——退出意图（确定性正则）→ END；否则 coach_survey 处理。"""
    msgs = state.get("coach_messages") or []
    if msgs and msgs[-1].get("role") == "user" and exit_intent.is_exit_intent(msgs[-1].get("content")):
        return END
    return "coach_survey"


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
        "route": "coach_trim",
    })

    # note 两段式：提取（昂贵 LLM，只跑一次）→ 确认入库（interrupt 点，resume 只重跑它）
    builder.add_conditional_edges("note_extract", _route_note,
                                  {"confirm": "note_confirm", "end": END})
    builder.add_edge("note_confirm", END)

    for n in ("collect", "read", "qa"):
        builder.add_edge(n, END)

    # coach 循环（模块 2）：llm ↔ (tool | human | guard) ↔ survey，共用 trim 保底上下文有界。
    # 模式切换（survey→planning→coaching）全部在节点内写 mode，条件边只做路由
    # （langgraph 1.2.10 条件边不支持返回 Command）。
    builder.add_node("coach_trim", coach_trim)
    builder.add_node("coach_llm", coach_llm)
    builder.add_node("coach_tool", coach_tool)
    builder.add_node("coach_human", coach_human)
    builder.add_node("coach_survey", coach_survey)
    builder.add_node("coach_guard", coach_guard)
    builder.add_node("coach_memory_write", coach_memory_write)
    builder.add_node("coach_kb_retrieve", coach_kb_retrieve)

    builder.add_edge("coach_trim", "coach_llm")
    builder.add_conditional_edges("coach_llm", _route_coach, {
        "coach_tool": "coach_tool",
        "coach_human": "coach_human",
        "coach_guard": "coach_guard",
        "coach_trim": "coach_trim",
    })
    builder.add_edge("coach_tool", "coach_trim")
    builder.add_conditional_edges("coach_human", _route_after_human, {
        "coach_survey": "coach_survey",
        END: END,
    })
    builder.add_edge("coach_guard", "coach_human")
    # 记忆系统 Step 1+2：用户回复后先走确定性写触发（coach_memory_write），
    # 再走确定性读路由（coach_kb_retrieve，提问先查库、命中注入 kb_context），最后进 trim→llm
    builder.add_edge("coach_survey", "coach_memory_write")
    builder.add_edge("coach_memory_write", "coach_kb_retrieve")
    builder.add_edge("coach_kb_retrieve", "coach_trim")

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
