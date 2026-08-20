"""coach 循环骨架单测（Step 2 + 部分 Step 3 路由）：survey 全流程 / 退出意图 / 工具护栏。

零网络：monkeypatch graph 模块的 chat_with_tools 为脚本化响应，驱动 interrupt/resume。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_coach_loop.py -v
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import src.graph as graph_mod
import src.pipelines.route as route_mod
from src.adapters import learner as le
from src.config import config
from src.graph import build_graph


def _run(graph, gconfig, payload, replies, *, max_iters=60):
    """驱动图执行；interrupt 时按序取 replies 恢复。返回 (final_state, interruptions)。"""
    it = iter(replies)
    interruptions = []
    stream_input = payload
    for _ in range(max_iters):
        stream = graph.stream_events(stream_input, gconfig, version="v3")
        if not stream.interrupted:
            return stream.output, interruptions
        for intr in stream.interrupts:
            interruptions.append(intr.value)
        try:
            reply = next(it)
        except StopIteration:
            reply = ""
        stream_input = Command(resume=reply)
    raise AssertionError("max_iters 内未收敛")


def _scripted_survey_chat(system_prompt, messages, tools):
    """按 system_prompt 中的字段标签返回固定提问（Step 2 无工具调用）。"""
    if "路线规划助手" in system_prompt:
        return {"content": "路线已初步生成：\n- 阶段1 环境搭建（4h）\n- 阶段2 核心概念（8h）\n请确认或提出修改。",
                "tool_calls": []}
    if "正在收集字段：自评熟悉度" in system_prompt:
        return {"content": "在开始前，先问你几个问题。你对这门技术的熟悉程度是几分（0-10）？", "tool_calls": []}
    if "正在收集字段：相关技术" in system_prompt:
        return {"content": "你熟悉哪些相关技术呢？", "tool_calls": []}
    if "正在收集字段：学习目标" in system_prompt:
        return {"content": "你这次是想快速上手跑通最小项目，还是深入原理？", "tool_calls": []}
    if "正在收集字段：时间预算" in system_prompt:
        return {"content": "每天大概能投入多少小时？", "tool_calls": []}
    if "动态诊断题" in system_prompt:
        return {"content": "来一道小诊断题：什么是依赖注入？", "tool_calls": []}
    return {"content": "（默认回复）", "tool_calls": []}


# ---------- survey 全流程 ----------

def test_survey_flow_to_planning(monkeypatch):
    """完整问卷（4 固定字段 + 2 诊断题）→ planning（stub 对话）→ 退出。"""
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_survey_chat)
    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-survey-1"}}
    replies = ["5", "Java Maven", "跑通最小项目", "2小时", "答1", "答2", "结束"]
    final, interrupts = _run(graph, gconfig, {"command": "route", "tech": "Spring Boot"}, replies)

    assert final["mode"] == "planning"
    assert final["tech"] == "Spring Boot"
    answers = final["survey_answers"]
    assert answers["self_level"] == 5
    assert answers["related"] == "Java Maven"
    assert answers["goal"] == "min_project"
    assert answers["time_budget"] == 2.0
    assert answers["diagnostics"] == ["答1", "答2"]
    assert final["learner_profile"]["bucket"] == "intermediate"  # 自评 5
    # interrupt 序列：4 固定字段 + 2 诊断 + 1 planning 呈现
    assert len(interrupts) == 7


def test_exit_mid_survey(monkeypatch):
    """问卷中途说「结束」→ 直接 END，不进入 planning。"""
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_survey_chat)
    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-survey-exit"}}
    final, interrupts = _run(graph, gconfig, {"command": "route", "tech": "X"}, ["结束"])
    assert final.get("mode") == "survey"
    assert not final.get("survey_answers")  # 未收集任何答案
    assert len(interrupts) == 1  # 只有第一个问题


def test_survey_parse_error_reasks(monkeypatch):
    """自评回答非数字 → 内部校验提示 → 模型重问（同字段，不推进）。"""
    state_log = {}

    def scripted(system_prompt, messages, tools):
        # 记录最近一次含【问卷校验】的消息
        for m in reversed(messages):
            if "问卷校验" in (m.get("content") or ""):
                state_log["saw_note"] = m["content"]
                break
        return _scripted_survey_chat(system_prompt, messages, tools)

    monkeypatch.setattr(graph_mod, "chat_with_tools", scripted)
    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-survey-reask"}}
    # 第一次答「abc」（非法）→ 重问 → 第二次答「5」
    final, _ = _run(graph, gconfig, {"command": "route", "tech": "X"},
                    ["abc", "5", "结束"])
    assert final["survey_answers"]["self_level"] == 5
    assert "问卷校验" in state_log.get("saw_note", "")
    assert state_log["saw_note"]


# ---------- 工具护栏（直接单测 _coach_guard） ----------

def _state_with_tool_call(args=None, count=1, sigs=None):
    # 用 OpenAI 格式构造 tool_calls（coach_llm 写入 coach_messages 的形态）
    return {
        "coach_messages": [{"role": "assistant", "content": None,
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "x",
                                                         "arguments": json.dumps(args or {}, ensure_ascii=False)}}]}],
        "coach_turn_tool_count": count,
        "last_tool_signatures": sigs or [],
    }


def test_guard_budget_forces_user_dialog():
    msg = graph_mod._coach_guard(_state_with_tool_call(count=8))
    assert msg
    assert "上限" in msg


def test_guard_repeat_detection():
    sig = ["x:{}"]
    msg = graph_mod._coach_guard(_state_with_tool_call(sigs=[sig, sig]))
    assert msg
    assert "重复" in msg


def test_guard_no_violation():
    assert graph_mod._coach_guard(_state_with_tool_call({"a": 1})) is None


def test_guard_single_repeat_not_triggered():
    sig = ["x:{}"]
    assert graph_mod._coach_guard(_state_with_tool_call(sigs=[sig])) is None


# ---------- coach_llm 节点行为 ----------

def test_empty_model_output_appends_system_note(monkeypatch):
    monkeypatch.setattr(graph_mod, "chat_with_tools",
                        lambda s, m, t: {"content": None, "tool_calls": []})
    out = graph_mod.coach_llm({"mode": "survey", "coach_messages": [], "tech": "X"})
    last = out["coach_messages"][-1]
    assert last["role"] == "system"
    assert "输出为空" in last["content"]


def test_coach_llm_tool_calls_passthrough(monkeypatch):
    monkeypatch.setattr(graph_mod, "chat_with_tools",
                        lambda s, m, t: {"content": None,
                                         "tool_calls": [{"id": "c1", "name": "get_roadmap", "arguments": {}}]})
    out = graph_mod.coach_llm({"mode": "planning", "coach_messages": [], "tech": "X"})
    last = out["coach_messages"][-1]
    assert last["role"] == "assistant"
    assert last["tool_calls"][0]["function"]["name"] == "get_roadmap"


# ---------- Step 3：planning 端到端（问卷 → 路线生成 → 确认 → coaching） ----------

def _scripted_planning_chat(system_prompt, messages, tools):
    """问卷固定提问 + planning 阶段脚本化工具调用（generate → 呈现 → confirm）。"""
    if "水平探测助手" in system_prompt:
        return _scripted_survey_chat(system_prompt, messages, tools)
    if "路线规划助手" in system_prompt:
        last = messages[-1] if messages else None
        generated = any(m.get("role") == "tool" and "路线已保存" in (m.get("content") or "")
                        for m in messages)
        if generated and last and last.get("role") == "user":
            # 路线已生成且用户回复 → 视为确认 → 调 confirm_roadmap
            return {"content": None,
                    "tool_calls": [{"id": "c_cf", "name": "confirm_roadmap", "arguments": {}}]}
        if last and last.get("role") == "tool":
            # generate_roadmap 刚执行完 → 把路线呈现给用户确认
            return {"content": "路线已生成，请确认：\n- 阶段1 环境搭建（4h）\n- 阶段2 核心概念（8h）",
                    "tool_calls": []}
        # 首次进入规划（问卷刚收尾，最后一条是用户回答）→ 生成路线
        return {"content": None,
                "tool_calls": [{"id": "c_rm", "name": "generate_roadmap", "arguments": {
                    "goal": "能跑通最小项目", "total_hours": 12, "revision": "",
                    "stages": [
                        {"name": "环境搭建", "goal": "跑通 hello", "est_hours": 4,
                         "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello"}]},
                        {"name": "核心概念", "goal": "掌握核心", "est_hours": 8,
                         "milestones": [{"desc": "理解 A"}]}]}}]}
    if "执行陪练" in system_prompt:
        return {"content": "路线已确认，开始执行第一步吧！", "tool_calls": []}
    return {"content": "（默认回复）", "tool_calls": []}


def test_planning_generates_roadmap_and_confirms(monkeypatch, tmp_path):
    """完整流程：问卷 → planning 生成路线（工具）→ 用户确认 → confirm → coaching。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_planning_chat)
    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-plan-1"}}
    replies = ["5", "Java Maven", "跑通最小项目", "2小时", "答1", "答2", "可以", "结束"]
    final, interrupts = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    assert final["mode"] == "coaching"
    roadmap = final["roadmap"]
    assert roadmap["tech"] == "X"
    assert roadmap["current_stage"] == "s1"
    assert len(roadmap["stages"]) == 2
    assert roadmap["stages"][0]["milestones"][0]["id"] == "s1-m1"
    # 路线已落盘
    loaded = le.load_roadmap("X")
    assert loaded and loaded["current_stage"] == "s1"
    # interrupts：6 问卷 + 1 路线呈现 + 1 coaching 开场
    assert len(interrupts) == 8
    assert "环境搭建" in interrupts[6]["message"]
    assert interrupts[6]["mode"] == "planning"


