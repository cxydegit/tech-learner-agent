"""CLI 图驱动的接线单测（零网络）：进度回调必须注册进注册表，让 route 也能看到进度。

背景：`_get_progress()` 只从注册表取回调，而过去只有 Web 注册（`web_progress`），
CLI 的 route 路径拿不到 → collect/read 这类要跑几分钟的工具全程静默。本用例守住这条接线。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_cli.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from click.testing import CliRunner

import src.graph as graph_mod
from src.adapters import learner
from src.adapters import memory as memory_store
from src.adapters import memory_search as memory_search_store
from src.cli import _drive, cli
from src.config import config


class _FakeStream:
    """替身 v3 流：非中断收尾。"""

    interrupted = False
    interrupts: list = []
    output = {"last_output": ""}


class _FakeGraph:
    def __init__(self):
        self.seen_registry: list[str] = []

    def stream_events(self, payload, cfg, version):  # noqa: ARG002
        # 模拟"节点执行期间"：这一刻注册表里应该已经有本线程的回调
        self.seen_registry = list(graph_mod._progress_registry.keys())
        return _FakeStream()


def test_drive_registers_progress_callback_for_cli():
    """_drive 调图时要把进度回调注册进册（与 Web 同一套），且出 with 后不留残留。"""
    graph = _FakeGraph()
    final = _drive(graph, {"configurable": {"thread_id": "t-cli"}},
                   {"command": "route", "tech": "X"})
    assert graph.seen_registry == ["t-cli"]        # 图执行期间在册 → 进度能打出来
    assert graph_mod._progress_registry == {}      # 退出后注销，不污染下一个 run
    assert final is not None


def test_memory_brief_command_is_wired(monkeypatch):
    """memory brief 只读视图，不调 LLM；空视图也要能正常出提示。"""
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    monkeypatch.setattr(memory_store, "ensure_view", lambda: {})
    monkeypatch.setattr(learner, "load_profile", lambda: {})
    monkeypatch.setattr(learner, "load_roadmap", lambda tech: None)
    result = CliRunner().invoke(
        cli, ["memory", "brief", "--mode", "coaching", "--tech", "python"])
    assert result.exit_code == 0
    assert "长期记忆简报" in result.output
    assert "当前没有 delta" in result.output


def test_memory_recall_command_is_wired(monkeypatch):
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    record = {
        "id": "cap:python:1", "kind": "capability", "text": "能写函数",
        "tech": "Python", "tech_key": "python", "scope": "tech",
        "key": None, "evidence": "verified", "status": "active",
        "date": "2026-11-01", "source": "view",
    }
    monkeypatch.setattr(memory_search_store, "load_records", lambda: [record])
    result = CliRunner().invoke(
        cli, ["memory", "recall", "--tech", "Python", "--kind", "capability"])
    assert result.exit_code == 0
    assert "能写函数" in result.output
    assert "记忆查询" in result.output


def test_memory_recall_evidence_filter_is_wired(monkeypatch):
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    records = [
        {"id": "v", "kind": "capability", "text": "已验收点",
         "tech": "Python", "tech_key": "python", "scope": "tech",
         "key": None, "evidence": "verified", "status": "active",
         "date": "2026-11-01", "source": "view"},
        {"id": "u", "kind": "capability", "text": "未考据点",
         "tech": "Python", "tech_key": "python", "scope": "tech",
         "key": None, "evidence": "unknown", "status": "active",
         "date": "2026-11-01", "source": "view"},
    ]
    monkeypatch.setattr(memory_search_store, "load_records", lambda: records)
    result = CliRunner().invoke(
        cli, ["memory", "recall", "--tech", "Python",
              "--kind", "capability", "--evidence", "unknown"])
    assert result.exit_code == 0
    assert "未考据点" in result.output
    assert "已验收点" not in result.output
