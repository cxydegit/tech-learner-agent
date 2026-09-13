"""CLI 图驱动的接线单测（零网络）：进度回调必须注册进注册表，让 route 也能看到进度。

背景：`_get_progress()` 只从注册表取回调，而过去只有 Web 注册（`web_progress`），
CLI 的 route 路径拿不到 → collect/read 这类要跑几分钟的工具全程静默。本用例守住这条接线。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_cli.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.graph as graph_mod
from src.cli import _drive


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
