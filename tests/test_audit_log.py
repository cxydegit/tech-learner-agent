"""审计事件层单测（零网络）：落盘 / 会话归属 / 后台线程归属 / 启动快照 / 降级。

覆盖可观测性方案阶段 0 的四条验收：日志落盘且每行是完整 JSON、按会话能捞出全部事件
（**含后台沉淀线程产生的**）、启动快照可用、测试不污染真实日志目录。
"""

import json
import time
from pathlib import Path

import pytest

from src import graph as graph_mod
from src.adapters import audit as audit_mod
from src.adapters.audit import audit, audit_context, boot
from src.config import config

TID = "learn-20260101-000000"

# langgraph 的 v3 流式协议会打实验性告警；graph.py 在导入时过滤了，但 pytest 每个用例都会
# 重置告警过滤器，所以这里再声明一次（只是噪声，不是问题）。
pytestmark = pytest.mark.filterwarnings("ignore:The v3 streaming protocol on Pregel is experimental.")


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------- 0-1 落盘 ----------

def test_event_lands_as_valid_jsonl(tmp_path, capsys):
    """落盘每行必须是**完整 JSON**；stderr 侧仍带人读前缀（两者要求不同，不能共用一个 formatter）。"""
    audit_mod.configure_audit_log()  # 让 stderr handler 绑到 capsys 的流
    audit("probe_event", answer=42)

    lines = _read_lines(tmp_path / "logs" / "audit.jsonl")
    assert len(lines) == 1
    event = lines[0]
    # 公共字段一个不少，且 ts/level 在 JSON 体内（不在行首前缀里）
    assert event["event"] == "probe_event"
    assert event["answer"] == 42
    assert event["level"] == "INFO"
    assert event["thread_id"] == audit_mod.NO_THREAD
    assert event["ts"].startswith("20")
    assert " INFO " in capsys.readouterr().err


def test_handler_creation_creates_no_file_until_first_event(tmp_path):
    """导入/配置不产生副作用：只建 handler 不建文件，第一次真写日志才落盘。"""
    audit_mod.configure_audit_log()
    target = tmp_path / "logs" / "audit.jsonl"
    assert not target.exists()
    audit("probe_event")
    assert target.exists()


def test_rotation_creates_backup(monkeypatch, tmp_path):
    """超上限轮转出 `.1`（按大小轮转，文件名不带日期——日期会和轮转打架）。"""
    monkeypatch.setattr(config, "AUDIT_LOG_MAX_BYTES", 300)
    audit_mod.configure_audit_log()
    for i in range(30):
        audit("probe_event", pad="x" * 100, seq=i)

    assert (tmp_path / "logs" / "audit.jsonl.1").exists()
    assert _read_lines(tmp_path / "logs" / "audit.jsonl.1")


def test_reconfigure_appends_instead_of_truncating(monkeypatch, tmp_path):
    """重建 handler（近似"进程重启"）后旧行仍在：日志是追加，不是覆盖。"""
    audit("probe_event", seq=1)
    audit_mod.configure_audit_log()
    audit("probe_event", seq=2)

    seqs = [e["seq"] for e in _read_lines(tmp_path / "logs" / "audit.jsonl")]
    assert seqs == [1, 2]


def test_unwritable_dir_degrades_without_raising(monkeypatch, tmp_path, capsys):
    """目录建不出来时：不抛异常、不重复告警，事件照旧走 stderr。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("占位：它不是目录，所以下面那层建不出来", encoding="utf-8")
    monkeypatch.setattr(config, "AUDIT_LOG_DIR", blocker / "logs")
    audit_mod.configure_audit_log()

    audit("probe_event", seq=1)
    audit("probe_event", seq=2)  # 第二次不该再告警（标记过一次就不再试）

    err = capsys.readouterr().err
    assert err.count("[audit] 审计日志落盘不可用") == 1
    assert err.count('"event": "probe_event"') == 2


# ---------- 0-2 会话标识 ----------

def _run_in_graph(thread_id: str, *, version: str = "v3"):
    """在真实 langgraph 执行里跑一次埋点：v3 流式走 ThreadPoolExecutor，与 Web 同一条路。"""
    from langgraph.graph import END, START, StateGraph

    def node(state):
        audit("graph_probe")
        return {}

    graph = StateGraph(dict)
    graph.add_node("n", node)
    graph.add_edge(START, "n")
    graph.add_edge("n", END)
    compiled = graph.compile()
    if version == "v3":
        stream = compiled.stream_events({}, {"configurable": {"thread_id": thread_id}}, version="v3")
        _ = stream.output
        return
    compiled.invoke({}, {"configurable": {"thread_id": thread_id}})


@pytest.mark.parametrize("version", ["v3", "invoke"])
def test_thread_id_is_attributed_inside_graph(audit_events, version):
    """图内事件必带会话标识，且 `step` 一并拿到——这是整条时间线能串起来的前提。"""
    _run_in_graph(TID, version=version)

    event = next(e for e in audit_events if e["event"] == "graph_probe")
    assert event["thread_id"] == TID
    assert isinstance(event["step"], int)


def test_real_graph_stack_attributes_thread_id(monkeypatch, audit_events):
    """本项目的真实接线也要成立：节点 → 管道 → _log_call → audit 全程带对会话标识。

    上面那个最小图证明的是 langgraph 的传播性质，这一条证明接线没错——节点里发出的
    llm_call 事件确实落到发起它的那个 thread_id 上（走 Web 同款的 v3 流式路径）。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from src.adapters.llm import _log_call

    def fake_qa_pipeline(question, *, tech=None, history=None, progress=None):
        _log_call("qa.answer", 1, 0.5, status="ok", finish_reason="stop")
        return {"answer": "缓存穿透是查不到的数据反复打到库上。", "sources": [], "no_hit": False}

    monkeypatch.setattr(graph_mod, "qa_pipeline", fake_qa_pipeline)
    graph = graph_mod.build_graph(InMemorySaver())

    stream = graph.stream_events({"command": "qa", "args": ["Redis 缓存穿透"]},
                                 {"configurable": {"thread_id": TID}}, version="v3")
    _ = stream.output

    event = next(e for e in audit_events if e["event"] == "llm_call")
    assert event["site"] == "qa.answer"
    assert event["thread_id"] == TID
    assert isinstance(event["step"], int)


