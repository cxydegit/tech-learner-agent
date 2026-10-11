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
              "直接勾吧", "勾了吧", "我在本地都搞定了，直接勾吧",
              # 认知型待办（「能说清 XX」）的完成只在用户脑子里，自述是唯一证据形态
              "我会了", "这块我掌握了", "我脑子里过了一遍，就不一一回答你了"]:
        assert is_completion_claim(t), t


def test_not_completion_claim():
    """否定 / 疑问 / 普通讨论 → False（仍走验收）。"""
    for t in ["还没搞定", "还没做完", "先别勾", "搞定了没？", "完成了吗", "这个怎么搞定？", ""]:
        assert not is_completion_claim(t), t
    # 新增的自评/免答词同样要过否定守卫：否定式不能因为含关键词就豁免验收
    assert not is_completion_claim("我脑子里还没过一遍")


def test_completion_claim_knowledge_class():
    """认知类自述（「我知道」这类表态）→ True：用户本人的掌握声明，豁免验收。

    这类措辞原先不在词表里，会掉进验收员；现已收进快路径（省一次 LLM 调用）。
    """
    for t in ["这个我知道", "我知道了", "我懂了", "我明白了", "这块我清楚了",
              "我了解了", "我理解了", "搞清楚了", "知道了"]:
        assert is_completion_claim(t), t


def test_knowledge_class_negation_guard():
    """认知类的否定 / 疑问式不得豁免：「不知道」「我不明白了」「我知道了没」仍走验收。"""
    for t in ["我不知道", "我不懂", "我不清楚", "这个我不了解", "我还没明白",
              "我不明白了", "我知道了没"]:
        assert not is_completion_claim(t), t


def test_knowledge_class_question_not_claim():
    """提问里的「知道」不是掌握声明——词表只收第一人称/完成态，不收光秃秃的动词。

    这条守的是误豁免方向（判 True 会跳过验收直接勾选），所以宁可漏判。
    """
    for t in ["我想知道这个为什么", "你知道为什么吗", "这个谁知道怎么弄"]:
        assert not is_completion_claim(t), t


def test_advance_only_is_not_completion_claim():
    """只说「往下走」不构成完成声明：打不打得勾交给验收员按证据判。"""
    assert is_advance_directive("直接下一步")
    assert not is_completion_claim("直接下一步")
    assert not is_completion_claim("直接进入下一阶段")
