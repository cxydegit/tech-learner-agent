"""briefing 纯函数单测：mode 契约 / 证据与路线 / 去重 / 预算 / 稳定渲染。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain import briefing as briefing_domain
from src.domain import memory as memory_domain

NOW = "2026-11-22T12:00:00+08:00"


def _cap(point_id, text, evidence, at="2026-11-01", **extra):
    item = {
        "id": point_id,
        "source_event_id": f"evt-{point_id}",
        "point": text,
        "evidence": evidence,
        "at": at,
        "first_at": at,
        "milestone_id": extra.pop("milestone_id", None),
        "status": "active",
    }
    item.update(extra)
    return item


def _cap_block(tech, points, last_active="2026-11-01"):
    return {
        "tech": tech,
        "last_active": last_active,
        "summary": "",
        "mastered": points,
        "route_present": True,
    }


def _view():
    return {
        "capabilities": {
            "python": _cap_block("Python", [
                _cap("cap:python:slice", "掌握字符串切片规则与负数下标含义", "verified",
                     "2026-11-21", milestone_id="s1-m1"),
                _cap("cap:python:forgot", "整理出一份「我确实忘了的点」清单", "verified",
                     "2026-11-21", milestone_id="s1-m2"),
                _cap("cap:python:func", "定义一个带有参数和返回值的函数", "unknown",
                     "2026-09-01"),
                _cap("cap:python:random", "成功导入 random 模块并生成随机数", "unknown",
                     "2026-09-01"),
            ]),
            "react": _cap_block("React", [
                _cap("cap:react:decl", "理解声明式与命令式的区别", "unknown",
                     "2026-11-16"),
                _cap("cap:react:one", "用一句话说明 React 是什么", "unknown",
                     "2026-11-15"),
                _cap("cap:react:pain", "知道 React 相比 HTML 拼页面的痛点", "unknown",
                     "2026-11-14"),
                _cap("cap:react:jsx", "JSX 基础语法", "unknown", "2026-09-01"),
            ], last_active="2026-11-16"),
            "pytorch": _cap_block("PyTorch", [
                _cap("cap:pytorch:terms", "听完 8 个核心术语", "unknown", "2026-10-13"),
                _cap("cap:pytorch:relation", "理解 AI/ML/深度学习的包含关系", "unknown",
                     "2026-10-12"),
                _cap("cap:pytorch:explain", "用大白话解释深度学习", "unknown",
                     "2026-10-11"),
            ], last_active="2026-10-13"),
        },
        "preferences": [
            {"id": "pref:global:analogy", "key": "analogy", "text": "不要用类比",
             "scope": "global", "tech_key": "", "last_confirmed": "2026-11-01"},
            {"id": "pref:tech:python:time", "key": "other",
             "text": "时间紧，不喜欢手敲练习", "scope": "tech", "tech_key": "python",
             "last_confirmed": "2026-11-01"},
            {"id": "pref:tech:python:style", "key": "other",
             "text": "偏好先看实际运行结果", "scope": "tech", "tech_key": "python",
             "last_confirmed": "2026-11-01"},
        ],
        "facts": [
            {"id": "fact:global:java", "key": "constraint:java_version",
             "text": "公司项目用 Java 8", "scope": "global", "tech_key": "",
             "last_confirmed": "2026-11-01"},
            {"id": "fact:tech:python:related", "key": "related", "text": "无",
             "scope": "tech", "tech_key": "python", "last_confirmed": "2026-11-01"},
            {"id": "fact:tech:python:level", "key": "self_level", "text": "5",
             "scope": "tech", "tech_key": "python", "last_confirmed": "2026-11-01"},
            {"id": "fact:tech:python:budget", "key": "time_budget", "text": "1",
             "scope": "tech", "tech_key": "python", "last_confirmed": "2026-11-01"},
            {"id": "fact:tech:python:goal", "key": "goal", "text": "看懂项目代码",
             "scope": "tech", "tech_key": "python", "last_confirmed": "2026-11-01"},
            {"id": "fact:tech:python:project", "key": "constraint:project",
             "text": "公司代码用 Python 3.8", "scope": "tech", "tech_key": "python",
             "last_confirmed": "2026-11-01"},
        ],
        "goals": {
            "global": [{"id": "goal:global:job", "text": "转岗 AI 应用开发"}],
            "by_tech": {"python": [{"id": "goal:tech:python", "text": "看懂项目代码"}]},
        },
        "open_items": [
            {"id": "open:python:exception", "tech_key": "python",
             "text": "下次讲异常处理", "expires_at": "2026-12-01"},
            {"id": "open:python:expired", "tech_key": "python",
             "text": "过期事项", "expires_at": "2026-01-01"},
            {"id": "open:python:typing", "tech_key": "python",
             "text": "想补 typing", "expires_at": "2026-12-01"},
            {"id": "open:react:closure", "tech_key": "react",
             "text": "React 闭包", "expires_at": "2026-12-01"},
        ],
    }


def _roadmap(*, done_points=None, extra_done=None):
    done_points = done_points or [
        "掌握字符串切片规则与负数下标含义",
        "整理出一份「我确实忘了的点」清单",
    ]
    milestones = [
        {"id": f"s1-m{i}", "desc": point, "done": True}
        for i, point in enumerate(done_points, 1)
    ]
    if extra_done:
        milestones.append({"id": f"s1-m{len(milestones) + 1}",
                           "desc": extra_done, "done": True})
    milestones.append({"id": "s1-m99", "desc": "尚未完成的里程碑", "done": False})
    return {
        "tech": "Python",
        "goal": "g",
        "total_hours": 10,
        "stages": [{"id": "s1", "name": "阶段", "goal": "g", "est_hours": 5,
                    "milestones": milestones}],
        "current_stage": "s1",
        "status": "active",
    }


def _profile():
    return {
        "self_level": 5,
        "related": "无",
        "goal": "看懂项目代码",
        "time_budget": "1",
        "diagnostics": [{"grade": "right"}],
    }


def _build(mode, **overrides):
    kwargs = {
        "view": _view(),
        "mode": mode,
        "tech": "Python",
        "profile": _profile(),
        "roadmap": _roadmap(),
        "coach_facts": ["时间紧，不喜欢手敲练习"],
        "coach_open_items": [{"text": "下次讲异常处理"}],
        "now": NOW,
    }
    kwargs.update(overrides)
    return briefing_domain.build_briefing(**kwargs)


def test_survey_includes_current_and_other_tech_points():
    brief = _build("survey")
    text = brief["text"]
    assert "字符串切片" in text
    assert "random" in text
    assert "React" in text and "声明式" in text
    assert "PyTorch" in text and "8 个核心术语" in text
    assert "不要用类比" in text and "Java 8" in text
    # 其他技术 top 3：React 最旧的 JSX 不应出现
    assert "JSX" not in text
    # 不是 roadmap、不是诊断、不是计数-only、不是技术偏好
    assert "当前路线" not in text
    assert "完成 2/" not in text
    assert "诊断" not in text
    assert "时间紧" not in text
    assert "self_level" not in text and "related" not in text
    assert "转岗 AI" not in text
    assert {s["kind"] for s in brief["sections"]} == {"identity", "current", "background"}


def test_survey_records_top_n_cut_for_other_techs():
    brief = _build("survey")
    dropped = {item.get("id"): item.get("reason") for item in brief["dropped"]}
    # React 有 4 个点，其他技术 top 3 → 最旧的 JSX 被 N 截断并留痕
    assert dropped.get("cap:react:jsx") == "n_cut"
    assert "cap:react:jsx" not in brief["included_ids"]
    # 当前技术走 current_points，不受 other_points 限制
    assert "cap:python:random" in brief["included_ids"]


def test_briefing_records_section_lengths():
    brief = _build("survey")
    lengths = brief["section_lengths"]
    assert set(lengths) == {"identity", "current", "background"}
    assert all(lengths[kind] > 0 for kind in lengths)
    nonempty = [n for n in lengths.values() if n > 0]
    assert sum(nonempty) + len(nonempty) - 1 == len(brief["text"])


def test_planning_current_tech_only_with_evidence_labels():
    brief = _build("planning")
    text = brief["text"]
    assert "字符串切片" in text
    assert "[当前路线·已验收]" in text
    assert "random" in text
    assert "[历史路线·未考据]" in text
    assert "React" not in text and "PyTorch" not in text
    assert "诊断" not in text
    assert "完成 2/" not in text
    assert "时间紧" not in text
    assert "转岗 AI" in text and "不要用类比" in text
    # planning 不放全局事实/约束
    assert "Java 8" not in text
    assert {s["kind"] for s in brief["sections"]} == {"identity", "current"}


def test_coaching_only_delta_and_dedupes_prompt_sections():
    brief = _build("coaching")
    text = brief["text"]
    assert "不要用类比" in text and "Java 8" in text
    # 技术偏好：time 被 coach_facts 覆盖，style 未被覆盖
    assert "时间紧" not in text
    assert "偏好先看实际运行结果" in text
    # 未决项：exception 被 coach_open_items 覆盖，typing 未被覆盖；过期项不出现
    assert "下次讲异常处理" not in text
    assert "想补 typing" in text
    assert "过期事项" not in text
    # 当前路线/历史点/其他技术/诊断都不进 coaching
    assert "字符串切片" not in text
    assert "random" not in text
    assert "React" not in text and "PyTorch" not in text
    assert "诊断" not in text
    assert "完成 2/" not in text
    assert "self_level" not in text and "related" not in text
    # 所有 done 都已验收 → 不生成异常段
    assert "注意：里程碑" not in text
    assert {s["kind"] for s in brief["sections"]} == {"identity", "coaching_delta"}
    dropped = {item.get("id"): item.get("reason") for item in brief["dropped"]}
    assert dropped.get("pref:tech:python:time") == "covered"
    assert dropped.get("open:python:exception") == "covered"
    assert dropped.get("open:python:expired") == "expired"
    assert dropped.get("fact:tech:python:related") == "covered"
    assert dropped.get("fact:tech:python:goal") == "covered"
    assert "pref:tech:python:time" not in brief["included_ids"]
    assert "open:python:expired" not in brief["included_ids"]


def test_global_and_tech_same_text_appears_once():
    view = _view()
    view["preferences"].append({
        "id": "pref:tech:python:dup", "key": "other",
        "text": "不要用类比", "scope": "tech", "tech_key": "python",
        "last_confirmed": "2026-11-01",
    })
    brief = _build("coaching", view=view)
    assert brief["text"].count("不要用类比") == 1
    dropped = {item.get("id"): item.get("reason") for item in brief["dropped"]}
    assert dropped.get("pref:tech:python:dup") == "covered"


def test_planning_reserves_room_for_historical_unverified_points():
    view = _view()
    view["capabilities"]["python"]["mastered"] = [
        _cap(f"cap:python:current{i}", f"当前路线已验收点 {i}", "verified",
             f"2026-11-{i + 1:02d}")
        for i in range(10)
    ] + [
        _cap("cap:python:history1", "历史未考据点一", "unknown", "2026-09-01"),
        _cap("cap:python:history2", "历史未考据点二", "unknown", "2026-09-01"),
    ]
    roadmap = {
        "tech": "Python",
        "goal": "g",
        "total_hours": 10,
        "stages": [{"id": "s1", "name": "阶段", "goal": "g", "est_hours": 5,
                    "milestones": [
                        {"id": f"s1-m{i}", "desc": f"当前路线已验收点 {i}",
                         "done": True}
                        for i in range(10)
                    ]}],
        "current_stage": "s1",
        "status": "active",
    }
    brief = _build("planning", view=view, roadmap=roadmap,
                   limits={"current_points": 8, "max_chars": 5000})
    text = brief["text"]
    assert "历史未考据点一" in text
    assert "[历史路线·未考据]" in text
    assert text.count("[当前路线·已验收]") == 6
    n_cut = [item for item in brief["dropped"] if item.get("reason") == "n_cut"]
    assert len(n_cut) == 4
    assert all(item["id"] not in brief["included_ids"] for item in n_cut)


def test_coaching_exception_names_specific_milestones():
    brief = _build(
        "coaching",
        roadmap=_roadmap(
            done_points=["掌握字符串切片规则与负数下标含义"],
            extra_done="定义一个带有参数和返回值的函数",
        ),
    )
    text = brief["text"]
    assert "注意：里程碑「定义一个带有参数和返回值的函数」" in text
    assert "未考据" in text
    # 已验收的里程碑不报警
    assert "注意：里程碑「掌握字符串切片规则与负数下标含义」" not in text


def test_coaching_exception_for_done_without_effective_evidence():
    brief = _build(
        "coaching",
        roadmap=_roadmap(done_points=[], extra_done="一个没有记忆证据的里程碑"),
    )
    text = brief["text"]
    assert "注意：里程碑「一个没有记忆证据的里程碑」" in text
    assert "没有有效证据" in text


def test_briefing_keeps_full_point_and_budget_drops_whole_item():
    long_point = "这是一个需要完整保留的长技术点，" * 4
    view = _view()
    view["capabilities"]["pytorch"]["mastered"] = [
        _cap("cap:pytorch:long", long_point, "unknown", "2026-10-13")
    ]
    brief = _build("survey", view=view, limits={"max_chars": 5000})
    expected = memory_domain.clean_text(long_point)
    assert expected in brief["text"]
    assert "…" not in expected
    small = _build("survey", view=view, limits={"max_chars": 60})
    assert long_point not in small["text"]
    assert any(item.get("id") == "cap:pytorch:long"
               and item.get("reason") == "budget" for item in small["dropped"])
    assert all(len(section.get("items") or []) >= 0 for section in small["sections"])


def test_briefing_is_stable_for_same_input():
    first = _build("survey")
    second = _build("survey")
    assert first["text"] == second["text"]
    assert first["included_ids"] == second["included_ids"]
    assert first["dropped"] == second["dropped"]


def test_empty_view_returns_empty_briefing():
    brief = briefing_domain.build_briefing(
        {}, mode="coaching", tech="Python", now=NOW, roadmap=None)
    assert brief["text"] == ""
    assert brief["included_ids"] == []
    assert brief["dropped"] == []
    assert brief["truncated"] is False
    assert brief["section_lengths"] == {"identity": 0, "coaching_delta": 0}


def test_capability_items_always_carry_full_text():
    brief = _build("survey")
    for section in brief["sections"]:
        for item in section.get("items") or []:
            if item.get("kind") == "capability":
                assert item.get("text")
                assert "…" not in item["text"]