def test_thread_id_is_local_outside_graph(audit_events):
    """非图上下文记 `local` 而不是 null：null 会把"归属机制坏了"和"本来就没有会话"混成一种现象。"""
    audit("probe_event")

    assert audit_events[-1]["thread_id"] == audit_mod.NO_THREAD


def test_audit_context_overrides_thread_id(audit_events):
    """显式声明的会话标识优先于 contextvar 兜底（后台线程用）。"""
    with audit_context("sweep-tid"):
        audit("probe_event")

    assert audit_events[-1]["thread_id"] == "sweep-tid"


def test_background_sweep_thread_events_are_attributed(monkeypatch, audit_events):
    """回归：后台沉淀线程里产生的事件必须归属到发起它的会话。

    这条线程是节点里新起的普通线程，`threading.Thread` 不复制 contextvars，
    langgraph 的 config 在它里面读不到——不显式声明的话，这条链上的 LLM 调用与副作用
    事件会全部退化成无归属，而它们恰是最需要归属的一类。
    """
    def fake_sweep(tech, buffer, progress=None):
        audit("llm_call", site="dedup.judge", status="ok")  # 真实链路里确实会发 LLM 调用
        return {"action": "skip"}

    monkeypatch.setattr(graph_mod, "run_memory_sweep", fake_sweep)
    graph_mod._sweep_results.pop(TID, None)
    graph_mod._start_sweep_thread("redis", [], TID)

    deadline = time.monotonic() + 5
    while TID not in graph_mod._sweep_results and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        event = next(e for e in audit_events if e["event"] == "llm_call")
        assert event["thread_id"] == TID, "后台线程产生的事件丢了会话归属"
    finally:
        graph_mod._sweep_results.pop(TID, None)


def test_public_fields_win_over_caller_fields(audit_events):
    """公共字段由 audit() 注入且优先：调用方传同名字段也不能破坏"每条事件带会话标识"这条不变式。"""
    audit("probe_event", ts="伪造", thread_id="伪造")

    event = audit_events[-1]
    assert event["event"] == "probe_event"
    assert event["ts"] != "伪造"
    assert event["thread_id"] == audit_mod.NO_THREAD


# ---------- 0-3 启动快照 ----------

def test_boot_snapshot_records_config_without_secrets(audit_events, monkeypatch):
    """boot 给之后每一行日志提供"当时的模型与阈值"背景；且绝不记 URL 里的 key。"""
    monkeypatch.setattr(config, "OPENAI_BASE_URL",
                        "https://gw.example.com/v1?api_key=sk-should-never-appear")
    monkeypatch.setattr(config, "LLM_MODEL", "probe-model")
    boot("cli")

    event = next(e for e in audit_events if e["event"] == "boot")
    assert event["entry"] == "cli"
    assert event["model"] == "probe-model"
    assert event["base_url_host"] == "gw.example.com"  # 只到 host，路径与 query 一律不留
    assert len(event["cfg_hash"]) == 12
    assert event["python"] and event["version"]
    assert event["config"]["LLM_MODEL"] == "probe-model"
    assert "sk-should-never-appear" not in json.dumps(event, ensure_ascii=False)
    assert "OPENAI_API_KEY" not in json.dumps(event, ensure_ascii=False)


def test_web_startup_records_boot(monkeypatch, audit_events, tmp_path):
    """`python -m src.web.server` 的启动路径也要留一条快照（Web 是最容易"没人看着终端"的那个）。"""
    import uvicorn

    from src.web import server as server_mod

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)  # 不起真服务
    server_mod.main()

    event = next(e for e in audit_events if e["event"] == "boot")
    assert event["entry"] == "web"


def test_boot_hash_changes_when_threshold_changes(audit_events, monkeypatch):
    """阈值动过就该看得出来——cfg_hash 是"配置变了吗"的判据。"""
    boot("cli")
    monkeypatch.setattr(config, "ROUTE_KB_INJECT_SIM", 0.9)
    boot("cli")

    hashes = [e["cfg_hash"] for e in audit_events if e["event"] == "boot"]
    assert len(hashes) == 2 and hashes[0] != hashes[1]


# ---------- 0-4 测试隔离 ----------

def test_conftest_redirects_audit_dir(tmp_path):
    """autouse 夹具必须把日志目录钉在 tmp_path：否则跑测试会往真实审计日志灌假事件。"""
    audit_dir = config.AUDIT_LOG_DIR  # 全大写属性会被 SIM300 当成常量，先取到变量再比
    assert audit_dir == tmp_path / "logs"
