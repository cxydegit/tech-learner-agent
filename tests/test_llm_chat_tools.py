"""adapters/llm.chat_with_tools 单测（零网络）：工具调用解析 / 错误分档 / 退避重试 / 降级。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_llm_chat_tools.py -v
"""

import sys
from pathlib import Path

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters import llm as llm_mod
from src.adapters.llm import ToolCallError, chat_with_tools
from src.config import config

_REQ = httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _status_error(code: int) -> APIStatusError:
    """构造带状态码的 SDK 异常（真实异常需要 response，故造一个最小 httpx.Response）。"""
    return APIStatusError("boom", response=httpx.Response(code, request=_REQ), body=None)


def _conn_error() -> APIConnectionError:
    return APIConnectionError(request=_REQ)


class _FakeTime:
    """替身 time 模块：monotonic 由 sleep / advance 推进，重试预算可确定性驱动。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def fake_time(monkeypatch):
    """替换 llm 模块内的 time：退避不再真睡，测试快且可断言（不碰标准库 time）。"""
    fake = _FakeTime()
    monkeypatch.setattr(llm_mod, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def fresh_client(monkeypatch):
    """重置客户端单例：本用例 patch 的 OpenAI 不该泄漏给下一个用例。"""
    monkeypatch.setattr(llm_mod, "_client", None)


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
    """前 fail_times 次调用抛 exc（默认 500，即瞬时错误），之后返回固定消息。"""

    def __init__(self, fail_times, response, exc=None):
        self.fail_times = fail_times
        self.response = response
        self.exc = exc or _status_error(500)
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
            raise self.exc
        return type("Resp", (), {"choices": [type("Choice", (), {"message": self.response})()]})()


class _SlowFailClient:
    """每次调用都抛瞬时错误并消耗 cost 秒，用来驱动「重试预算耗尽」场景。"""

    def __init__(self, fake_time, cost, exc=None):
        self.time = fake_time
        self.cost = cost
        self.exc = exc or _status_error(503)
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self.time.advance(self.cost)
        raise self.exc


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
    assert out["fallback"] is False  # 正常路径显式标记未降级
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


def test_request_timeout_is_explicit(monkeypatch):
    """每次请求都显式带 timeout：SDK 默认 600s 会把交互式 coach 循环挂死。"""
    client = _FakeClient([_msg("ok")])
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    chat_with_tools("s", [], _TOOLS)
    assert client.calls[0]["timeout"] <= config.LLM_REQUEST_TIMEOUT


# ---------- 瞬时错误：退避重试 ----------

def test_transient_error_retries_with_backoff(monkeypatch, fake_time):
    """瞬时错误退避后重试成功：退避递增（不是立即重试），且恢复后不算降级。"""
    client = _FlakyClient(fail_times=2, response=_msg("恢复"), exc=_conn_error())
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("s", [], _TOOLS)
    assert out["content"] == "恢复"
    assert out["fallback"] is False
    assert len(client.calls) == 3
    assert "tools" in client.calls[0]           # 重试仍带工具，不是降级
    assert len(fake_time.slept) == 2
    assert fake_time.slept[0] >= config.LLM_RETRY_BASE_DELAY
    assert fake_time.slept[1] >= 2 * config.LLM_RETRY_BASE_DELAY  # 指数递增
    assert fake_time.slept[1] > fake_time.slept[0]


def test_retries_then_text_fallback(monkeypatch):
    client = _FlakyClient(fail_times=3, response=_msg("降级回答"))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("s", [], _TOOLS)
    assert out["content"] == "降级回答"
    assert out.get("fallback") is True
    # 前 3 次带 tools，回退那次不带
    assert all("tools" in c for c in client.calls[:3])
    assert "tools" not in client.calls[3]


def test_retry_budget_cuts_retries_short(monkeypatch, fake_time):
    """单次调用就吃掉大半预算时提前停止重试，不让用户干等到次数用满。"""
    client = _SlowFailClient(fake_time, cost=50)
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    with pytest.raises(ToolCallError):
        chat_with_tools("s", [], _TOOLS)
    # 预算是 90s、每次耗时 50s：第 2 次尝试后退避会超预算 → 直接进降级（共 3 次调用）；
    # 没有预算闸门的话会是 3 次带工具 + 1 次降级 = 4 次
    assert len(client.calls) == 3


def test_raises_tool_call_error_when_fallback_disabled(monkeypatch):
    client = _FlakyClient(fail_times=100, response=_msg("x"))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    monkeypatch.setattr(config, "ROUTE_FALLBACK_TO_TEXT", False)
    try:
        chat_with_tools("s", [], _TOOLS)
        raise AssertionError("应当抛 ToolCallError")
    except ToolCallError:
        pass


# ---------- 确定性错误：不重试（必要时降级） ----------

def test_auth_error_fails_fast(monkeypatch):
    """401 重试一万次也一样：一次调用就判死，标记 fatal，且不浪费降级调用。"""
    client = _FlakyClient(fail_times=99, response=_msg("x"), exc=_status_error(401))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    with pytest.raises(ToolCallError) as ei:
        chat_with_tools("s", [], _TOOLS)
    assert ei.value.fatal is True
    assert "401" in str(ei.value)
    assert len(client.calls) == 1  # 不重试、不降级


def test_bad_request_skips_retry_but_degrades(monkeypatch):
    """400 很可能是 tools 定义不被该模型支持：重试同样的 payload 无意义，降级值得一试。"""
    client = _FlakyClient(fail_times=1, response=_msg("纯文本答复"), exc=_status_error(400))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    out = chat_with_tools("sys", [], _TOOLS)
    assert out["fallback"] is True
    assert out["content"] == "纯文本答复"
    assert len(client.calls) == 2      # 一次带 tools + 一次降级，没有重复重试
    assert "tools" in client.calls[0]
    assert "tools" not in client.calls[1]


def test_bad_request_on_both_channels_is_fatal(monkeypatch):
    """带工具与降级两次都是 4xx：原因不在 tools，判 fatal，文案带上具体原因。"""
    client = _FlakyClient(fail_times=99, response=_msg("x"), exc=_status_error(400))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    with pytest.raises(ToolCallError) as ei:
        chat_with_tools("s", [], _TOOLS)
    assert ei.value.fatal is True
    assert "HTTP 400" in str(ei.value) and "请求被拒绝" in str(ei.value)
    assert len(client.calls) == 2  # 一次带 tools + 一次降级，未重复重试


def test_timeout_status_after_degrade_is_not_fatal(monkeypatch):
    """降级后仍是瞬时错误（503）：不该误判成确定性错误——那是网络问题，值得稍后再试。"""
    calls = []

    class _Client:
        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            calls.append(kwargs)
            raise _status_error(503)

    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: _Client())
    with pytest.raises(ToolCallError) as ei:
        chat_with_tools("s", [], _TOOLS)
    assert ei.value.fatal is False


def test_fallback_prompt_declares_tools_unavailable(monkeypatch):
    """降级请求必须告知模型「工具没了」：否则它会输出叙述式假成功。

    瞬时错误要耗满重试次数才会降级（fail_times 对齐 LLM_MAX_ATTEMPTS）。
    """
    client = _FlakyClient(fail_times=config.LLM_MAX_ATTEMPTS,
                          response=_msg("抱歉，本次无法执行"), exc=_conn_error())
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    chat_with_tools("SYSTEM", [{"role": "user", "content": "帮我生成路线"}], _TOOLS)
    degraded = client.calls[config.LLM_MAX_ATTEMPTS]  # 第 N+1 次 = 降级那次
    assert "tools" not in degraded
    degraded_system = degraded["messages"][0]
    assert degraded_system["role"] == "system"
    assert degraded_system["content"].startswith("SYSTEM")  # 原提示词不丢
    assert "工具通道不可用" in degraded_system["content"]
    # 历史消息原样保留
    assert degraded["messages"][1]["content"] == "帮我生成路线"
    # 非降级那次不带这段声明
    assert "工具通道不可用" not in client.calls[0]["messages"][0]["content"]


def test_unexpected_exception_propagates(monkeypatch):
    """非 SDK 异常 = 代码 bug：不重试、不包装成「模型暂时不可用」，原样抛出显形。"""
    client = _FlakyClient(fail_times=99, response=_msg("x"), exc=TypeError("响应结构变了"))
    monkeypatch.setattr(llm_mod, "OpenAI", lambda **kw: client)
    with pytest.raises(TypeError):
        chat_with_tools("s", [], _TOOLS)
    assert len(client.calls) == 1
