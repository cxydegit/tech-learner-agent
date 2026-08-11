"""domain/card_input 卡片级校验纯函数单测（零网络）。

覆盖：三卡片（collect/read/ask）合法/非法输入、collect 的 focus 自由文本、
ask 映射到图命令 qa、缺必填项的提示文案。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_card_input.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.card_input import parse_card_input


# ---------- collect ----------

def test_collect_tech_only():
    """只给技术名 → 无 focus（走固定模板）。"""
    assert parse_card_input("collect", ["FastAPI"]) == {"command": "collect", "tech": "FastAPI"}


def test_collect_tech_focus():
    """技术名 + 自由文本 focus → focus 字段（用户提示词，走非固定模板）。"""
    assert parse_card_input("collect", ["FastAPI", "异步编程"]) == {
        "command": "collect", "tech": "FastAPI", "focus": "异步编程"}


def test_collect_tech_multiword_focus():
    """多个 token 拼成一个 focus。"""
    assert parse_card_input("collect", ["FastAPI", "异步", "性能调优"]) == {
        "command": "collect", "tech": "FastAPI", "focus": "异步 性能调优"}


def test_collect_blank_focus():
    """focus 全空白 → 视为无 focus。"""
    assert parse_card_input("collect", ["FastAPI", "  "]) == {"command": "collect", "tech": "FastAPI"}


def test_collect_missing_tech():
    assert parse_card_input("collect", []) == {"error": "请输入技术名"}
    assert parse_card_input("collect", ["   "]) == {"error": "请输入技术名"}
    # standalone click 未传 tech 时参数是 None（容错）
    assert parse_card_input("collect", [None]) == {"error": "请输入技术名"}


# ---------- read / ask ----------

def test_read_url():
    assert parse_card_input("read", ["https://example.com"]) == {
        "command": "read", "args": ["https://example.com"]}


def test_read_missing_url():
    assert parse_card_input("read", []) == {"error": "请输入链接"}


def test_ask_question():
    """ask 卡片 → 图命令 qa；整个问题作为一个 token 传入（REPL /ask 用法）。"""
    assert parse_card_input("ask", ["闭包 和 作用域"]) == {
        "command": "qa", "args": ["闭包 和 作用域"]}


def test_ask_multi_token_question():
    """多 token 也拼成一句问题（兼容 Web 表单按词拆传）。"""
    assert parse_card_input("ask", ["闭包", "和", "作用域"]) == {
        "command": "qa", "args": ["闭包 和 作用域"]}


def test_ask_missing_question():
    assert parse_card_input("ask", []) == {"error": "请输入问题"}
    assert parse_card_input("ask", ["   "]) == {"error": "请输入问题"}
