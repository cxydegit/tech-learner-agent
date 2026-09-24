"""pytest 全局夹具。

审计日志落盘后必须有**统一出口**把日志目录指到 tmp_path：项目原本没有 conftest，
隔离靠各测试自己 monkeypatch，而大多数测试根本不关心日志——跑一次测试就会往真实的
`logs/audit.jsonl` 里灌进假事件（现成的例子：`test_unexpected_exception_propagates`、
`test_generate_text_single_attempt_no_retry`，两者都没用 `log_capture` 夹具却会走到
`_log_call`）。假事件混进真实审计日志，正好毁掉这套日志的可信度。
"""

import json
import logging

import pytest

from src.adapters import audit as audit_mod
from src.config import config


@pytest.fixture(autouse=True)
def _audit_log_to_tmp(tmp_path, monkeypatch):
    """把审计日志重定向到本用例的 tmp_path，跑完还原。"""
    monkeypatch.setattr(config, "AUDIT_LOG_DIR", tmp_path / "logs")
    audit_mod.configure_audit_log()
    yield
    audit_mod.configure_audit_log()


@pytest.fixture
def audit_events(monkeypatch):
    """捕获审计事件（解析每行 JSON），替代 stderr / 落盘。

    与 test_llm_chat_tools 的 log_capture 同类，但可跨模块复用：断言事件字段时用它，
    不需要真的读文件。
    """
    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.events: list[dict] = []

        def emit(self, record):
            self.events.append(json.loads(record.getMessage()))

    cap = _Capture()
    monkeypatch.setattr(audit_mod.LOGGER, "handlers", [cap])
    return cap.events
