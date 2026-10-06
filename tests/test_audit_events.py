"""阶段 1 各审计事件的字段断言（零网络）。

事件按"谁在改变世界"分族，这里逐个钉住它们真的发出来、字段对得上、且不泄内容：
编排（tool_call / args_parse_failed）／否决（guard_trigger / tool_gate / milestone_verify）／
中断（interrupt / resume）／上下文（ctx_compact / sweep_fire / sweep_drain）／
副作用（note_persist / artifact_write / index_reconcile）／分步（pipeline_step）／错误（error）。
"""

import json
import logging
import time

import pytest

import src.graph as graph_mod
import src.pipelines.note as note_mod
import src.pipelines.route as route_mod
import src.web.runner as runner_mod
from src.adapters import vector as vector_mod
from src.adapters.audit import audit
from src.adapters.store import save_file_tool
from src.config import config
from src.domain import roadmap as rm
from src.graph import _tool_sig_hash, _tool_signature
from src.pipelines.collect import _step_recorder
from src.pipelines.route import CoachCtx, tool_arg_fields

pytestmark = pytest.mark.filterwarnings("ignore:The v3 streaming protocol on Pregel is experimental.")


def _events(events, name):
    return [e for e in events if e["event"] == name]


# ============================================================
# 1-1 编排：tool_call / args_parse_failed
# ============================================================

def test_arg_whitelist_never_leaks_note_content():
    """note 的 content 上限 2000 字，绝不能整包落盘——白名单漏了就等于把学习内容写进日志。"""
    secret = "用户的私有笔记正文" * 100
    fields = tool_arg_fields("note", {"tech": "redis", "content": secret})

    assert fields == {"tech": "redis"}
    assert secret[:20] not in json.dumps(fields, ensure_ascii=False)


def test_arg_whitelist_truncates_free_text_and_counts_lists():
    """自由文本只留长度 + 首 20 字；stages 是整份学习计划，只记条数。"""
    fields = tool_arg_fields("revise_roadmap",
                             {"goal": "目标" * 30, "total_hours": 12,
                              "stages": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})

    assert fields["goal_preview"] == "目标" * 10
    assert fields["goal_chars"] == 60
    assert fields["stages_count"] == 3 and "stages" not in fields
    assert fields["total_hours"] == 12


def test_arg_whitelist_keeps_short_identifiers():
    fields = tool_arg_fields("collect", {"tech": "Spring Boot", "focus": "异步编程"})
    assert fields == {"tech": "Spring Boot", "focus": "异步编程"}


def test_tool_sig_joinable_with_guard():
    """事件里的 sig 与护栏判重复用的签名必须同源，否则两类事件 join 不起来。"""
    args = {"tech": "Redis", "focus": "缓存"}
    assert _tool_sig_hash("collect", args) == _tool_sig_hash("collect", dict(reversed(list(args.items()))))
    assert _tool_sig_hash("collect", args) != _tool_sig_hash("collect", {"tech": "Redis"})
    assert _tool_signature("collect", {"a": 1}) == 'collect:{"a": 1}'


def _assistant(name="collect", args=None, tc_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": tc_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args if args is not None else {"tech": "Redis"},
                                                                 ensure_ascii=False)}}]}


def test_tool_call_event_passes_status_through(monkeypatch, audit_events):
    """blocked / rejected 是"代码否决了模型"的实锤，不能被折叠成 error。"""
    monkeypatch.setattr(graph_mod, "run_coach_tool",
                        lambda name, args, ctx: {"status": "blocked", "hint": "别重跑"})
    graph_mod.coach_tool({"coach_messages": [_assistant()]})

    event = _events(audit_events, "tool_call")[-1]
    assert event["name"] == "collect"
    assert event["status"] == "blocked"
    assert event["tech"] == "Redis"
    assert event["elapsed_s"] >= 0
    assert len(event["sig"]) == 12


def test_tool_call_event_records_exception_as_error(monkeypatch, audit_events):
    def boom(name, args, ctx):
        raise RuntimeError("工具炸了")

    monkeypatch.setattr(graph_mod, "run_coach_tool", boom)
    graph_mod.coach_tool({"coach_messages": [_assistant()]})

    event = _events(audit_events, "tool_call")[-1]
    assert event["status"] == "error" and "工具炸了" in event["error"]


