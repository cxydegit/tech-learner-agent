"""domain/extraction 纯函数单测（零网络）。

锁 parse_entries / parse_classify / extract_json_object / as_list 的行为，
防止在后续重构中回归。运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_extraction.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.extraction import as_list, extract_json_object, parse_classify, parse_entries


# ---------- extract_json_object ----------

def test_extract_json_object_simple():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_brace_in_string():
    # 字符串状态机：`}` 在字符串内不提前截断
    assert extract_json_object('{"msg": "hello } world", "n": 2}') == {"msg": "hello } world", "n": 2}


def test_extract_json_object_rejects_non_object():
    assert extract_json_object('"not an object"') == {}
    assert extract_json_object("") == {}
    assert extract_json_object("junk") == {}


# ---------- parse_classify ----------

def test_parse_classify_strips_code_fence():
    raw = '```json\n{"is_technical": true, "reason": "官方文档"}\n```'
    assert parse_classify(raw) == {"is_technical": True, "reason": "官方文档"}


def test_parse_classify_fallback_to_object_scan():
    raw = '前缀文字 {"is_technical": false, "reason": "营销页"} 后缀'
    assert parse_classify(raw)["is_technical"] is False


def test_parse_classify_garbage_returns_empty():
    assert parse_classify("完全不是 JSON") == {}


# ---------- parse_entries ----------

def test_parse_entries_array():
    raw = '[{"topic": "A", "content": "x"}, {"topic": "B", "content": "y"}]'
    entries = parse_entries(raw)
    assert len(entries) == 2
    assert entries[0]["topic"] == "A"
    assert entries[1]["topic"] == "B"


def test_parse_entries_code_fence():
    assert parse_entries('```json\n[]\n```') == []


def test_parse_entries_fallback_array_in_text():
    raw = '一些文字 [{"topic": "T", "content": "c"}] 结尾'
    entries = parse_entries(raw)
    assert len(entries) == 1
    assert entries[0]["topic"] == "T"


def test_parse_entries_garbage_returns_empty():
    assert parse_entries("完全不是 JSON") == []


# ---------- as_list ----------

def test_as_list_filters_non_dict():
    assert as_list([{"a": 1}, 2, "x"]) == [{"a": 1}]


def test_as_list_non_list_returns_empty():
    assert as_list("not list") == []
    assert as_list(None) == []
