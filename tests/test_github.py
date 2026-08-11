"""adapters/github 星数查询单测（mock urlopen，零真实网络）。

覆盖：非 github 链接直接 None（不发起网络）、github 链接解析 owner/repo、
请求异常优雅降级为 None、带路径后缀的仓库链接取到正确的 api 路径。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_github.py -v
"""

import json
import sys
from pathlib import Path
from unittest import mock

# 保证 tests/ 下能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.github import fetch_star_count


class _FakeResp:
    """带 json 载荷的假响应（供 `with urlopen(...) as resp` 使用）。"""
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._data).encode("utf-8")


def test_non_github_returns_none():
    """非 github 链接直接返回 None，不发网络请求。"""
    assert fetch_star_count("https://python.org/tutorial", token="t") is None


def test_github_returns_star_count():
    with mock.patch("src.adapters.github.urllib.request.urlopen",
                    return_value=_FakeResp({"stargazers_count": 15000})):
        assert fetch_star_count("https://github.com/langchain-ai/langgraph", token="t") == 15000


def test_github_failure_degrades():
    """请求异常（网络/404）→ 返回 None（优雅降级）。"""
    with mock.patch("src.adapters.github.urllib.request.urlopen",
                    side_effect=OSError("network down")):
        assert fetch_star_count("https://github.com/a/b", token="t") is None


def test_github_repo_with_path_suffix():
    """带路径/查询后缀的仓库链接，api 请求仍指向 /repos/owner/repo。"""
    captured = {}
    with mock.patch("src.adapters.github.urllib.request.urlopen") as mo:
        mo.return_value = _FakeResp({"stargazers_count": 5})
        fetch_star_count("https://github.com/psf/requests/tree/main", token="t")
        captured["url"] = mo.call_args.args[0].full_url
    assert captured["url"] == "https://api.github.com/repos/psf/requests"
