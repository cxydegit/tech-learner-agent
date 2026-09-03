"""记忆系统单测：并行沉淀（后台线程 + 内存侧信道）—— fire / drain / 超时兜底 / e2e。

零网络：monkeypatch note_pipeline / run_memory_sweep / _start_sweep_thread。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_sweep_async.py -v
"""

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import src.graph as graph_mod
import src.pipelines.route as route_mod
from src.config import config
from src.graph import build_graph

_BUF = [{"role": "assistant", "content": "讲解一"}, {"role": "user", "content": "懂了"},
        {"role": "assistant", "content": "讲解二"}, {"role": "user", "content": "继续"}]
_NOW = datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005 —— 与 graph.isoformat 保持 naive 字符串以做陈旧比较
_STALE = "2000-01-01T00:00:00"


@pytest.fixture(autouse=True)
def _clean_sweep_results():
    graph_mod._sweep_results.clear()
    yield
    graph_mod._sweep_results.clear()


def _state(**over):
    s = {
        "mode": "coaching",
        "memory_sweep_buffer": list(_BUF),
        "memory_sweep_inflight": None,
        "coach_note_pending": None,
        "coach_messages": [{"role": "user", "content": "继续"}],
        "tech": "X",
    }
    s.update(over)
    return s


def _inflight(fired=_NOW, buffer=None):
    return {"tech": "X", "buffer": buffer if buffer is not None else list(_BUF), "fired_at": fired}


# ============ fire：达阈值 → 快照 + 后台线程 ============

def test_fire_threshold_met_spawns_thread(monkeypatch):
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 100000)
    captured = {}

    def fake_start(tech, buffer, tid):
        captured["tech"] = tech
        captured["buffer"] = buffer
        captured["tid"] = tid

    monkeypatch.setattr(graph_mod, "_start_sweep_thread", fake_start)
    out = graph_mod.coach_memory_write(_state())
    assert captured["tech"] == "X"
    assert len(captured["buffer"]) == 4  # 快照 = 触发时的完整 buffer
    assert out["memory_sweep_buffer"] == []  # 清空，交给后台
    assert out["memory_sweep_inflight"]["buffer"] == _BUF  # 快照入 inflight
    assert "fired_at" in out["memory_sweep_inflight"]
    assert "coach_messages" not in out  # 并行：本回合不立即应用


def test_fire_below_threshold_keeps_buffer(monkeypatch):
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 100)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 100000)
    monkeypatch.setattr(graph_mod, "_start_sweep_thread",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应 spawn")))
    assert graph_mod.coach_memory_write(_state()) == {}


def test_fire_skips_when_pending(monkeypatch):
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 100000)
    monkeypatch.setattr(graph_mod, "_start_sweep_thread",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应 spawn")))
    assert graph_mod.coach_memory_write(_state(coach_note_pending={"x": 1})) == {}


# ============ drain：后台结果就绪 → 应用 ============

def test_drain_persisted_applies_and_clears(monkeypatch):
    graph_mod._sweep_results["local"] = {
        "action": "persisted", "count": 1, "pending": None,
        "message": "（内部）已自动沉淀 1 个新知识点。"}
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight()))
    assert out["memory_sweep_inflight"] is None
    assert "已自动沉淀" in out["coach_messages"][-1]["content"]
    assert "coach_note_pending" not in out
    assert "local" not in graph_mod._sweep_results  # 已弹出


def test_drain_pending_sets_pending(monkeypatch):
    """pending → 暂存候选（图路由到确定性确认节点），不加 system 提示、不经过 agent。"""
    pending = {"merge_candidates": [{"x": 1}], "_tech": "X", "_auto": True}
    graph_mod._sweep_results["local"] = {
        "action": "pending", "count": 1, "pending": pending, "message": "（内部）候选..."}
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight()))
    assert out["memory_sweep_inflight"] is None
    assert out["coach_note_pending"] == pending
    assert "coach_messages" not in out  # 候选由 coach_candidate_confirm 确定性确认


def test_drain_skip_just_clears(monkeypatch):
    graph_mod._sweep_results["local"] = {
        "action": "skip", "count": 0, "pending": None, "message": None}
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight()))
    assert out == {"memory_sweep_inflight": None}


def test_drain_thread_still_running_returns_empty(monkeypatch):
    """后台线程未完成、未超时 → 本回合不阻塞、不重复 fire、保留 inflight。"""
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(graph_mod, "_start_sweep_thread",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应 fire")))
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight(fired=_NOW)))
    assert out == {}


