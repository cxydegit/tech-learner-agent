"""domain/exit_intent 纯规则单测：退出意图 + 推进授权指令判定。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_exit_intent.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.exit_intent import is_advance_directive, is_completion_claim, is_exit_intent


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


# ---------- is_advance_directive（里程碑推进授权） ----------

def test_advance_directive_phrases():
    """明确推进/豁免指令 → True（勾选里程碑后免确认直接推进）。"""
    for t in ["直接进入下一阶段", "直接开始下一个里程碑", "直接推进", "直接下一步",
              "直接进入下一阶段吧", "直接学下一阶段", "不用再问了", "不用确认了",
              "别问了，直接进入下一阶段", "直接进行下一里程碑"]:
        assert is_advance_directive(t), t


def test_not_advance_directive():
    """普通讨论 / 反向表达 → False（仍走确认询问）。"""
    assert not is_advance_directive("进入下一阶段之前，先把当前阶段讲完")
    assert not is_advance_directive("好的")
    assert not is_advance_directive("")
    assert not is_advance_directive("先别推进，等我把这里的疑问搞清楚了再进入下一阶段")


def test_advance_directive_accepts_contextual_false_positive():
    """设计接受的低风险误判：技术排障里的「直接进入下一步」也会免确认（少问一次，代价低）。"""
    assert is_advance_directive("这个报错直接进入下一步排查，帮我看看")


# ---------- is_completion_claim（完成声明豁免验收） ----------

def test_completion_claim_phrases():
    """明确声明完成 / 要求直接勾选 → True（对话外完成的里程碑豁免验收）。"""
    for t in ["都搞定了", "我学会了", "本地跑通了", "装好了", "都做完了",
              "直接勾吧", "勾了吧", "我在本地都搞定了，直接勾吧"]:
        assert is_completion_claim(t), t


def test_not_completion_claim():
    """否定 / 疑问 / 普通讨论 → False（仍走验收）。"""
    for t in ["还没搞定", "还没做完", "先别勾", "搞定了没？", "完成了吗", "这个怎么搞定？", ""]:
        assert not is_completion_claim(t), t
