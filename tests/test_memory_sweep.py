"""记忆系统 Step 1 单测：确定性写触发（run_memory_sweep + coach_memory_write + 图级 e2e）。

零网络：monkeypatch note_pipeline / persist_points / run_memory_sweep。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_sweep.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import src.graph as graph_mod
import src.pipelines.route as route_mod
from src.config import config
from src.graph import build_graph


# ============ run_memory_sweep（route.py 纯函数） ============

_EMPTY = {"new_points": [], "merge_candidates": [], "empty_reason": "无新内容"}
_NEW = {"new_points": [{"topic": "依赖注入", "tags": ["X"], "content": "C1"}],
        "merge_candidates": [], "empty_reason": None}
_CAND = {"new_points": [{"topic": "依赖注入", "tags": ["X"], "content": "C1"}],
         "merge_candidates": [{"old_path": "x.md", "old_topic": "旧笔记", "topic": "依赖注入",
                               "similarity": 0.8, "reason": "相似", "content": "C1"}],
         "empty_reason": None}

_BUF = [{"role": "assistant", "content": "讲解一"}, {"role": "user", "content": "懂了"},
        {"role": "assistant", "content": "讲解二"}, {"role": "user", "content": "继续"}]


def test_sweep_empty_buffer_no_pipeline(monkeypatch):
    called = {}
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda *a, **k: called.setdefault("np", True))
    out = route_mod.run_memory_sweep("X", [])
    assert out["action"] == "skip"
    assert not called


def test_sweep_no_new_content_skips(monkeypatch):
    monkeypatch.setattr(route_mod, "note_pipeline", lambda *a, **k: dict(_EMPTY))
    out = route_mod.run_memory_sweep("X", _BUF)
    assert out["action"] == "skip"
    assert out["count"] == 0


def test_sweep_all_invalid_entries_skips(monkeypatch):
    """LLM 输出条目全无效（topic/正文空被过滤）→ 两桶都空，等价无新内容，按 skip 处理。"""
    both_empty = {"new_points": [], "merge_candidates": [], "empty_reason": None}
    monkeypatch.setattr(route_mod, "note_pipeline", lambda *a, **k: dict(both_empty))
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应落库")))
    out = route_mod.run_memory_sweep("X", _BUF)
    assert out["action"] == "skip"
    assert out["count"] == 0


def test_sweep_auto_persists_new_points(monkeypatch):
    seen = {}
    monkeypatch.setattr(route_mod, "note_pipeline", lambda *a, **k: dict(_NEW))

    def fake_persist(tech, new_points, candidates, indices):
        seen["tech"] = tech
        seen["new_points"] = new_points
        return {"new_count": len(new_points), "results": []}

    monkeypatch.setattr(route_mod, "persist_points", fake_persist)
    out = route_mod.run_memory_sweep("X", _BUF)
    assert out["action"] == "persisted"
    assert out["count"] == 1
    assert seen["tech"] == "X"
    assert seen["new_points"] == _NEW["new_points"]
    assert "已自动沉淀 1 个新知识点" in out["message"]


def test_sweep_candidates_pend_until_commit(monkeypatch):
    seen = {}
    monkeypatch.setattr(route_mod, "note_pipeline", lambda *a, **k: dict(_CAND))
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda *a: seen.setdefault("pp", True))
    out = route_mod.run_memory_sweep("X", _BUF)
    assert out["action"] == "pending"
    assert "pp" not in seen  # 候选存在时先不落库，等用户决定后 note_commit 一次落库
    assert out["pending"]["_tech"] == "X"
    assert out["pending"]["_auto"] is True
    assert "候选" in out["message"]


# ============ coach_memory_write（graph.py 节点） ============

def _sweep_state(**over):
    state = {
        "mode": "coaching",
        "memory_sweep_buffer": list(_BUF),
        "coach_note_pending": None,
        "coach_messages": [{"role": "user", "content": "继续"}],
        "tech": "X",
    }
    state.update(over)
    return state


def _no_sweep(*a, **k):
    raise AssertionError("不应触发沉淀")


def test_write_skips_non_coaching(monkeypatch):
    monkeypatch.setattr(graph_mod, "run_memory_sweep", _no_sweep)
    assert graph_mod.coach_memory_write(_sweep_state(mode="planning")) == {}


def test_write_skips_empty_buffer(monkeypatch):
    monkeypatch.setattr(graph_mod, "run_memory_sweep", _no_sweep)
    assert graph_mod.coach_memory_write(_sweep_state(memory_sweep_buffer=[])) == {}


def test_write_skips_when_pending(monkeypatch):
    monkeypatch.setattr(graph_mod, "run_memory_sweep", _no_sweep)
    assert graph_mod.coach_memory_write(_sweep_state(coach_note_pending={"x": 1})) == {}


def test_write_below_threshold_preserves_buffer(monkeypatch):
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 10)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 10000)
    monkeypatch.setattr(graph_mod, "run_memory_sweep", _no_sweep)
    assert graph_mod.coach_memory_write(_sweep_state()) == {}


def test_write_triggers_on_turns_and_clears(monkeypatch):
    # pinned 到 ASYNC=false：v1 同步路径（逃生舱），触发即应用
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", False)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 10000)
    seen = {}

    def fake_sweep(tech, buffer, progress=None):
        seen["tech"] = tech
        seen["buffer"] = buffer
        return {"action": "persisted", "count": 1, "pending": None,
                "message": "（内部）已自动沉淀 1 个新知识点。"}

    monkeypatch.setattr(graph_mod, "run_memory_sweep", fake_sweep)
    out = graph_mod.coach_memory_write(_sweep_state())
    assert seen["tech"] == "X"
    assert len(seen["buffer"]) == 4  # 2 个用户回合（每回合 assistant + user）
    assert out["memory_sweep_buffer"] == []
    assert "已自动沉淀" in out["coach_messages"][-1]["content"]
    assert "coach_note_pending" not in out


def test_write_triggers_on_chars(monkeypatch):
    # pinned 到 ASYNC=false：v1 同步路径（逃生舱）
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", False)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 100)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 5)  # buffer 必然超
    called = {}
    monkeypatch.setattr(graph_mod, "run_memory_sweep",
                        lambda *a, **k: (called.setdefault("c", True),
                                         {"action": "skip", "count": 0,
                                          "pending": None, "message": None})[1])
    out = graph_mod.coach_memory_write(_sweep_state())
    assert called.get("c")
    assert out["memory_sweep_buffer"] == []


def test_write_pending_sets_state(monkeypatch):
    # pinned 到 ASYNC=false：v1 同步路径（逃生舱）
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", False)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 10000)
    pending = {"merge_candidates": [{"x": 1}], "_tech": "X", "_auto": True}
    monkeypatch.setattr(graph_mod, "run_memory_sweep",
                        lambda *a, **k: {"action": "pending", "count": 1,
                                         "pending": pending,
                                         "message": "（内部）候选..."})
    out = graph_mod.coach_memory_write(_sweep_state())
    assert out["coach_note_pending"] == pending
    assert out["memory_sweep_buffer"] == []


# ============ 图级 e2e：coach 对话积累触发自动沉淀 ============

def _scripted_sweep_chat(system_prompt, messages, tools):
    """问卷固定提问 + planning 工具脚本 + coaching 纯讲解（无工具调用）。"""
    if "水平探测助手" in system_prompt:
        if "正在收集字段：自评熟悉度" in system_prompt:
            return {"content": "你对该技术熟悉程度是几分（0-10）？", "tool_calls": []}
        if "正在收集字段：相关技术" in system_prompt:
            return {"content": "你熟悉哪些相关技术呢？", "tool_calls": []}
        if "正在收集字段：学习目标" in system_prompt:
            return {"content": "想快速上手还是深入原理？", "tool_calls": []}
        if "正在收集字段：时间预算" in system_prompt:
            return {"content": "每天能投入多少小时？", "tool_calls": []}
        if "动态诊断题" in system_prompt:
            return {"content": "来一道诊断题：什么是依赖注入？", "tool_calls": []}
    if "路线规划助手" in system_prompt:
        last = messages[-1] if messages else None
        generated = any(m.get("role") == "tool" and "路线已保存" in (m.get("content") or "")
                        for m in messages)
        if generated and last and last.get("role") == "user":
            return {"content": None,
                    "tool_calls": [{"id": "c_cf", "name": "confirm_roadmap", "arguments": {}}]}
        if last and last.get("role") == "tool":
            return {"content": "路线已生成，请确认", "tool_calls": []}
        return {"content": None, "tool_calls": [{"id": "c_rm", "name": "generate_roadmap", "arguments": {
            "goal": "能跑通最小项目", "total_hours": 12, "revision": "",
            "stages": [{"name": "环境搭建", "goal": "跑通 hello", "est_hours": 4,
                        "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello"}]}]}}]}
    if "执行陪练" in system_prompt:
        return {"content": "知识点讲解：Spring 的核心是依赖注入，容器负责管理 bean 的生命周期。",
                "tool_calls": []}
    return {"content": "（默认回复）", "tool_calls": []}


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


def test_e2e_coaching_sweep_auto_persist(monkeypatch, tmp_path):
    """coaching 连续 2 回合（阈值调低）→ 确定性触发自动沉淀，buffer 清空。
    pinned 到 ASYNC=false：v1 同步路径（逃生舱）的确定性 e2e。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", False)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 100000)
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_sweep_chat)

    sweeps = []
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda tech, text, **k: (sweeps.append(text) or
                                                 dict(_NEW)))
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda tech, np_, mc, idx: {"new_count": len(np_), "results": []})

    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-sweep-e2e"}}
    replies = ["5", "Java", "跑通最小项目", "2小时", "答1", "答2", "可以", "嗯", "明白了", "结束"]
    final, _ = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    assert final["mode"] == "coaching"
    # 触发后清空；最后一回合（说「结束」退出前）的对话留在 buffer（退出不强制扫，设计如此）
    assert len(sweeps) == 1  # 恰好触发一次
    assert len(final["memory_sweep_buffer"]) == 2
    assert final["memory_sweep_buffer"][-1]["content"] == "结束"
    assert any("已自动沉淀 1 个新知识点" in (m.get("content") or "")
               for m in final["coach_messages"])