# ---------- Step 4：coaching 工具端到端 + 上下文压缩 ----------

def _scripted_coaching_chat(system_prompt, messages, tools):
    """问卷 + planning（生成/确认路线）+ coaching（collect → 勾选里程碑 → 结束）。"""
    if "水平探测助手" in system_prompt:
        return _scripted_survey_chat(system_prompt, messages, tools)
    if "路线规划助手" in system_prompt:
        last = messages[-1] if messages else None
        generated = any(m.get("role") == "tool" and "路线已保存" in (m.get("content") or "")
                        for m in messages)
        if generated and last and last.get("role") == "user":
            return {"content": None,
                    "tool_calls": [{"id": "c_cf", "name": "confirm_roadmap", "arguments": {}}]}
        if last and last.get("role") == "tool":
            return {"content": "路线已生成，请确认", "tool_calls": []}
        return {"content": None,
                "tool_calls": [{"id": "c_rm", "name": "generate_roadmap", "arguments": {
                    "goal": "能跑通最小项目", "total_hours": 12, "revision": "",
                    "stages": [
                        {"name": "环境搭建", "goal": "跑通 hello", "est_hours": 4,
                         "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello"}]},
                        {"name": "核心概念", "goal": "掌握核心", "est_hours": 8,
                         "milestones": [{"desc": "理解 A"}]}]}}]}
    if "执行陪练" in system_prompt:
        last = messages[-1] if messages else None
        if last and last.get("role") == "tool":
            content = last.get("content") or ""
            if "materials_path" in content:
                return {"content": "资料收集完成，可以开始第一步了。", "tool_calls": []}
            if "milestone_id" in content:
                return {"content": "里程碑已勾选，进度更新。", "tool_calls": []}
        if last and last.get("role") == "user":
            return {"content": None,
                    "tool_calls": [{"id": "c_um", "name": "update_roadmap",
                                    "arguments": {"milestone_id": "s1-m1", "done": True}}]}
        return {"content": None,
                "tool_calls": [{"id": "c_col", "name": "collect", "arguments": {"tech": "X"}}]}
    return {"content": "（默认回复）", "tool_calls": []}


