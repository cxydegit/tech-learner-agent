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
    assert sv.parse_answer_for_field("goal", "想快速上手跑个最小项目")[0] == sv.GOAL_MIN_PROJECT
    assert sv.parse_answer_for_field("goal", "想深入原理看源码")[0] == sv.GOAL_DEEP
    v, e = sv.parse_answer_for_field("goal", "随便看看")
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
    p = sv.derive_profile({"self_level": 8, "related": "Java", "goal": sv.GOAL_DEEP,
                           "time_budget": 2.0, "diagnostics": ["a"]}, "Spring")
    assert p["related"] == "Java"
    assert p["diagnostics"] == ["a"]


def test_profile_summary():
    p = sv.derive_profile({"self_level": 8, "related": "Java", "goal": sv.GOAL_DEEP,
                           "time_budget": 2.0}, "Spring Boot")
    s = sv.profile_summary(p)
    assert "Spring Boot" in s
    assert "开发者" in s
    assert "深入原理" in s