def test_e2e_coaching_sweep_candidates_need_user(monkeypatch, tmp_path):
    """sweep 产出相似候选 → 确定性确认节点 interrupt 用户 → 决定后落库（不经过 agent）。
    pinned 到 ASYNC=false：v1 同步路径（逃生舱）的确定性 e2e。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_ASYNC", False)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 100000)

    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_sweep_chat)
    monkeypatch.setattr(route_mod, "note_pipeline", lambda tech, text, **k: dict(_CAND))

    # coach_candidate_confirm（graph 节点）调用 graph_mod.persist_points
    commits = []
    monkeypatch.setattr(graph_mod, "persist_points",
                        lambda tech, np_, mc, idx: (commits.append((len(mc), set(idx))) or
                                                    {"new_count": 0, "merged_count": len(mc), "results": []}))

    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-sweep-cand"}}
    replies = ["5", "Java", "跑通最小项目", "2小时", "答1", "答2", "可以", "嗯", "明白了", "all", "结束"]
    final, interrupts = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    # 候选经确定性确认节点 interrupt（用户回 all → 合并 1 条），pending 已清空
    assert commits == [(1, {0})]
    assert final["coach_note_pending"] is None
    # 确认 interrupt 出现（候选列表文本）
    assert any("相似" in (i if isinstance(i, str) else str(i)) for i in interrupts)
