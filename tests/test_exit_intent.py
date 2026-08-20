"""domain/exit_intent 纯规则单测：短文本退出意图判定，反向长句不误判。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_exit_intent.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.exit_intent import is_exit_intent


def test_short_exit_phrases():
    for t in ["结束", "退出", "停", "不学了", "今天就到这", "就到这里吧", "收工", "太乱了", "不搞了"]:
        assert is_exit_intent(t), t


def test_long_sentence_with_keyword_not_exit():
    assert not is_exit_intent("我还没结束学习呢")
    assert not is_exit_intent("我想今天就结束这个技术的学习，太乱了")
    assert not is_exit_intent("这个知识点还没学完")


def test_normal_content_not_exit():
    assert not is_exit_intent("Java Maven")
    assert not is_exit_intent("5")
    assert not is_exit_intent("")
