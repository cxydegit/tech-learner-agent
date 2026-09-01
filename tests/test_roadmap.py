"""domain/roadmap 纯规则单测：结构校验 / id 分配 / 里程碑推进 / Markdown 渲染。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_roadmap.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.domain import roadmap as rm


def _stages():
    """两阶段样例：s1 两个里程碑、s2 一个里程碑。"""
    return [
        {"name": "环境搭建", "goal": "跑通 hello world", "est_hours": 4,
         "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello world"}]},
        {"name": "核心概念", "goal": "掌握核心", "est_hours": 8,
         "milestones": [{"desc": "理解 A"}]},
    ]


def _build():
    stages, _ = rm.normalize_stages(_stages())
    return rm.build_roadmap("t", "goal", 12, stages)


# ---------- normalize_stages ----------

def test_normalize_assigns_ids():
    stages, errors = rm.normalize_stages(_stages())
    assert errors == []
    assert stages[0]["id"] == "s1"
    assert stages[0]["milestones"][0]["id"] == "s1-m1"
    assert stages[0]["milestones"][0]["done"] is False
    assert stages[0]["milestones"][1]["id"] == "s1-m2"


def test_normalize_keeps_materials():
    raw = [{**_stages()[0], "materials": "官方文档"}]
    stages, _ = rm.normalize_stages(raw)
    assert stages[0]["materials"] == "官方文档"


def test_normalize_drops_invalid_stage():
    raw = _stages() + [{"name": "", "goal": "x", "est_hours": 1, "milestones": [{"desc": "d"}]}]
    stages, errors = rm.normalize_stages(raw)
    assert len(stages) == 2
    assert any("阶段 3" in e for e in errors)
    assert stages[-1]["id"] == "s2"  # id 按合法阶段连续分配


def test_normalize_skips_invalid_first():
    bad = {"name": "", "goal": "x", "est_hours": 1, "milestones": [{"desc": "d"}]}
    stages, errors = rm.normalize_stages([bad, _stages()[1]])
    assert [s["name"] for s in stages] == ["核心概念"]
    assert stages[0]["id"] == "s1"


def test_normalize_rejects_non_dict():
    stages, errors = rm.normalize_stages([["not", "dict"]])
    assert stages == []
    assert errors


# ---------- parse_roadmap_raw ----------

def test_parse_roadmap_raw_ok():
    raw = '{"goal":"能跑通最小项目","total_hours":12,"stages":[{"name":"A","goal":"g","est_hours":4,"milestones":[{"desc":"m1"}]}]}'
    r, errs = rm.parse_roadmap_raw(raw, "spring-boot")
    assert not errs
    assert r["tech"] == "spring-boot"
    assert r["current_stage"] == "s1"
    assert r["status"] == "active"


def test_parse_roadmap_raw_bad_json():
    r, errs = rm.parse_roadmap_raw("not json at all", "x")
    assert r is None
    assert errs


def test_parse_roadmap_raw_bad_total_hours():
    raw = '{"goal":"g","total_hours":"many","stages":[{"name":"A","goal":"g","est_hours":4,"milestones":[{"desc":"m"}]}]}'
    r, errs = rm.parse_roadmap_raw(raw, "x")
    assert r is None
    assert any("total_hours" in e for e in errs)


def test_parse_roadmap_raw_empty_stages():
    raw = '{"goal":"g","total_hours":10,"stages":[]}'
    r, errs = rm.parse_roadmap_raw(raw, "x")
    assert r is None


# ---------- complete_milestone / 阶段推进 ----------

def test_complete_milestone_advances_stage():
    r = _build()
    r = rm.complete_milestone(r, "s1-m1")
    assert r["current_stage"] == "s1"
    r = rm.complete_milestone(r, "s1-m2")
    assert r["current_stage"] == "s2"
    assert r["status"] == "active"


def test_complete_all_marks_completed():
    r = _build()
    r = rm.complete_milestone(r, "s1-m1")
    r = rm.complete_milestone(r, "s1-m2")
    r = rm.complete_milestone(r, "s2-m1")
    assert r["status"] == "completed"


def test_complete_milestone_unknown_raises():
    with pytest.raises(KeyError):
        rm.complete_milestone(_build(), "s9-m9")


def test_uncheck_does_not_roll_back_stage():
    r = _build()
    r = rm.complete_milestone(r, "s1-m1")
    r = rm.complete_milestone(r, "s1-m2")  # 推进到 s2
    r = rm.complete_milestone(r, "s1-m1", done=False)  # 取消勾选不回退（MVP 防震荡）
    assert r["current_stage"] == "s2"


def test_uncheck_completed_resets_status():
    """勾满标 completed 后再取消勾选 → 状态回退 active + 当前阶段指回未完成阶段。

    （真实事故：取消勾选后 status 仍是 completed，coaching 提示词显示「✅ 已完成」
    但里程碑未勾满，误导 Agent 判断。）
    """
    r = _build()
    for mid in ("s1-m1", "s1-m2", "s2-m1"):
        r = rm.complete_milestone(r, mid)
    assert r["status"] == "completed"
    r2 = rm.complete_milestone(r, "s2-m1", done=False)
    assert r2["status"] == "active"
    assert r2["current_stage"] == "s2"
    # 未取消的 s1 里程碑保持完成
    assert r2["stages"][0]["milestones"][0]["done"] is True


def test_milestone_update_does_not_mutate_input():
    r = _build()
    r2 = rm.complete_milestone(r, "s1-m1")
    assert r["stages"][0]["milestones"][0]["done"] is False  # 原路线不受影响
    assert r2["stages"][0]["milestones"][0]["done"] is True


# ---------- stage_progress / markdown / validate ----------

def test_stage_progress():
    r = _build()
    p = rm.stage_progress(r, "s1")
    assert (p["total"], p["done"], p["pct"]) == (2, 0, 0)
    r = rm.complete_milestone(r, "s1-m1")
    assert rm.stage_progress(r, "s1")["pct"] == 50


def test_roadmap_to_markdown():
    md = rm.roadmap_to_markdown(_build())
    assert "# t 学习路线" in md
    assert "[ ]" in md
    assert "s1" in md


def test_validate_roadmap_ok_and_bad():
    r = _build()
    assert rm.validate_roadmap(r) == []
    bad = dict(r)
    bad["stages"] = []
    assert rm.validate_roadmap(bad)


# ---------- merge_progress（修订保留进度） ----------

def test_merge_progress_keeps_done_by_desc():
    old = rm.complete_milestone(_build(), "s1-m1")  # s1-m1「安装完成」已勾选
    new = _build()  # 结构相同
    merged = rm.merge_progress(old, new)
    assert merged["stages"][0]["milestones"][0]["done"] is True  # desc 匹配 → 保留
    assert merged["stages"][0]["milestones"][1]["done"] is False
    assert merged["status"] == "active"


def test_merge_progress_ignores_removed_stage():
    old = rm.complete_milestone(_build(), "s1-m1")  # 阶段1 的「安装完成」已勾选
    # 新路线删掉阶段1，只剩阶段2（其里程碑描述不在旧勾选集）
    stages, _ = rm.normalize_stages([_stages()[1]])
    new = rm.build_roadmap("t", "goal", 8, stages)
    merged = rm.merge_progress(old, new)
    assert all(not m["done"] for s in merged["stages"] for m in s["milestones"])
    assert merged["current_stage"] == "s1"


def test_merge_progress_advances_to_first_unfinished():
    old = rm.complete_milestone(_build(), "s1-m1")
    old = rm.complete_milestone(old, "s1-m2")  # s1 全完成，推进到 s2
    new = _build()
    merged = rm.merge_progress(old, new)
    # s1 的两个里程碑描述匹配保留 done → s1 仍全完成 → current_stage 校正到 s2
    assert merged["stages"][0]["milestones"][0]["done"] is True
    assert merged["stages"][0]["milestones"][1]["done"] is True
    assert merged["current_stage"] == "s2"
    assert merged["status"] == "active"


def test_merge_progress_all_done_completed():
    old = _build()
    old = rm.complete_milestone(old, "s1-m1")
    old = rm.complete_milestone(old, "s1-m2")
    old = rm.complete_milestone(old, "s2-m1")  # 全部完成
    new = _build()
    merged = rm.merge_progress(old, new)
    assert merged["status"] == "completed"
    assert all(m["done"] for s in merged["stages"] for m in s["milestones"])


def test_merge_progress_does_not_mutate_inputs():
    old = rm.complete_milestone(_build(), "s1-m1")
    new = _build()
    merged = rm.merge_progress(old, new)
    assert old["stages"][0]["milestones"][0]["done"] is True  # 旧路线勾选保持
    assert new["stages"][0]["milestones"][0]["done"] is False  # 新路线未被改
    assert merged is not new
