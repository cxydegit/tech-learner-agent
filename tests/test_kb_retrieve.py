"""记忆系统单测：确定性读路由（run_kb_retrieve + coach_kb_retrieve + 提示词注入 + 图级 e2e）。

零网络：monkeypatch _search_notes / run_kb_retrieve / chat_with_tools。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_kb_retrieve.py -v
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

# ============ run_kb_retrieve（route.py 纯函数） ============

def _seed_hits():
    """命中集：2 条达标 + 1 条低于阈值（语义检索返回结构，相似度降序）。"""
    return [
        {"id": "a::0", "path": "knowledge/spring/bean.md", "source": "knowledge", "tech": "spring",
         "topic": "Bean 生命周期", "url": "", "similarity": 0.90,
         "document": "Bean 生命周期：实例化 → 属性注入 → 初始化 → 使用 → 销毁。"},
        {"id": "a::1", "path": "knowledge/spring/scope.md", "source": "knowledge", "tech": "spring",
         "topic": "作用域", "url": "", "similarity": 0.70,
         "document": "单例与原型作用域：单例共享一个实例，原型每次获取新建。"},
        {"id": "b::0", "path": "knowledge/spring/other.md", "source": "knowledge", "tech": "spring",
         "topic": "无关", "url": "", "similarity": 0.30,
         "document": "无关的旧片段。"},
    ]


def test_meta_question_no_search(monkeypatch):
    """廉价闸门：明显过程/元问题不检索（命中即返回空，零成本跳过）。"""
    def boom(q, k, t):
        raise AssertionError("元问题不应触发检索")
    monkeypatch.setattr(route_mod, "_search_notes", boom)
    for q in ("继续", "现在到哪了", "这个路线对吗", "好的，继续"):
        assert route_mod.run_kb_retrieve("X", q) == []


def test_non_meta_question_searches(monkeypatch):
    """含学习内容的提问不被廉价闸门误判，正常检索（tech 透传）。"""
    seen = {}
    monkeypatch.setattr(route_mod, "_search_notes",
                        lambda q, k, t: (seen.update(q=q, k=k, t=t) or []))
    assert route_mod.run_kb_retrieve("X", "Spring 的 Bean 生命周期是怎样的？") == []
    assert seen["q"] == "Spring 的 Bean 生命周期是怎样的？"
    assert seen["t"] == "X"


def test_empty_question_or_tech(monkeypatch):
    """空问题 / 空 tech → 直接空，不检索。"""
    called = {}
    monkeypatch.setattr(route_mod, "_search_notes",
                        lambda q, k, t: called.setdefault("c", True) or [])
    assert route_mod.run_kb_retrieve("X", "   ") == []
    assert route_mod.run_kb_retrieve("", "问题") == []
    assert not called


def test_below_threshold_not_injected(monkeypatch):
    """质量闸门：命中相似度全部低于阈值 → 不注入。"""
    hits = [{"path": "p1", "similarity": 0.4, "document": "d1"},
            {"path": "p2", "similarity": 0.3, "document": "d2"}]
    monkeypatch.setattr(route_mod, "_search_notes", lambda q, k, t: hits)
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.5)
    assert route_mod.run_kb_retrieve("X", "问题") == []


def test_above_threshold_injected(monkeypatch):
    """质量闸门：达标片段注入（含来源路径），低于阈值的丢弃。"""
    monkeypatch.setattr(route_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.5)
    monkeypatch.setattr(config, "ROUTE_KB_SNIPPETS", 3)
    monkeypatch.setattr(config, "QA_SNIPPET_CHARS", 1000)
    out = route_mod.run_kb_retrieve("X", "Bean 生命周期")
    assert out == [
        {"path": "knowledge/spring/bean.md",
         "snippet": "Bean 生命周期：实例化 → 属性注入 → 初始化 → 使用 → 销毁。"},
        {"path": "knowledge/spring/scope.md",
         "snippet": "单例与原型作用域：单例共享一个实例，原型每次获取新建。"},
    ]


def test_snippet_cap(monkeypatch):
    """注入片段数上限：超过 ROUTE_KB_SNIPPETS 只取前 N 条。"""
    hits = [{"path": f"p{i}", "similarity": 0.9 - i * 0.1, "document": f"d{i}"} for i in range(5)]
    monkeypatch.setattr(route_mod, "_search_notes", lambda q, k, t: hits)
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.0)
    monkeypatch.setattr(config, "ROUTE_KB_SNIPPETS", 3)
    out = route_mod.run_kb_retrieve("X", "问题")
    assert len(out) == 3


def test_snippet_truncated(monkeypatch):
    """片段按 QA_SNIPPET_CHARS 截断（复用 qa 截断参数）。"""
    monkeypatch.setattr(route_mod, "_search_notes",
                        lambda q, k, t: [{"path": "p", "similarity": 0.9, "document": "ABCDEF"}])
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.0)
    monkeypatch.setattr(config, "QA_SNIPPET_CHARS", 3)
    out = route_mod.run_kb_retrieve("X", "问题")
    assert out[0]["snippet"] == "ABC"


def test_search_exception_degrades_empty(monkeypatch):
    """检索异常（RAG 未索引 / Chroma 异常）优雅降级为空，不抛错。"""
    def boom(q, k, t):
        raise RuntimeError("Chroma down")
    monkeypatch.setattr(route_mod, "_search_notes", boom)
    assert route_mod.run_kb_retrieve("X", "问题") == []


# ============ _coaching_prompt 注入 kb_context（提示词改动待用户审核） ============

def test_coaching_prompt_injects_kb_block():
    """命中知识库时，coaching 提示词渲染「知识库相关片段」块（标注来源 + 优先依据作答）。"""
    state = {
        "mode": "coaching", "roadmap": None, "coach_summary": "",
        "kb_context": [
            {"path": "knowledge/spring/bean.md", "snippet": "Bean 生命周期：实例化→注入→初始化。"},
            {"path": "knowledge/spring/scope.md", "snippet": "单例与原型作用域。"},
        ],
    }
    prompt = route_mod._coaching_prompt(state, "Spring")
    assert "知识库相关片段" in prompt
    assert "knowledge/spring/bean.md" in prompt
    assert "Bean 生命周期" in prompt
    assert "优先依据上述片段作答" in prompt
    assert "标注来源" in prompt


def test_coaching_prompt_without_kb():
    """无命中（kb_context 为空/None）时提示词不含片段块，行为不变。"""
    for kb in (None, []):
        state = {"mode": "coaching", "roadmap": None, "coach_summary": "", "kb_context": kb}
        prompt = route_mod._coaching_prompt(state, "Spring")
        assert "知识库相关片段" not in prompt
        assert "执行陪练" in prompt


# ============ coach_kb_retrieve（graph.py 节点） ============

def _kb_state(**over):
    state = {
        "mode": "coaching",
        "tech": "X",
        "coach_messages": [
            {"role": "assistant", "content": "讲解：Spring 的核心。", "type": "coach"},
            {"role": "user", "content": "Spring 的 Bean 生命周期？"},
        ],
        "kb_context": None,
    }
    state.update(over)
    return state


def _no_retrieve(*a, **k):
    raise AssertionError("不应触发检索")


def test_node_skips_non_coaching(monkeypatch):
    """非 coaching 模式（survey/planning）不检索，返回空更新。"""
    monkeypatch.setattr(graph_mod, "run_kb_retrieve", _no_retrieve)
    for mode in ("survey", "planning"):
        assert graph_mod.coach_kb_retrieve(_kb_state(mode=mode)) == {}


def test_node_uses_last_user_message(monkeypatch):
    """取最后一条用户消息作问题（跳过 assistant 讲解）；无命中清空 kb_context。"""
    seen = {}
    monkeypatch.setattr(graph_mod, "run_kb_retrieve",
                        lambda tech, q: (seen.update(tech=tech, q=q) or []))
    out = graph_mod.coach_kb_retrieve(_kb_state())
    assert seen["q"] == "Spring 的 Bean 生命周期？"
    assert seen["tech"] == "X"
    assert out == {"kb_context": None}


def test_node_sets_kb_context(monkeypatch):
    """命中 → 片段写入 kb_context。"""
    ctx = [{"path": "knowledge/spring/bean.md", "snippet": "s1"}]
    monkeypatch.setattr(graph_mod, "run_kb_retrieve", lambda tech, q: ctx)
    out = graph_mod.coach_kb_retrieve(_kb_state())
    assert out == {"kb_context": ctx}


def test_node_clears_kb_context(monkeypatch):
    """无命中 → 覆盖清空旧 kb_context（每用户回合替换）。"""
    monkeypatch.setattr(graph_mod, "run_kb_retrieve", lambda tech, q: [])
    out = graph_mod.coach_kb_retrieve(_kb_state(kb_context=[{"path": "old", "snippet": "old"}]))
    assert out == {"kb_context": None}


# ============ 图级 e2e：用户提问 → 命中注入 → agent 依据片段回答 ============

kb_seen = {}


def _scripted_kb_chat(system_prompt, messages, tools):
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
        if "知识库相关片段" in system_prompt:
            kb_seen["injected"] = True
            return {"content": "根据你的笔记，Spring Bean 生命周期是实例化→注入→初始化（来源：knowledge/spring/bean.md）。",
                    "tool_calls": []}
        return {"content": "Spring 有什么想深入了解的？", "tool_calls": []}
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


def test_e2e_question_hits_kb_and_injects(monkeypatch, tmp_path):
    """coaching 用户提问命中知识库 → kb_context 注入 → agent 依据片段回答。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    monkeypatch.setattr(graph_mod, "chat_with_tools", _scripted_kb_chat)
    monkeypatch.setattr(graph_mod, "run_kb_retrieve",
                        lambda tech, q: [{"path": "knowledge/spring/bean.md",
                                          "snippet": "Bean 生命周期：实例化→注入→初始化。"}]
                        if "Bean" in q else [])
    kb_seen.clear()

    graph = build_graph(InMemorySaver())
    gconfig = {"configurable": {"thread_id": "test-kb-e2e"}}
    replies = ["5", "Java", "跑通最小项目", "2小时", "答1", "答2", "可以",
               "Spring 的 Bean 生命周期？", "结束"]
    final, _ = _run(graph, gconfig, {"command": "route", "tech": "X"}, replies)

    assert final["mode"] == "coaching"
    assert kb_seen["injected"] is True  # coaching 提示词渲染了知识库片段块
    assert final["kb_context"] == [{"path": "knowledge/spring/bean.md",
                                    "snippet": "Bean 生命周期：实例化→注入→初始化。"}]
