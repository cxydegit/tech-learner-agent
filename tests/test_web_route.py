"""src/web route 卡片接入单测：_build_payload / _pending_interrupt(coach)。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_web_route.py -v
"""

import queue
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from src.config import config
from src.web import runner as runner_mod
from src.web import server as server_mod
from src.web import sessions as sessions_mod


def test_build_payload_route():
    req = server_mod.RunRequest(command="route", tech="Spring Boot")
    payload = server_mod._build_payload(req)
    assert payload == {"command": "route", "tech": "Spring Boot"}


def test_build_payload_route_missing_tech():
    req = server_mod.RunRequest(command="route", tech="   ")
    payload = server_mod._build_payload(req)
    assert "error" in payload


class _CoachState(TypedDict, total=False):
    r: dict


def test_pending_interrupt_coach_question():
    """coach_question（dict 负载）识别为 coach kind；note 兼容函数返回 None。"""

    def ask(state: _CoachState) -> dict:
        value = interrupt({"type": "coach_question", "mode": "survey", "tech": "X", "message": "Q1?"})
        return {"r": value}

    from langgraph.checkpoint.sqlite import SqliteSaver

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "ckpt.sqlite")
        with SqliteSaver.from_conn_string(db) as saver:
            saver.setup()
            graph = (StateGraph(_CoachState)
                     .add_node("ask", ask).add_edge(START, "ask").compile(checkpointer=saver))
            cfg = {"configurable": {"thread_id": "t1"}}
            list(graph.stream({}, cfg))
            tup = saver.get_tuple(cfg)
            p = sessions_mod._pending_interrupt(tup)
            assert p is not None
            assert p["kind"] == "coach_question"
            assert p["value"]["message"] == "Q1?"
            # coach interrupt 不应被 note 兼容读取（向后兼容语义）
            assert sessions_mod._pending_note_interrupt(tup) is None


def _drain(job: runner_mod.Job, timeout: float = 12.0) -> list[dict]:
    """轮询 job.queue 直到收到 done（worker 结束），返回全部事件。"""
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            evt = job.queue.get_nowait()
        except queue.Empty:
            if not job.active and job.queue.empty():
                break
            time.sleep(0.05)
            continue
        seen.append(evt)
        if evt.get("type") == "done":
            break
    return seen


def test_runner_coach_flow_end_to_end(monkeypatch, tmp_path):
    """后台执行 route：coach_question interrupt → resume → final（Web 整条后端链路）。"""
    monkeypatch.setattr(config, "GRAPH_DB_DIR", tmp_path)
    monkeypatch.setattr(config, "GRAPH_DB_PATH", tmp_path / "ckpt.sqlite")
    import src.graph as graph_mod
    monkeypatch.setattr(graph_mod, "chat_with_tools",
                        lambda sp, msgs, tools: {"content": "你对这个技术熟悉程度是？0-10 打几分？",
                                                 "tool_calls": []})

    assert runner_mod.start_run("t-route", {"command": "route", "tech": "X"}) is None
    evts = _drain(runner_mod.get_job("t-route"))
    interrupts = [e for e in evts if e.get("type") == "interrupt"]
    assert interrupts, "run 阶段应收到 interrupt"
    assert interrupts[0]["kind"] == "coach_question"
    assert "0-10" in interrupts[0]["payload"]["message"]

    # resume（用户回复「结束」→ 退出流程）→ final
    assert runner_mod.resume_run("t-route", "结束") is None
    evts2 = _drain(runner_mod.get_job("t-route"))
    assert any(e.get("type") == "final" for e in evts2), "resume 后应收到 final"
