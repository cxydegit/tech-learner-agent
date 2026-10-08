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
    """注入篇数上限：超过 ROUTE_KB_SNIPPETS 只取前 N **篇**。"""
    hits = [{"path": f"p{i}", "similarity": 0.9 - i * 0.1, "document": f"d{i}"} for i in range(5)]
    monkeypatch.setattr(route_mod, "_search_notes", lambda q, k, t: hits)
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.0)
    monkeypatch.setattr(config, "ROUTE_KB_SNIPPETS", 3)
    out = route_mod.run_kb_retrieve("X", "问题")
    assert len(out) == 3


def test_same_note_takes_one_slot(monkeypatch):
    """同一篇笔记的多个分块只占一个名额：N 是**篇数**，不被同篇分块重复占用。

    旧口径按条取前 3，这条 seed 会返回 3 条全是同一篇的片段（1 篇）；新口径取 3 篇。
    """
    hits = [{"path": "knowledge/spring/bean.md", "similarity": 0.90, "document": "块1"},
            {"path": "knowledge/spring/bean.md", "similarity": 0.88, "document": "块2"},
            {"path": "knowledge/spring/bean.md", "similarity": 0.86, "document": "块3"},
            {"path": "knowledge/spring/scope.md", "similarity": 0.80, "document": "作用域"},
            {"path": "knowledge/spring/aop.md", "similarity": 0.75, "document": "AOP"}]
    monkeypatch.setattr(route_mod, "_search_notes", lambda q, k, t: hits)
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.0)
    monkeypatch.setattr(config, "ROUTE_KB_SNIPPETS", 3)
    out = route_mod.run_kb_retrieve("X", "Bean 生命周期")
    # 3 个名额给 3 篇不同笔记（旧口径会给同一篇的 3 个块）
    assert [o["path"] for o in out] == ["knowledge/spring/bean.md",
                                        "knowledge/spring/scope.md",
                                        "knowledge/spring/aop.md"]
    assert out[0]["snippet"] == "块1"  # 取该篇最靠前（相似度最高）的那条


def test_recall_depth_from_config(monkeypatch):
    """召回深度按 QA_RECALL_CHUNKS（分块）取，而不是旧的按条数截断值。"""
    seen = {}
    monkeypatch.setattr(route_mod, "_search_notes",
                        lambda q, k, t: (seen.update(k=k) or []))
    monkeypatch.setattr(config, "QA_RECALL_CHUNKS", 24)
    route_mod.run_kb_retrieve("X", "Spring 的 Bean 生命周期？")
    assert seen["k"] == 24


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


# ============ kb_context 注入：system → 末尾注入消息（前缀缓存：system 保持会话级稳定） ============

def test_runtime_context_injects_kb_block():
    """命中知识库时，kb 片段渲染进末尾注入消息（标注来源 + 优先依据作答）；system 不再含它。"""
    state = {
        "mode": "coaching", "roadmap": None, "coach_summary": "",
        "kb_context": [
            {"path": "knowledge/spring/bean.md", "snippet": "Bean 生命周期：实例化→注入→初始化。"},
            {"path": "knowledge/spring/scope.md", "snippet": "单例与原型作用域。"},
        ],
    }
    inj = route_mod.coach_runtime_context(state)
    assert inj is not None and inj["role"] == "system"
    assert "系统注入的运行时状态" in inj["content"]
    assert "知识库相关片段" in inj["content"]
    assert "knowledge/spring/bean.md" in inj["content"]
    assert "Bean 生命周期" in inj["content"]
    assert "优先依据上述片段作答" in inj["content"]
    assert "标注来源" in inj["content"]
    # system 提示词不再含片段块（前缀缓存的前提：system 会话级稳定）
    prompt = route_mod._coaching_prompt(state, "Spring")
    assert "知识库相关片段" not in prompt


def test_runtime_context_without_kb_or_pending():
    """无命中且无卡点 → 不产生注入消息（请求 messages 原样），system 行为不变。"""
    for kb in (None, []):
        state = {"mode": "coaching", "roadmap": None, "coach_summary": "", "kb_context": kb}
        assert route_mod.coach_runtime_context(state) is None
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


def _scripted_kb_chat(system_prompt, messages, tools, **_kw):
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
        # 方案 B 后 kb 片段在末尾注入消息里，不在 system（见 coach_runtime_context）
        if any("知识库相关片段" in (m.get("content") or "") for m in messages):
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
    assert kb_seen["injected"] is True  # coaching 请求渲染了知识库片段注入消息
    assert final["kb_context"] == [{"path": "knowledge/spring/bean.md",
                                    "snippet": "Bean 生命周期：实例化→注入→初始化。"}]
    # 注入消息用完即弃：绝不落盘 coach_messages（落盘 = 过期指令累积 + 上下文膨胀）
    assert not any("知识库相关片段" in (m.get("content") or "")
                   for m in final["coach_messages"])