def test_drain_stale_restores_buffer(monkeypatch):
    """后台线程超时（进程重启等）→ 快照并回 buffer，未来正常 fire 重扫（不阻塞、不重跑）。"""
    monkeypatch.setattr(graph_mod, "run_memory_sweep",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应重跑")))
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight(fired=_STALE)))
    assert out["memory_sweep_inflight"] is None
    assert len(out["memory_sweep_buffer"]) == 8  # 快照 4 + 当前 buffer 4


def test_drain_error_restores_buffer(monkeypatch):
    """后台线程报 error → 快照并回 buffer，不把 error 当结果应用（不阻塞、不重跑）。"""
    graph_mod._sweep_results["local"] = {"action": "error", "error": "RuntimeError: x"}
    monkeypatch.setattr(graph_mod, "run_memory_sweep",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应重跑")))
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight()))
    assert out["memory_sweep_inflight"] is None
    assert len(out["memory_sweep_buffer"]) == 8


def test_drain_persisted_emits_feedback(monkeypatch):
    """persisted → 确定性 SSE 反馈（不经过 agent）。"""
    seen = {}
    graph_mod._sweep_results["local"] = {
        "action": "persisted", "count": 2, "pending": None,
        "message": "（内部）已自动沉淀 2 个新知识点。"}
    monkeypatch.setattr(graph_mod, "_emit_sweep_feedback",
                        lambda m: seen.setdefault("msg", m))
    out = graph_mod.coach_memory_write(_state(memory_sweep_inflight=_inflight()))
    assert "已自动沉淀 2 个" in seen.get("msg", "")
    assert out["memory_sweep_inflight"] is None
    assert "已自动沉淀 2 个" in out["coach_messages"][-1]["content"]  # system 提示保留


def test_candidate_confirm_node(monkeypatch):
    """确定性候选确认：interrupt 用户 → 解析决定 → 落库 → 清 pending（不经过 agent）。"""
    pending = {"merge_candidates": [{"old_path": "x.md", "old_topic": "旧", "topic": "T1",
                                     "similarity": 0.8, "reason": "相似", "content": "C1"}],
               "new_points": [{"topic": "T1", "tags": ["X"], "content": "C1"}],
               "_tech": "X", "_auto": True}
    monkeypatch.setattr(graph_mod, "interrupt", lambda *a, **k: "all")
    seen = {}

    def fake_persist(tech, np_, mc, idx):
        seen["tech"] = tech
        seen["mc"] = mc
        seen["idx"] = set(idx)
        return {"new_count": 1, "merged_count": len(mc), "results": []}

    monkeypatch.setattr(graph_mod, "persist_points", fake_persist)
    out = graph_mod.coach_candidate_confirm(_state(coach_note_pending=pending))
    assert seen["tech"] == "X"
    assert seen["idx"] == {0}
    assert out == {"coach_note_pending": None}


def test_route_after_memory_write():
    """任何 pending（含旧 note 工具流遗留的无 _auto）→ 确定性确认节点；无 pending → 正常读路由。"""
    assert graph_mod._route_after_memory_write(
        _state(coach_note_pending={"merge_candidates": [{}], "_auto": True})) == "coach_candidate_confirm"
    assert graph_mod._route_after_memory_write(
        _state(coach_note_pending={"merge_candidates": [{}]})) == "coach_candidate_confirm"  # 旧遗留也路由
    assert graph_mod._route_after_memory_write(_state()) == "coach_kb_retrieve"


# ============ 真实 daemon 线程机制 ============

def test_start_sweep_thread_writes_result(monkeypatch):
    monkeypatch.setattr(graph_mod, "run_memory_sweep",
                        lambda *a, **k: {"action": "persisted", "count": 1,
                                         "pending": None, "message": "ok"})
    graph_mod._start_sweep_thread("X", _BUF, "tid-real")
    deadline = time.time() + 2
    while time.time() < deadline:
        if "tid-real" in graph_mod._sweep_results:
            break
        time.sleep(0.01)
    assert graph_mod._sweep_results.get("tid-real", {}).get("action") == "persisted"


