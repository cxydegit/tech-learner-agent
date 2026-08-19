"""src/web/sessions._pending_note_interrupt 单测：从 checkpoint 的 pending_writes 读 note 合并确认负载。

note 合并确认（interrupt 暂停）时，SqliteSaver 的 CheckpointTuple.pending_writes 含
`("task_id", "__interrupt__", [Interrupt(value=候选文本)])`——前端据此在切会话 / 刷新后
恢复决策面板。本测试用最小中断图复现：中断后返回候选文本，resume 完成后返回 None。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_web_pending_note.py -v
"""

import operator
import sys
import tempfile
from pathlib import Path
from typing import Annotated, TypedDict

# 保证 tests/ 下能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt

from src.web import sessions as sessions_mod


class _State(TypedDict, total=False):
    """最小状态：results 累加，模拟 note 合并决策后的入库结果。"""
    results: Annotated[list, operator.add]


def _build_graph(checkpointer):
    """构造一个「interrupt → 等 resume → 返回决策」的图（与 graph.note_confirm_node 的 interrupt 用法一致）。"""

    def ask(state: _State) -> dict:
        decision = interrupt("候选文本：\n1) 相似点 A\n2) 相似点 B")
        return {"results": [decision]}

    return StateGraph(_State).add_node("ask", ask).add_edge(START, "ask").compile(checkpointer=checkpointer)


def test_pending_note_interrupt_reads_candidates_then_clears():
    """中断后 _pending_note_interrupt 返回候选文本；resume 完成后返回 None。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ckpt.sqlite")
        with SqliteSaver.from_conn_string(db) as saver:
            saver.setup()
            graph = _build_graph(saver)
            cfg = {"configurable": {"thread_id": "learn-test"}}

            # 跑到 interrupt：图暂停，checkpoint 带 __interrupt__ pending write
            list(graph.stream({"results": []}, cfg))
            tup = saver.get_tuple(cfg)
            assert tup is not None
            pending = sessions_mod._pending_note_interrupt(tup)
            assert pending is not None, "中断阶段应能读到合并候选"
            assert "候选文本" in pending["candidates_text"]
            assert "相似点 A" in pending["candidates_text"]

            # resume 提交决策：图完成，__interrupt__ pending write 消失
            list(graph.stream(Command(resume="all"), cfg))
            tup2 = saver.get_tuple(cfg)
            assert tup2 is not None
            assert sessions_mod._pending_note_interrupt(tup2) is None


def test_pending_note_interrupt_absent_without_interrupt():
    """从未中断的 checkpoint 返回 None（普通会话详情不带 pending_note）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ckpt.sqlite")
        with SqliteSaver.from_conn_string(db) as saver:
            saver.setup()
            graph = _build_graph(saver)
            cfg = {"configurable": {"thread_id": "learn-plain"}}

            # 空 state 初始 checkpoint：无任何 pending write
            graph.update_state(cfg, {})
            tup = saver.get_tuple(cfg)
            assert tup is not None
            assert sessions_mod._pending_note_interrupt(tup) is None