def test_args_parse_failed_is_recorded(monkeypatch, audit_events):
    """模型给了不合法 JSON 参数时兜底 {}，但必须留痕——否则看起来像"模型决定的空参数"。"""
    import src.adapters.llm as llm_mod

    msg = type("Msg", (), {
        "content": None,
        "tool_calls": [type("TC", (), {
            "id": "c1", "function": type("F", (), {"name": "collect",
                                                   "arguments": '{"tech": "Red'})()})()],
    })()
    llm_mod._parse_chat_response(msg, call_site="coach.chat")

    event = _events(audit_events, "args_parse_failed")[-1]
    assert event["tool"] == "collect" and event["site"] == "coach.chat"
    assert event["raw_chars"] == len('{"tech": "Red')


# ============================================================
# 1-2 代码否决模型：guard_trigger / tool_gate
# ============================================================

def test_guard_trigger_budget(audit_events):
    state = {"coach_messages": [_assistant()], "coach_turn_tool_count": 8}
    graph_mod.coach_guard(state)

    event = _events(audit_events, "guard_trigger")[-1]
    assert event["kind"] == "budget" and event["turn_count"] == 8


def _multi_tool_msg(*calls):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                           for i, (name, args) in enumerate(calls)]}


def test_guard_trigger_heavy_cap(audit_events):
    # 同一批里 3 条贵工具：已用 1 + 本批 3 > 上限 2 → 拦下来问用户先做哪个
    graph_mod.coach_guard({
        "coach_messages": [_multi_tool_msg(("collect", {"tech": "A"}),
                                           ("read", {"url": "u"}),
                                           ("collect", {"tech": "B"}))],
        "coach_turn_heavy_count": 1,
    })

    event = _events(audit_events, "guard_trigger")[-1]
    assert event["kind"] == "heavy_cap" and event["pending_heavy"] == 3
    assert event["names"] == ["collect", "read", "collect"]


def test_guard_trigger_repeat(audit_events):
    msg = _assistant("collect", {"tech": "Redis"})
    cur = [_tool_signature("collect", {"tech": "Redis"})]
    state = {"coach_messages": [msg], "last_tool_signatures": [cur, cur]}
    graph_mod.coach_guard(state)

    event = _events(audit_events, "guard_trigger")[-1]
    assert event["kind"] == "repeat"
    assert event["sigs"] == [_tool_sig_hash("collect", {"tech": "Redis"})]


def test_no_guard_event_without_violation(audit_events):
    graph_mod.coach_guard({"coach_messages": [_assistant()]})
    assert not _events(audit_events, "guard_trigger")


def test_tool_gate_heavy_blocked(audit_events):
    """贵工具失败闸：同回合内失败过的贵工具不再放行重跑。"""
    ctx = CoachCtx({"tech": "redis", "coach_heavy_failures": {"collect": {"count": 2, "stage": "generate"}}})
    out = route_mod._heavy_blocked(ctx, "collect")

    assert out["status"] == "blocked"
    event = _events(audit_events, "tool_gate")[-1]
    assert event["kind"] == "heavy_blocked" and event["tool"] == "collect"
    assert event["stage"] == "generate" and event["failures"] == 2 and event["status"] == "blocked"


def test_tool_gate_heavy_blocked_allows_early_search_failure(audit_events):
    """搜索段首次失败是便宜的，放行重跑——不留事件。"""
    ctx = CoachCtx({"tech": "redis", "coach_heavy_failures": {"collect": {"count": 1, "stage": "search"}}})
    assert route_mod._heavy_blocked(ctx, "collect") is None
    assert not _events(audit_events, "tool_gate")


def _roadmap():
    stages, _ = rm.normalize_stages([
        {"name": "环境搭建", "goal": "g", "est_hours": 4,
         "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello"}]},
    ])
    return rm.build_roadmap("t", "goal", 4, stages)


