"""domain/dedup 纯函数单测（零网络）。

锁 sanitize_filename / _topics_overlap / _with_header 的行为，防止回归。
运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_dedup.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.dedup import _topics_overlap, _with_header, sanitize_filename


# ---------- sanitize_filename ----------

def test_sanitize_filename_basic():
    assert sanitize_filename("Spring Boot") == "spring-boot"
    assert sanitize_filename("FastAPI") == "fastapi"


def test_sanitize_filename_chinese_and_symbols():
    assert sanitize_filename("依赖 注入!@#") == "依赖-注入"


def test_sanitize_filename_strips_edges():
    assert sanitize_filename("--rust--") == "rust"


# ---------- _topics_overlap ----------

def test_overlap_equal():
    assert _topics_overlap("依赖注入", "依赖注入")


def test_overlap_substring():
    assert _topics_overlap("依赖注入", "依赖注入的原理")


def test_overlap_case_insensitive():
    assert _topics_overlap("HTTP 缓存", "http 缓存")


def test_overlap_distinct():
    assert not _topics_overlap("HTTP 缓存", "数据库事务")


def test_overlap_empty():
    assert not _topics_overlap("", "依赖注入")
    assert not _topics_overlap("依赖注入", "")


# ---------- _with_header ----------

def test_with_header():
    header = _with_header("依赖注入", ["spring", "di"], "正文内容")
    assert header.startswith("# 依赖注入")
    assert "#spring" in header and "#di" in header
    assert header.endswith("正文内容")
    assert "> 日期：" in header


def test_with_header_no_tags():
    header = _with_header("依赖注入", None, "正文")
    assert "> 标签：" in header
    assert header.endswith("正文")
