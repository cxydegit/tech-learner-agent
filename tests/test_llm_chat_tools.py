"""adapters/llm.chat_with_tools 单测（零网络）：工具调用解析 / 重试 / 降级回退 / 抛错。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_llm_chat_tools.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters import llm as llm_mod
from src.adapters.llm import ToolCallError, chat_with_tools
from src.config import config


def _msg(content=None, tool_calls=None):
    return type("Msg", (), {"content": content, "tool_calls": tool_calls})()


def _tc(call_id, name, arguments):
    return type("TC", (), {"id": call_id,
                           "function": type("F", (), {"name": name, "arguments": arguments})()})()


class _FakeClient:
    """按序弹出预置消息；记录每次 create 的 kwargs。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = self.responses.pop(0)
        return type("Resp", (), {"choices": [type("Choice", (), {"message": msg})()]})()


class _FlakyClient:
    """前 fail_times 次调用抛错，之后返回固定消息。"""

    def __init__(self, fail_times, response):
        self.fail_times = fail_times
        self.response = response
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("server 500")
        return type("Resp", (), {"choices": [type("Choice", (), {"message": self.response})()]})()


_TOOLS = [{"type": "function", "function": {"name": "generate_roadmap", "parameters": {"type": "object"}}}]


def test_returns_tool_calls(monkeypatch):
    client = _FakeClient([_msg(None, [_tc("call_1", "generate_roadmap", '{"goal":"g"}')])])
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("sys", [{"role": "user", "content": "hi"}], _TOOLS)
    assert out["content"] is None
    assert len(out["tool_calls"]) == 1
    assert out["tool_calls"][0]["id"] == "call_1"
    assert out["tool_calls"][0]["name"] == "generate_roadmap"
    assert out["tool_calls"][0]["arguments"] == {"goal": "g"}
    # 工具定义传给了 API
    assert "tools" in client.calls[0]
    # 系统提示词在最前
    assert client.calls[0]["messages"][0]["role"] == "system"


def test_plain_text_reply(monkeypatch):
    client = _FakeClient([_msg("你好")])
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("sys", [], _TOOLS)
    assert out["content"] == "你好"
    assert out["tool_calls"] == []


def test_bad_arguments_json_falls_back_to_empty(monkeypatch):
    client = _FakeClient([_msg(None, [_tc("c1", "x", "not-json")])])
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("s", [], _TOOLS)
    assert out["tool_calls"][0]["arguments"] == {}


def test_retries_then_text_fallback(monkeypatch):
    client = _FlakyClient(fail_times=3, response=_msg("降级回答"))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("s", [], _TOOLS)
    assert out["content"] == "降级回答"
    assert out.get("fallback") is True
    # 前 3 次带 tools，回退那次不带
    assert all("tools" in c for c in client.calls[:3])
    assert "tools" not in client.calls[3]


def test_raises_tool_call_error_when_fallback_disabled(monkeypatch):
    client = _FlakyClient(fail_times=100, response=_msg("x"))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    monkeypatch.setattr(config, "ROUTE_FALLBACK_TO_TEXT", False)
    try:
        chat_with_tools("s", [], _TOOLS)
        raise AssertionError("应当抛 ToolCallError")
    except ToolCallError:
        pass
