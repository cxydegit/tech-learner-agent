"""origin / run_id / AUDIT_LOG_NAME 的落点与优先级测试。

不依赖 langgraph / chromadb，可在精简环境单独跑：
    python -m pytest tests/test_audit_origin.py -q
"""

import json

from src.adapters import audit as audit_mod
from src.adapters.audit import audit, audit_context, boot
from src.config import config


def test_origin_and_run_id_from_test_env(audit_events):
    audit("probe")
    event = audit_events[-1]
    assert event["origin"] == "test"
    assert event["run_id"] is None


def test_context_overrides_env(audit_events):
    with audit_context("tid", origin="eval", run_id="run-1"):
        audit("probe")
    event = audit_events[-1]
    assert event["thread_id"] == "tid"
    assert event["origin"] == "eval"
    assert event["run_id"] == "run-1"


def test_explicit_args_override_context(audit_events):
    with audit_context("tid", origin="eval", run_id="run-1"):
        audit("probe", origin="product", run_id="run-2")
    event = audit_events[-1]
    assert event["origin"] == "product"
    assert event["run_id"] == "run-2"


def test_invalid_origin_degrades_to_unknown(audit_events):
    audit("probe", origin="not-a-cohort")
    assert audit_events[-1]["origin"] == "unknown"


def test_boot_carries_origin(audit_events):
    boot("cli")
    event = audit_events[-1]
    assert event["event"] == "boot"
    assert event["origin"] == "test"
    assert "git_sha" in event


def test_turn_from_mapping(audit_events):
    from src.adapters.audit import set_turn

    set_turn("turn-tid", 3)
    with audit_context("turn-tid"):
        audit("probe")
    assert audit_events[-1]["turn"] == 3


def test_explicit_turn_overrides_mapping(audit_events):
    from src.adapters.audit import set_turn

    set_turn("turn-tid-2", 3)
    with audit_context("turn-tid-2"):
        audit("probe", turn=4)
    assert audit_events[-1]["turn"] == 4


def test_turn_is_null_without_mapping(audit_events):
    with audit_context("turn-tid-3"):
        audit("probe")
    assert audit_events[-1]["turn"] is None


def test_audit_log_name_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "AUDIT_LOG_NAME", "probe.jsonl")
    audit_mod.configure_audit_log()

    audit("probe", run_id="run-1")

    path = tmp_path / "probe.jsonl"
    assert path.exists()
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event"] == "probe"
    assert event["origin"] == "test"
    assert event["run_id"] == "run-1"