def test_milestone_batch_gate_is_recorded(monkeypatch, audit_events):
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    ctx = CoachCtx({"tech": "t", "roadmap": _roadmap(), "coach_milestone_pending": "s1-m1"})
    out = route_mod.run_coach_tool("update_roadmap", {"milestone_id": "s1-m2", "done": True}, ctx)

    assert out["status"] == "rejected"  # 闸 1：一轮只能勾一个
    event = _events(audit_events, "tool_gate")[-1]
    assert event["kind"] == "milestone_batch" and event["pending"] == "s1-m1"
    assert event["status"] == "rejected"


def test_milestone_verify_records_verdict(monkeypatch, audit_events):
    """验收结论本身要留痕：只记被拒那次会让"验收几乎从没触发"这类问题查不出来。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda desc, transcript: {"verified": True, "missing": [], "reason": "覆盖到位"})
    ctx = CoachCtx({"tech": "t", "roadmap": _roadmap(),
                    "coach_messages": [{"role": "user", "content": "继续"}],
                    "conversation": [{"role": "user", "type": "chat", "content": "都弄好了"}]})
    out = route_mod.run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)

    assert out["status"] == "ok"
    event = _events(audit_events, "milestone_verify")[-1]
    assert event["milestone"] == "s1-m1" and event["verified"] is True
    assert event["missing_count"] == 0 and event["rejects"] == 0


# ============================================================
# 1-3 人工中断：interrupt / resume（宿主侧记，天然只经过一次）
# ============================================================

def test_resume_event_records_answer_kind(monkeypatch, audit_events):
    monkeypatch.setattr(runner_mod, "_start", lambda tid, payload: None)
    runner_mod.resume_run("learn-x", "1,3")

    event = _events(audit_events, "resume")[-1]
    assert event["answer_kind"] == "numbered" and event["answer_chars"] == 3
    assert event["thread_id"] == "learn-x"  # 请求线程没有图上下文，必须显式声明


def test_resume_not_recorded_when_run_rejected(monkeypatch, audit_events):
    monkeypatch.setattr(runner_mod, "_start", lambda tid, payload: "该会话已有任务在运行")
    assert runner_mod.resume_run("learn-x", "all") == "该会话已有任务在运行"
    assert not _events(audit_events, "resume")


def test_interrupt_event_from_real_worker(monkeypatch, tmp_path, audit_events):
    """真实 Web worker：图跑到 interrupt → 事件归属发起它的会话（worker 是普通线程，
    不显式声明 audit_context 的话这里会退化成 "local"）。"""
    from src.graph import build_graph

    monkeypatch.setattr(config, "GRAPH_DB_DIR", tmp_path / ".graph")
    monkeypatch.setattr(config, "GRAPH_DB_PATH", tmp_path / ".graph" / "checkpoints.sqlite")
    monkeypatch.setattr(graph_mod, "chat_with_tools",
                        lambda s, m, t, **kw: {"content": "你对这门技术的熟悉程度是几分？",
                                               "tool_calls": []})

    tid = "learn-20260101-000001"
    assert runner_mod.start_run(tid, {"command": "route", "tech": "Redis"}) is None
    job = runner_mod.get_job(tid)
    deadline = time.monotonic() + 10
    while job.active and time.monotonic() < deadline:
        time.sleep(0.02)

    event = _events(audit_events, "interrupt")[-1]
    assert event["kind"] == "coach_question"
    assert event["thread_id"] == tid
    assert event["payload_chars"] > 0
    assert build_graph is not None  # 保持导入含义清晰：本用例走的是真实图


# ============================================================
# 1-4 上下文与记忆：ctx_compact / sweep_fire / sweep_drain
# ============================================================

def _dense_history(user_turns: int, per_turn: int = 8):
    msgs = []
    for i in range(user_turns):
        msgs.append({"role": "user", "content": f"第{i}轮问题"})
        msgs.extend({"role": "assistant", "content": f"第{i}轮回答{j}"} for j in range(per_turn))
    return msgs


def test_ctx_compact_records_cut(monkeypatch, audit_events):
    monkeypatch.setattr(graph_mod, "consolidate_memory",
                        lambda existing, msgs, tech: {"facts": ["f"], "open_items": [], "summary": "s"})
    msgs = _dense_history(8)
    assert len(msgs) > config.COACH_COMPRESS_AT

    out = graph_mod.coach_trim({"coach_messages": msgs})

    event = _events(audit_events, "ctx_compact")[-1]
    assert isinstance(event["cut"], int) and event["cut"] > 0
    assert event["before"] == len(msgs)
    assert event["after"] == len(out["coach_messages"])
    assert event["dropped"] == len(msgs) - len(out["coach_messages"])


def test_ctx_compact_warns_when_cut_point_missing(audit_events):
    """消息数超阈值却找不到 user 切点 = 已知的"触发却不裁"空转，用 WARNING 让它显形。"""
    msgs = _dense_history(3, per_turn=20)  # 63 条消息但只有 3 轮
    assert len(msgs) > config.COACH_COMPRESS_AT

    out = graph_mod.coach_trim({"coach_messages": msgs})

    event = _events(audit_events, "ctx_compact")[-1]
    assert event["cut"] == "none" and event["level"] == "WARNING"
    assert event["dropped"] == 0 and len(out["coach_messages"]) == len(msgs)


def test_sweep_fire_and_drain_sync_path(monkeypatch, audit_events):
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", False)
    monkeypatch.setattr(graph_mod, "run_memory_sweep",
                        lambda tech, buffer, progress=None: {"action": "persisted", "count": 2,
                                                            "message": "已沉淀"})
    buffer = [{"role": "user", "content": "x" * 3000}]
    graph_mod.coach_memory_write({"mode": "coaching", "tech": "Redis", "memory_sweep_buffer": buffer})

    fire = _events(audit_events, "sweep_fire")[-1]
    drain = _events(audit_events, "sweep_drain")[-1]
    assert fire["items"] == 1 and fire["mode"] == "sync"
    assert fire["chars"] == 3000  # 与触发判定共用 _buffer_chars：日志里的批次大小要能和"为什么触发"对上
    assert drain["result"] == "persisted" and drain["count"] == 2 and drain["mode"] == "sync"


def test_sweep_fire_async_records_chars(monkeypatch, audit_events):
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", True)
    monkeypatch.setattr(graph_mod, "_start_sweep_thread", lambda tech, buffer, tid: None)
    buffer = [{"role": "assistant", "content": "讲" * 3000}, {"role": "user", "content": "问" * 200}]
    graph_mod.coach_memory_write({"mode": "coaching", "tech": "Redis", "memory_sweep_buffer": buffer})

    fire = _events(audit_events, "sweep_fire")[-1]
    assert fire["mode"] == "async" and fire["items"] == 2 and fire["chars"] == 3200


def test_note_recall_event_records_sizes_without_content(monkeypatch, audit_events):
    """召回现场必须留痕：批次/查询长度、召回了哪几篇（路径 + 分数）；正文一律不进日志。

    没有这条事件，「召回窄了」与「提取质量差」在日志里长得一样——都是"该抑制的没抑制住"。
    """
    monkeypatch.setattr(config, "NOTE_QUERY_CHARS", 500)
    monkeypatch.setattr(config, "NOTE_CONTENT_CHARS", 1800)
    monkeypatch.setattr(note_mod, "recall_existing_notes",
                        lambda tech, query, top_k, **k: [
                            {"path": "redis/缓存.md", "similarity": 0.8123},
                            {"path": "redis/持久化.md", "similarity": None},  # RAG 不可用回退
                        ])
    monkeypatch.setattr(note_mod, "generate_text", lambda *a, **k: "[]")
    monkeypatch.setattr(note_mod, "get_existing_notes", lambda tech: [])
    secret = "学习内容" * 400  # 1600 字：超查询窗口、未超内容上限

    note_mod.note_pipeline("redis", secret)

    event = _events(audit_events, "note_recall")[-1]
    assert event["tech"] == "redis"
    assert event["batch_chars"] == 1600 and event["content_chars"] == 1600
    assert event["content_truncated"] is False
    assert event["query_count"] == 1 and event["query_chars"] == 500
    assert event["query_truncated"] is True
    assert event["kb_notes"] == 0
    assert event["recalled_count"] == 2
    assert event["recalled"] == [{"path": "redis/缓存.md", "sim": 0.812},
                                 {"path": "redis/持久化.md", "sim": None}]
    assert "学习内容" not in json.dumps(event, ensure_ascii=False)


def test_note_recall_marks_content_truncation(monkeypatch, audit_events):
    """批次超过 NOTE_CONTENT_CHARS 时报出来——`[:12000]` 是否真触发过只能靠这个字段回答。"""
    monkeypatch.setattr(config, "NOTE_CONTENT_CHARS", 100)
    monkeypatch.setattr(note_mod, "recall_existing_notes", lambda tech, query, top_k, **k: [])
    monkeypatch.setattr(note_mod, "generate_text", lambda *a, **k: "[]")
    monkeypatch.setattr(note_mod, "get_existing_notes", lambda tech: [])

    note_mod.note_pipeline("redis", "x" * 250)

    event = _events(audit_events, "note_recall")[-1]
    assert event["batch_chars"] == 250 and event["content_chars"] == 100
    assert event["content_truncated"] is True
    assert event["recalled_count"] == 0 and event["recalled"] == []


def test_note_recall_event_records_query_count_and_kb_size(monkeypatch, audit_events):
    """按段召回要留下读数：发了几次查询、库里本来有几篇。

    召回为空时，"库里本来就没有笔记"和"有笔记但都被相似度下限滤掉了"是两种状态
    （后者是过抑制的现实来源），只有 kb_notes 能把它们分开。
    """
    monkeypatch.setattr(config, "NOTE_QUERY_CHARS", 100)
    monkeypatch.setattr(note_mod, "recall_existing_notes",
                        lambda tech, query, top_k, **k: [])
    monkeypatch.setattr(note_mod, "generate_text", lambda *a, **k: "[]")
    monkeypatch.setattr(note_mod, "get_existing_notes",
                        lambda tech: [{"path": f"redis/{i}.md", "topic": f"t{i}"} for i in range(9)])

    note_mod.note_pipeline("redis", "整批正文", query_segments=["段一" * 40, "段二" * 40])

    event = _events(audit_events, "note_recall")[-1]
    assert event["query_count"] == 2  # 两段各一条查询（每段 80 字，未触发窗口切分）
    assert event["query_chars"] == 160
    assert event["kb_notes"] == 9 and event["recalled_count"] == 0


def test_sweep_drain_marks_stale_on_timeout(monkeypatch, audit_events):
    """超时兜底（线程死 / 进程重启）要能和正常排水区分开——这是并行沉淀事故的直接证据。"""
    graph_mod._sweep_results.pop("learn-x", None)
    state = {"mode": "coaching", "tech": "Redis",
             "memory_sweep_inflight": {"tech": "Redis", "buffer": [{"role": "user", "content": "x"}],
                                       "fired_at": "2000-01-01T00:00:00"}}
    out = graph_mod.coach_memory_write(state)

    event = _events(audit_events, "sweep_drain")[-1]
    assert event["result"] == "stale" and event["items"] == 1
    assert event["elapsed_s"] > 0
    assert out["memory_sweep_inflight"] is None and len(out["memory_sweep_buffer"]) == 1


# ============================================================
# 1-6 分步进度：pipeline_step
# ============================================================

def test_step_recorder_emits_and_forwards(audit_events):
    seen: list[str] = []
    emit = _step_recorder("collect", seen.append)
    emit("🔍 搜索: a")
    emit("🛰️ 并发抓取 5 个页面...")

    steps = _events(audit_events, "pipeline_step")
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0]["pipeline"] == "collect" and "搜索" in steps[0]["label"]
    assert all(s["elapsed_s"] >= 0 for s in steps)
    assert seen == ["🔍 搜索: a", "🛰️ 并发抓取 5 个页面..."]


def test_step_recorder_emits_even_without_progress_callback(audit_events):
    """CLI 下 progress 为 None，但事件必须照记——埋点不能依附于"有没有人在看终端"。"""
    _step_recorder("collect", None)("🛡️ 预筛低质量链接...")

    assert len(_events(audit_events, "pipeline_step")) == 1


# ============================================================
# 1-5 副作用：note_persist / artifact_write / index_reconcile
# ============================================================

def test_note_persist_events(monkeypatch, audit_events):
    monkeypatch.setattr(note_mod, "persist_note",
                        lambda tech, topic, content, tags, **kw: {
                            "action": "merged" if kw else "new",
                            "path": f"redis/{topic}.md", "topic": topic, "index_ok": True})
    monkeypatch.setattr(note_mod, "merge_notes",
                        lambda old, new, topic: {"content": "merged", "report": "有矛盾"})
    out = note_mod.persist_points(
        "redis",
        [{"topic": "持久化", "content": "c", "tags": ["redis"]}],
        [{"topic": "缓存", "old_topic": "缓存问题", "old_content": "o", "content": "n", "tags": [],
          "old_path": "redis/缓存问题.md"}],
        {0})

    events = _events(audit_events, "note_persist")
    assert [e["kind"] for e in events] == ["new", "merged"]
    assert events[0]["path"] == "redis/持久化.md" and events[0]["tech"] == "redis"
    assert events[1]["conflict"] is True
    assert out["new_count"] == 1 and out["merged_count"] == 1


@pytest.mark.parametrize("path,kind", [("materials/a.md", "materials"),
                                       ("reports/b.md", "reports"),
                                       ("other/c.md", "other")])
def test_artifact_write_event(monkeypatch, tmp_path, audit_events, path, kind):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    save_file_tool(path, "内容")

    event = _events(audit_events, "artifact_write")[-1]
    assert event["kind"] == kind and event["path"] == path
    assert event["bytes"] == len("内容".encode())


def test_index_reconcile_gate_tripped_is_warning(monkeypatch, audit_events):
    """安全闸（防全库被清空）触发必须是 WARNING——全项目最该响铃的地方。"""
    monkeypatch.setattr(vector_mod, "_reconcile_index",
                        lambda backfill=True, max_backfill=0: {
                            "orphans": 0, "backfilled": 2, "warn": "疑似扫描异常"})
    vector_mod.reconcile_orphans(force=True)

    event = _events(audit_events, "index_reconcile")[-1]
    assert event["gate_tripped"] is True and event["level"] == "WARNING"
    assert event["backfilled"] == 2 and "疑似扫描异常" in event["warn"]


def test_index_reconcile_silent_when_nothing_happened(monkeypatch, audit_events):
    monkeypatch.setattr(vector_mod, "_reconcile_index",
                        lambda backfill=True, max_backfill=0: {"orphans": 0, "backfilled": 0, "warn": ""})
    vector_mod.reconcile_orphans(force=True)
    assert not _events(audit_events, "index_reconcile")


# ============================================================
# 1-7 错误路径：error
# ============================================================

def test_consolidate_failure_is_recorded(monkeypatch, audit_events):
    monkeypatch.setattr(route_mod, "generate_text",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("LLM 挂了")))
    out = route_mod.consolidate_memory({"facts": ["f"], "open_items": [], "summary": "s"},
                                       [{"role": "user", "content": "hi"}], "redis")

    event = _events(audit_events, "error")[-1]
    assert event["stage"] == "coach.consolidate" and event["kind"] == "RuntimeError"
    assert out["facts"] == ["f"]  # 三舱原样保留


def test_unparsable_memory_output_logs_length_not_content(monkeypatch, audit_events):
    """记忆增量的原文是学习内容，解析失败时只能记长度。"""
    secret = "用户的私有事实" * 50
    monkeypatch.setattr(route_mod, "generate_text", lambda *a, **kw: secret)
    route_mod.consolidate_memory({"facts": [], "open_items": [], "summary": ""},
                                 [{"role": "user", "content": "hi"}], "redis")

    event = _events(audit_events, "error")[-1]
    assert event["kind"] == "unparsable_output"
    assert str(len(secret)) in event["message"]
    assert secret[:8] not in json.dumps(event, ensure_ascii=False)


def test_audit_level_constant_is_int():
    """防止 level 传成字符串把 getLevelName 弄反。"""
    audit("probe_event", level=logging.WARNING)
