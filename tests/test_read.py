"""pipelines/read 的接线单测（零网络）：抓取必须带显式超时 + 关掉 SDK 重试。

背景：read 是单页抓取、直接阻塞在工具路径上，没有 `fetch_many` 那样的共享墙钟兜底。
不给超时时 Firecrawl 客户端是 `timeout=None`（不设 HTTP 超时），叠加 SDK 默认的
`max_retries=3`，最坏可以长时间挂住——与"裸客户端无超时"那类事故同源。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_read.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.pipelines.read as read_mod
from src.config import config


def _fetch_ok(url, **kw):
    return {"url": url, "markdown": "# 正文", "title": "T", "truncated": False}


def test_read_fetch_passes_timeout_and_no_retry(monkeypatch):
    """抓取调用必须带 timeout 与 max_retries=0：否则最坏 = SDK 3 次重试 × 每次无超时。"""
    captured = {}

    def fake_fetch(url, **kw):
        captured.update(kw)
        return _fetch_ok(url)

    monkeypatch.setattr(read_mod, "fetch_tool", fake_fetch)
    monkeypatch.setattr(read_mod, "_classify_technical", lambda *a: (True, ""))
    monkeypatch.setattr(read_mod, "generate_text", lambda s, u, **kw: "# 报告")
    monkeypatch.setattr(read_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})
    monkeypatch.setattr(read_mod, "index_file_lazy", lambda path: {"index_ok": True})

    read_mod.read_pipeline("https://example.com/doc")

    assert captured["timeout"] == config.FETCH_TIMEOUT_SECONDS
    assert captured["max_retries"] == 0


def test_read_fetch_failure_propagates(monkeypatch):
    """抓取抛错（超时/网络）→ 传出 read_pipeline，由调用方按错误处理（不吞成空报告）。"""
    def boom(url, **kw):
        raise TimeoutError("scrape timed out")

    monkeypatch.setattr(read_mod, "fetch_tool", boom)
    try:
        read_mod.read_pipeline("https://example.com/doc")
        raise AssertionError("抓取失败应当抛出")
    except TimeoutError:
        pass


def test_read_report_wires_heartbeat_progress(monkeypatch):
    """解读报告生成同样要挂心跳（与 collect 对齐）。"""
    captured = {}

    def fake_generate(system, user, **kw):
        captured.update(kw)
        return "# 报告"

    monkeypatch.setattr(read_mod, "fetch_tool", _fetch_ok)
    monkeypatch.setattr(read_mod, "_classify_technical", lambda *a: (True, ""))
    monkeypatch.setattr(read_mod, "generate_text", fake_generate)
    monkeypatch.setattr(read_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})
    monkeypatch.setattr(read_mod, "index_file_lazy", lambda path: {"index_ok": True})

    seen: list[str] = []
    read_mod.read_pipeline("https://example.com/doc", progress=seen.append)

    assert captured["progress"] == seen.append
    assert captured["progress_label"] == "LLM 生成解读报告"
