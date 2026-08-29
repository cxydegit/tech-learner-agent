"""domain/survey 纯规则单测：固定字段解析 / 完成判定 / 画像推导。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_survey.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain import survey as sv


def test_self_level_parse():
    assert sv.parse_answer_for_field("self_level", "5") == (5, None)
    assert sv.parse_answer_for_field("self_level", "7/10") == (7, None)
    v, e = sv.parse_answer_for_field("self_level", "大概6分吧")
    assert v == 6 and e is None
    v, e = sv.parse_answer_for_field("self_level", "abc")
    assert v is None and e
    v, e = sv.parse_answer_for_field("self_level", "11")
    assert v is None and e


def test_related_parse():
    assert sv.parse_answer_for_field("related", "Java Maven") == ("Java Maven", None)
    v, e = sv.parse_answer_for_field("related", "   ")
    assert v is None and e


def test_goal_parse():
    """goal 自由文本：原样保存用户原话，仅校验非空。"""
    raw = "熟悉python语法，基本看懂python项目里面的代码逻辑"
    assert sv.parse_answer_for_field("goal", raw) == (raw, None)
    assert sv.parse_answer_for_field("goal", "想深入原理看源码") == ("想深入原理看源码", None)
    assert sv.parse_answer_for_field("goal", "随便看看") == ("随便看看", None)
    v, e = sv.parse_answer_for_field("goal", "   ")
    assert v is None and e


def test_time_budget_parse():
    assert sv.parse_answer_for_field("time_budget", "每天2小时")[0] == 2.0
    assert sv.parse_answer_for_field("time_budget", "1.5小时")[0] == 1.5
    v, e = sv.parse_answer_for_field("time_budget", "")
    assert v is None and e


def test_apply_answer_error_does_not_mutate():
    a = {}
    a2, err = sv.apply_answer(a, "self_level", "abc")
    assert err
    assert a2 is a  # 失败时原样返回，不写字段
    assert "self_level" not in a


def test_next_field_sequence_and_complete():
    a = {}
    assert sv.next_field(a) == "self_level"
    a, _ = sv.apply_answer(a, "self_level", "5")
    assert sv.next_field(a) == "related"
    a, _ = sv.apply_answer(a, "related", "Java")
    a, _ = sv.apply_answer(a, "goal", "跑通")
    a, _ = sv.apply_answer(a, "time_budget", "2小时")
    assert sv.is_fixed_done(a)
    assert sv.next_field(a) is None
    assert not sv.is_survey_complete(a)  # 固定字段收齐但诊断题未够
    a["diagnostics"] = ["答1", "答2"]
    assert sv.is_survey_complete(a)


def test_diagnostics_cap():
    a = {"diagnostics": ["1"]}
    assert not sv.diagnostics_done(a)
    a["diagnostics"] = ["1", "2", "3"]  # 超上限只判够数，不判超限
    assert sv.diagnostics_done(a)


def test_derive_profile_buckets():
    assert sv.derive_profile({"self_level": 2}, "t")["bucket"] == "beginner"
    assert sv.derive_profile({"self_level": 3}, "t")["bucket"] == "beginner"
    assert sv.derive_profile({"self_level": 5}, "t")["bucket"] == "intermediate"
    assert sv.derive_profile({"self_level": 8}, "t")["bucket"] == "developer"
    assert sv.derive_profile({}, "t")["bucket"] == "beginner"  # 未填按小白处理


def test_derive_profile_carries_fields():
    p = sv.derive_profile({"self_level": 8, "related": "Java", "goal": "能看懂项目代码",
                           "time_budget": 2.0, "diagnostics": ["a"]}, "Spring")
    assert p["related"] == "Java"
    assert p["goal"] == "能看懂项目代码"
    assert p["diagnostics"] == ["a"]


def test_profile_summary():
    p = sv.derive_profile({"self_level": 8, "related": "Java", "goal": "能独立写个小工具",
                           "time_budget": 2.0}, "Spring Boot")
    s = sv.profile_summary(p)
    assert "Spring Boot" in s
    assert "开发者" in s
    assert "目标：能独立写个小工具" in s


def test_profile_summary_legacy_enum_goal():
    """旧 profile.json / checkpoint 残留的枚举 goal 兼容渲染为可读文案。"""
    p = sv.derive_profile({"self_level": 8, "goal": sv.GOAL_DEEP}, "X")
    assert "目标：深入原理" in sv.profile_summary(p)
    p2 = sv.derive_profile({"self_level": 8, "goal": sv.GOAL_MIN_PROJECT}, "X")
    assert "目标：快速上手跑通最小项目" in sv.profile_summary(p2)


# ---------- 诊断题：标准答案解析 / 选项提取 / 自测渲染 ----------

def test_extract_diag_answer_normal():
    """模型内嵌「【答案】X」：剥离答案行，返回题目 + 大写答案。"""
    q = "单选：Spring Boot 自动配置基于什么机制？\nA. 手写配置\nB. 条件装配\nC. XML\nD. 注解扫描\n请直接回复选项字母。\n【答案】B"
    cleaned, correct = sv.extract_diag_answer(q)
    assert correct == "B"
    assert "【答案】" not in cleaned  # 已剥离
    assert cleaned.startswith("单选：Spring Boot")
    assert cleaned.endswith("请直接回复选项字母。")  # 引导语保留


def test_extract_diag_answer_variants():
    """小写答案、无标记、题干无引导语。"""
    assert sv.extract_diag_answer("题A 题B 【答案】a") == ("题A 题B", "A")
    assert sv.extract_diag_answer("普通问题，没有答案") == ("普通问题，没有答案", None)
    assert sv.extract_diag_answer("") == ("", None)


def test_parse_diag_choice():
    """用户回答提取选项字母（容忍变体），提取不到 → None（不误判为错）。"""
    assert sv.parse_diag_choice("B") == "B"
    assert sv.parse_diag_choice("选B") == "B"
    assert sv.parse_diag_choice("答案是 b") == "B"
    assert sv.parse_diag_choice("我不确定，随便猜一个") is None
    assert sv.parse_diag_choice("") is None


def test_profile_summary_diag_grades():
    """有判定的诊断题 → 渲染「诊断自测」统计（内部信号，进 planning/coaching 提示词）。"""
    diag = [
        {"question": "q1", "answer": "B", "correct": "B", "grade": sv.GRADE_RIGHT},
        {"question": "q2", "answer": "C", "correct": "B", "grade": sv.GRADE_WRONG},
    ]
    p = sv.derive_profile({"self_level": 8, "diagnostics": diag}, "Spring Boot")
    s = sv.profile_summary(p)
    assert "诊断自测：2题中1对、1错" in s
    assert "第1题对" in s and "第2题错" in s


def test_profile_summary_diag_not_graded_skipped():
    """未判定（correct/choice 缺失 → grade None）或旧 [str] 条目：不渲染、不误报统计。"""
    diag = [
        {"question": "q1", "answer": "B", "correct": None, "grade": None},
        "旧格式回答字符串",  # 兼容旧 checkpoint / 旧 profile.json
    ]
    p = sv.derive_profile({"self_level": 8, "diagnostics": diag}, "X")
    s = sv.profile_summary(p)
    assert "诊断自测" not in s  # 无判定则不渲染
    assert "技术：X" in s