def test_coaching_uses_collect_and_update_roadmap(monkeypatch, tmp_path):
    """coaching 模式：agent 自主调 collect → 呈现 → 用户反馈 → 勾选里程碑。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(route_mod, "collect_pipeline",
                        lambda tech, focus=None, progress=None: {
                            "urls": ["u1"], "report": "## 官方文档\n...", "materials_path": "materials/x.md"})
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_coaching_chat)
    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-coach-1"}}
    replies = ["5", "Java Maven", "跑通最小项目", "2小时", "答1", "答2", "可以", "好的", "结束"]
    final, interrupts = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    assert final["mode"] == "coaching"
    assert final["roadmap"]["stages"][0]["milestones"][0]["done"] is True  # s1-m1 已勾选
    # interrupts：6 问卷 + 1 planning 呈现 + 1 collect 结果 + 1 里程碑更新
    assert len(interrupts) == 9
    assert interrupts[7]["mode"] == "coaching"
    assert "资料收集完成" in interrupts[7]["message"]
    # collect 产出的材料报告带 doc chip（查看完整文档）
    doc_recs = [m for m in (final.get("conversation") or [])
                if m.get("role") == "assistant" and m.get("doc_type") == "collect"]
    assert doc_recs, "collect 结果应带 doc_type=collect 的对话记录"
    assert doc_recs[0]["doc"].startswith("materials/")


def test_coach_trim_compresses_over_threshold(monkeypatch):
    """超阈值 → LLM 摘要写入 coach_summary，消息裁剪到最近 N 轮。"""
    msgs = [{"role": "user", "content": f"消息{i}"} for i in range(config.COACH_COMPRESS_AT + 5)]
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u: "【压缩摘要】")
    out = graph_mod.coach_trim({"mode": "coaching", "coach_messages": msgs,
                                "coach_summary": "", "tech": "X", "survey_answers": {}})
    assert out["coach_summary"] == "【压缩摘要】"
    assert len(out["coach_messages"]) <= config.COACH_HISTORY_KEEP * 2


def test_coach_trim_no_compress_under_threshold(monkeypatch):
    """低于阈值不压缩、不调摘要 LLM。"""
    msgs = [{"role": "user", "content": "x"} for _ in range(5)]
    monkeypatch.setattr(route_mod, "generate_text",
                        lambda s, u: (_ for _ in ()).throw(AssertionError("不应触发摘要")))
    out = graph_mod.coach_trim({"mode": "coaching", "coach_messages": msgs,
                                "coach_summary": "", "tech": "X", "survey_answers": {}})
    assert out["coach_messages"] == msgs
    assert not out.get("coach_summary")


def test_coach_trim_initializes_survey():
    out = graph_mod.coach_trim({"mode": None, "coach_messages": [], "survey_answers": None})
    assert out["mode"] == "survey"
    assert out["survey_field"] == "self_level"


# ---------- Step 4：note → 合并确认 → note_commit 端到端 ----------

def _scripted_note_chat(system_prompt, messages, tools):
    """coaching 里 agent 调 note（有相似候选）→ 呈现 → 用户决定 → note_commit。"""
    if "水平探测助手" in system_prompt:
        return _scripted_survey_chat(system_prompt, messages, tools)
    if "路线规划助手" in system_prompt:
        last = messages[-1] if messages else None
        generated = any(m.get("role") == "tool" and "路线已保存" in (m.get("content") or "")
                        for m in messages)
        if generated and last and last.get("role") == "user":
            return {"content": None,
                    "tool_calls": [{"id": "c_cf", "name": "confirm_roadmap", "arguments": {}}]}
        if last and last.get("role") == "tool":
            return {"content": "路线已生成，请确认", "tool_calls": []}
        return {"content": None,
                "tool_calls": [{"id": "c_rm", "name": "generate_roadmap", "arguments": {
                    "goal": "能跑通最小项目", "total_hours": 12, "revision": "",
                    "stages": [{"name": "环境搭建", "goal": "跑通 hello", "est_hours": 4,
                                "milestones": [{"desc": "安装完成"}]}]}}]}
    if "执行陪练" in system_prompt:
        last = messages[-1] if messages else None
        if last and last.get("role") == "tool":
            content = last.get("content") or ""
            if "needs_decision" in content:
                try:
                    msg = json.loads(content).get("message", content)
                except Exception:  # noqa: BLE001
                    msg = content
                return {"content": msg, "tool_calls": []}
            if "new_count" in content or "merged_count" in content:
                return {"content": "笔记已沉淀完成。", "tool_calls": []}
        if last and last.get("role") == "user":
            return {"content": None,
                    "tool_calls": [{"id": "c_nc", "name": "note_commit",
                                    "arguments": {"decision": last.get("content")}}]}
        return {"content": None,
                "tool_calls": [{"id": "c_n", "name": "note",
                                "arguments": {"tech": "X", "content": "刚学到的内容"}}]}
    return {"content": "（默认回复）", "tool_calls": []}


def test_coaching_note_merge_confirm_end_to_end(monkeypatch, tmp_path):
    """note 工具产生相似候选 → 呈现用户 → 决定 → note_commit 提交，pending 清空。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda tech, log, materials_path=None, progress=None: {
                            "new_points": [],
                            "merge_candidates": [{"old_path": "a.md", "old_topic": "A",
                                                  "old_content": "o", "similarity": 0.9,
                                                  "reason": "同一主题", "topic": "T",
                                                  "tags": [], "content": "n"}],
                            "empty_reason": None, "summary": "s", "raw": "r",
                            "new_count": 0, "merged_count": 1})
    captured = {}
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda tech, np, mc, mi: captured.update(mi=mi) or
                            {"results": [{"topic": "A", "path": "a.md", "action": "merged"}],
                             "new_count": 0, "merged_count": 1})
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_note_chat)
    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-note-1"}}
    replies = ["5", "Java Maven", "跑通最小项目", "2小时", "答1", "答2", "可以", "全部合并", "结束"]
    final, interrupts = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    assert final["mode"] == "coaching"
    assert final["coach_note_pending"] is None  # 提交后清空
    assert captured["mi"] == {0}  # 「全部合并」→ 合并索引 0
    # interrupts：6 问卷 + 1 planning 呈现 + 1 相似候选呈现 + 1 沉淀完成
    assert len(interrupts) == 9
    assert "发现 1 条" in interrupts[7]["message"]
