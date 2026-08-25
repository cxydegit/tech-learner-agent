"""记忆系统 Step 3 单测：合并时矛盾识别（merge_notes JSON 解析 + persist_points 收集 + 各展示点透出）。

零网络：monkeypatch generate_text / persist_note / merge_notes / persist_points。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_merge_conflict.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.graph as graph_mod
import src.pipelines.note as note_mod
import src.pipelines.route as route_mod


def _cand():
    """一个合并候选（note_pipeline 产出结构）。"""
    return {
        "old_path": "spring-boot/2026-08-01-默认端口.md",
        "old_topic": "默认端口",
        "old_content": "# 默认端口\n\n> 日期：2026-08-01\n> 标签：#spring-boot\n\n"
                       "Spring Boot 默认端口是 8080。",
        "similarity": 0.9, "reason": "同一主题",
        "topic": "默认端口 3.x 变化", "tags": ["spring-boot"],
        "content": "Spring Boot 3 起默认端口改为 8081。",
    }


# ============ merge_notes（note.py）：JSON 解析 + 矛盾报告 ============

def test_merge_notes_parses_json(monkeypatch):
    """merge_notes 解析 JSON → 返回 {content, report}；旧笔记头部剥掉后才喂 LLM。"""
    raw = ('{"content": "Spring Boot 默认端口是 8081。", '
           '"report": "发现 1 处矛盾：旧笔记说 8080，新内容说 3.x 起为 8081，已按新内容改为 8081。"}')
    captured = {}

    def _generate(s, u):
        captured["n"] = (s, u)
        return raw

    monkeypatch.setattr(note_mod, "generate_text", _generate)
    c = _cand()
    out = note_mod.merge_notes(c["old_content"], c["content"], c["topic"])
    assert out["content"] == "Spring Boot 默认端口是 8081。"
    assert "已按新内容改为 8081" in out["report"]
    # 旧笔记头部被剥掉才喂给 LLM（避免头部当正文重复输出）
    assert "> 日期" not in captured["n"][1]
    assert "Spring Boot 默认端口是 8080" in captured["n"][1]
    assert "Spring Boot 3 起默认端口改为 8081" in captured["n"][1]


def test_merge_notes_no_conflict_empty_report(monkeypatch):
    """无矛盾 → report 为空字符串、content 正常。"""
    monkeypatch.setattr(note_mod, "generate_text", lambda s, u: '{"content": "合并后正文", "report": ""}')
    out = note_mod.merge_notes("旧正文", "新正文", "主题")
    assert out["content"] == "合并后正文"
    assert out["report"] == ""


def test_merge_notes_wraps_code_fence(monkeypatch):
    """兼容 ```json 代码块包裹（parse_json_object 的容错路径）。"""
    raw = '```json\n{"content": "正文", "report": "发现矛盾"}```'
    monkeypatch.setattr(note_mod, "generate_text", lambda s, u: raw)
    out = note_mod.merge_notes("旧", "新", "主题")
    assert out["content"] == "正文"
    assert out["report"] == "发现矛盾"


def test_merge_notes_fallback_plain_markdown(monkeypatch):
    """解析失败（输出纯 markdown）→ 降级 content=原始输出、report=""（与旧行为一致）。"""
    raw = "这是 LLM 直接输出的合并正文，没有 JSON。"
    monkeypatch.setattr(note_mod, "generate_text", lambda s, u: raw)
    out = note_mod.merge_notes("旧正文", "新正文", "主题")
    assert out["content"] == raw
    assert out["report"] == ""


def test_merge_notes_fallback_malformed_json(monkeypatch):
    """解析失败（非法 JSON 且含未转义引号）→ 降级 content=原始输出、report=""。"""
    raw = '{"content": "正文里有一个未转义的双引号"没说全'
    monkeypatch.setattr(note_mod, "generate_text", lambda s, u: raw)
    out = note_mod.merge_notes("旧正文", "新正文", "主题")
    assert out["content"] == raw
    assert out["report"] == ""


# ============ persist_points（note.py）：收集矛盾报告 ============

def test_persist_points_collects_conflict_reports(monkeypatch):
    """合并候选被选中且 merge 报告非空 → 返回 conflict_reports；落盘用合并后的 content、保留旧主题。"""
    persisted = {}
    monkeypatch.setattr(
        note_mod, "persist_note",
        lambda tech, topic, content, tags, **kw: (persisted.setdefault("calls", []).append((topic, content))
                                                  or {"action": "merged", "path": "x.md", "topic": topic}))
    monkeypatch.setattr(note_mod, "merge_notes",
                        lambda old, new, topic: {"content": "合并后 8081", "report": "发现矛盾：已改为 8081"})
    c = _cand()
    out = note_mod.persist_points("spring-boot", [], [c], {0})
    assert out["merged_count"] == 1
    assert out["conflict_reports"] == [{"path": c["old_path"], "topic": c["topic"],
                                        "report": "发现矛盾：已改为 8081"}]
    # 落盘用合并后的 content；主题保留旧笔记主题（identity 属于被合并的旧笔记）
    assert persisted["calls"][0] == (c["old_topic"], "合并后 8081")


def test_persist_points_no_conflict_empty_report(monkeypatch):
    """merge 报告为空 → conflict_reports 为空列表。"""
    monkeypatch.setattr(note_mod, "persist_note",
                        lambda tech, topic, content, tags, **kw: {"action": "merged", "path": "x.md", "topic": topic})
    monkeypatch.setattr(note_mod, "merge_notes", lambda old, new, topic: {"content": "合并后", "report": ""})
    out = note_mod.persist_points("spring-boot", [], [_cand()], {0})
    assert out["conflict_reports"] == []


def test_persist_points_unselected_merge_no_merge_call(monkeypatch):
    """候选未被选中（merge_indices 空）→ 不调 merge_notes、无报告。"""
    called = {}
    monkeypatch.setattr(note_mod, "persist_note",
                        lambda tech, topic, content, tags, **kw: (called.setdefault("p", True)
                                                                  or {"action": "new", "path": "y.md", "topic": topic}))
    monkeypatch.setattr(note_mod, "merge_notes",
                        lambda *a: (_ for _ in ()).throw(AssertionError("未选中的候选不应合并")))
    out = note_mod.persist_points("spring-boot", [], [_cand()], set())
    assert "p" not in called
    assert out["conflict_reports"] == []


def test_persist_points_new_points_no_reports(monkeypatch):
    """纯新建（无合并候选）→ 不产生矛盾报告。"""
    monkeypatch.setattr(note_mod, "persist_note",
                        lambda tech, topic, content, tags, **kw: {"action": "new", "path": "y.md", "topic": topic})
    out = note_mod.persist_points("spring-boot", [{"topic": "t", "tags": [], "content": "c"}], [], set())
    assert out["new_count"] == 1
    assert out["conflict_reports"] == []


# ============ 展示点透出 ============

def test_note_confirm_node_surfaces_report(monkeypatch):
    """CLI 图路径 note_confirm_node：persist 的 conflict_reports 追加进 summary/last_output。"""
    monkeypatch.setattr(graph_mod, "persist_points",
                        lambda *a: {"results": [{"action": "merged", "path": "x.md", "topic": "t"}],
                                    "new_count": 0, "merged_count": 1,
                                    "conflict_reports": [{"path": "x.md", "topic": "t",
                                                          "report": "发现矛盾：已按新内容改为 8081"}]})
    state = {"tech": "spring-boot", "note_result": {"new_points": [], "merge_candidates": []},
             "notes": [{"report": "r"}]}
    out = graph_mod.note_confirm_node(state)
    assert "已按新内容改为 8081" in out["last_output"]
    assert out["noted_count"] == 1  # 未沉淀报告数不受矛盾报告影响


def test_note_commit_surfaces_report(monkeypatch):
    """coach note_commit 工具：conflict_reports 透进工具结果，pending 提交后清空。"""
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda *a: {"results": [], "new_count": 0, "merged_count": 1,
                                    "conflict_reports": [{"path": "x.md", "topic": "t", "report": "发现矛盾"}]})
    ctx = route_mod.CoachCtx({"coach_note_pending": {"_tech": "spring-boot",
                                                     "new_points": [], "merge_candidates": [_cand()]}})
    out = route_mod._note_commit({"decision": "all"}, ctx)
    assert out["conflict_reports"] == [{"path": "x.md", "topic": "t", "report": "发现矛盾"}]
    assert ctx.updates["coach_note_pending"] is None


def test_note_commit_backward_compat_no_reports_key(monkeypatch):
    """persist_points 返回旧结构（无 conflict_reports 键）→ 工具结果带空报告列表，不报错。"""
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda *a: {"results": [], "new_count": 1, "merged_count": 0})
    ctx = route_mod.CoachCtx({"coach_note_pending": {"_tech": "s",
                                                     "new_points": [], "merge_candidates": []}})
    out = route_mod._note_commit({"decision": "skip"}, ctx)
    assert out["conflict_reports"] == []
