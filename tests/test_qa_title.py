"""qa_node 会话标题固化单测（纯 ask 会话不再恒为"新会话"）。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_qa_title.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.graph as graph_mod


def _qa_result():
    return {"answer": "示例回答", "sources": [], "no_hit": False}


def _state(**over):
    base = {"command": "qa", "args": ["python 字典的特点是什么"]}
    base.update(over)
    return base


def test_qa_node_sets_title_from_first_question(monkeypatch):
    """纯 ask 会话（无 title / 无 tech）→ 首次问题被固化为会话标题。"""
    monkeypatch.setattr(graph_mod, "qa_pipeline", lambda *a, **k: _qa_result())
    out = graph_mod.qa_node(_state())
    assert out["title"] == "python 字典的特点是什么"
    assert "qa_history" in out and "conversation" in out


def test_qa_node_keeps_existing_title(monkeypatch):
    """会话已有标题（如先 collect 固化）→ ask 不覆盖。"""
    monkeypatch.setattr(graph_mod, "qa_pipeline", lambda *a, **k: _qa_result())
    out = graph_mod.qa_node(_state(title="Python"))
    assert out.get("title") is None  # 不写回 title，原值保留


def test_qa_node_keeps_existing_tech_as_title_fallback(monkeypatch):
    """有 tech 无 title（早期会话）→ 不写 ask 标题，_summarize 会回退到 tech。"""
    monkeypatch.setattr(graph_mod, "qa_pipeline", lambda *a, **k: _qa_result())
    out = graph_mod.qa_node(_state(tech="python"))
    assert out.get("title") is None


def test_ask_title_truncates_long_question():
    """超长问题截断 + 省略号；末尾问号语气词被剔除。"""
    assert graph_mod._ask_title("Python 列表和字典在增删改查上的差异对比分析总结一下？") == \
        "Python 列表和字典在增删改查上的…"
    assert len(graph_mod._ask_title("Python 列表和字典在增删改查上的差异对比分析总结一下？")) == 20
    assert graph_mod._ask_title("python 字典的特点？") == "python 字典的特点"
    assert graph_mod._ask_title("  ") == ""
