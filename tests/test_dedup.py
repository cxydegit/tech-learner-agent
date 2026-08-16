"""domain/dedup 纯函数单测（零网络）。

锁 sanitize_filename / _topics_overlap / _with_header 的行为，防止回归。
运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_dedup.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.dedup import (
    _parse_tags,
    _title_fast_match,
    _topics_overlap,
    _with_header,
    sanitize_filename,
    strip_note_header,
)


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


# ---------- strip_note_header ----------

def test_strip_note_header_removes_front_matter():
    content = _with_header("依赖注入", ["spring", "di"], "## 是什么\n正文内容")
    assert content.startswith("# 依赖注入")
    assert strip_note_header(content) == "## 是什么\n正文内容"


def test_strip_note_header_without_title():
    body = strip_note_header("\n> 日期：2026-08-09\n> 标签：#x\n\n正文")
    assert body == "正文"


def test_strip_note_header_plain_body():
    assert strip_note_header("## 是什么\n正文") == "## 是什么\n正文"


# ---------- 去重标题 fast-path（RAG_OPTIMIZATION P0 压力测试后重构） ----------

def test_parse_tags_from_header():
    assert _parse_tags("# 主题\n\n> 日期：2026-08-09\n> 标签：#Redis #数据结构 #选型\n\n正文") == [
        "Redis", "数据结构", "选型"]
    assert _parse_tags("# 主题\n\n> 日期：2026-08-09\n\n正文") == []


def test_title_fast_match_equal():
    assert _title_fast_match("Redis 持久化", "Redis 持久化")


def test_title_fast_match_meta_suffix_synonym():
    """meta 后缀词（机制/原理/对比/区别）不参与：同一句子的不同说法 → 命中。"""
    assert _title_fast_match("Redis 持久化机制", "Redis 持久化原理")
    assert _title_fast_match("RDB 与 AOF 对比", "RDB 与 AOF 区别")


def test_title_fast_match_number_words():
    """数词/量词不参与：「五种核心角色」vs「五大核心角色」仍是同一知识点 → 命中。"""
    assert _title_fast_match("Redis 五种核心角色", "Redis 五大核心角色")


def test_title_fast_match_no_false_merge():
    """错合并实证（合成压力测试）：只共享几个字不算同一句 → 不命中。

    旧 `_topics_overlap` 单字切分 + 0.4 阈值让「NoSQL内存数据库五种核心角色」
    误撞「redis 的五大核心角色」；fast-path 双字词切分 + 停数词后集合不相等。
    """
    assert not _title_fast_match("NoSQL内存数据库五种核心角色", "redis 的五大核心角色")
    assert not _title_fast_match("Redis 缓存", "Redis 缓存雪崩")  # 子主题不命中，交 LLM
    assert not _title_fast_match("Redis 缓存穿透", "Redis 缓存雪崩")  # 同域不同知识点
    assert not _title_fast_match("Redis 持久化", "Redis 数据结构")


def test_title_fast_match_case_insensitive():
    assert _title_fast_match("Redis 持久化", "redis 持久化")


def test_title_fast_match_empty():
    """空标题 / 全部停用词（无词元）不匹配。"""
    assert not _title_fast_match("", "Redis 持久化")
    assert not _title_fast_match("Redis 持久化", "")
    assert not _title_fast_match("的 了 与 机制", "机制 原理")