def test_start_sweep_thread_error_recorded(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(graph_mod, "run_memory_sweep", boom)
    graph_mod._start_sweep_thread("X", _BUF, "tid-err")
    deadline = time.time() + 2
    while time.time() < deadline:
        if "tid-err" in graph_mod._sweep_results:
            break
        time.sleep(0.01)
    assert graph_mod._sweep_results["tid-err"]["action"] == "error"


# ============ 图级 e2e：fire（后台 mock 为同步落结果）→ 下一回合排水生效 ============

def _scripted_chat(system_prompt, messages, tools):
    if "水平探测助手" in system_prompt:
        if "自评熟悉度" in system_prompt:
            return {"content": "你对该技术熟悉程度是几分（0-10）？", "tool_calls": []}
        if "相关技术" in system_prompt:
            return {"content": "你熟悉哪些相关技术呢？", "tool_calls": []}
        if "学习目标" in system_prompt:
            return {"content": "想快速上手还是深入原理？", "tool_calls": []}
        if "时间预算" in system_prompt:
            return {"content": "每天能投入多少小时？", "tool_calls": []}
        if "动态诊断题" in system_prompt:
            return {"content": "诊断题：什么是依赖注入？", "tool_calls": []}
    if "路线规划助手" in system_prompt:
        last = messages[-1] if messages else None
        generated = any(m.get("role") == "tool" and "路线已保存" in (m.get("content") or "")
                        for m in messages)
        if generated and last and last.get("role") == "user":
            return {"content": None,
                    "tool_calls": [{"id": "cf", "name": "confirm_roadmap", "arguments": {}}]}
        if last and last.get("role") == "tool":
            return {"content": "路线已生成，请确认", "tool_calls": []}
        return {"content": None, "tool_calls": [{"id": "rm", "name": "generate_roadmap", "arguments": {
            "goal": "跑通最小项目", "total_hours": 12, "revision": "",
            "stages": [{"name": "环境搭建", "goal": "hello", "est_hours": 4,
                        "milestones": [{"desc": "安装"}, {"desc": "hello"}]}]}}]}
    if "执行陪练" in system_prompt:
        return {"content": "讲解：Spring 的核心是依赖注入。", "tool_calls": []}
    return {"content": "（默认回复）", "tool_calls": []}


def _run(graph, gconfig, payload, replies, *, max_iters=80):
    it = iter(replies)
    stream_input = payload
    for _ in range(max_iters):
        stream = graph.stream_events(stream_input, gconfig, version="v3")
        if not stream.interrupted:
            return stream.output
        try:
            reply = next(it)
        except StopIteration:
            reply = ""
        stream_input = Command(resume=reply)
    raise AssertionError("max_iters 内未收敛")


def test_e2e_async_fire_then_drain(monkeypatch, tmp_path):
    """coaching 第 2 回合 fire（后台 mock 同步落结果）→ 第 3 回合排水应用 system 提示。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", tmp_path / "knowledge")  # 防泄漏真实知识库
    monkeypatch.setattr(config, "CHROMA_DIR", tmp_path / ".chroma")  # 索引同样隔离，防对账误删真实库
    # vector 全局单例若已指向真实 .chroma，重置让 get_collection 落到上面的临时目录
    import src.adapters.vector as vector_mod
    monkeypatch.setattr(vector_mod, "_client", None)
    monkeypatch.setattr(vector_mod, "_collection", None)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_TURNS", 2)
    monkeypatch.setattr(config, "ROUTE_MEMORY_SWEEP_CHARS", 100000)
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_chat)
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda tech, text, **k: {"new_points": [{"topic": "依赖注入",
                                                                 "tags": ["X"], "content": "C1"}],
                                                 "merge_candidates": [], "empty_reason": None})
    # ⚠️ 必须 mock persist_points：run_memory_sweep 内部会真实落库，不 mock 会污染真实 knowledge/
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda tech, np_, mc, idx: {"new_count": len(np_), "merged_count": 0, "results": []})

    def fake_start(tech, buffer, tid):
        # 测试内同步跑后台 worker（真实图里是 daemon 线程，这里 mock 保证确定性）
        result = graph_mod.run_memory_sweep(tech, buffer)
        with graph_mod._sweep_results_lock:
            graph_mod._sweep_results[tid] = result

    monkeypatch.setattr(graph_mod, "_start_sweep_thread", fake_start)

    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-async-e2e"}}
    replies = ["5", "Java", "跑通最小项目", "2小时", "答1", "答2", "可以",
               "嗯", "明白了", "接着讲", "结束"]
    final = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    # 第 2 回合 fire → 第 3 回合排水应用（system 提示进 coach_messages）；排水不清 buffer
    assert final["memory_sweep_inflight"] is None  # 已排水清空
    # 结束回合跳过 memory_write（退出不强制扫）：buffer 留「接着讲」+「结束」两回合
    assert len(final["memory_sweep_buffer"]) == 4
    assert final["memory_sweep_buffer"][-1]["content"] == "结束"
    assert any("已自动沉淀 1 个新知识点" in (m.get("content") or "")
               for m in final["coach_messages"])
