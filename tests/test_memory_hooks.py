"""长期记忆与图/提示词的接线单测：coach_trim 编译任务简报、三模式提示词注入。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_hooks.py -v
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import graph
from src.config import config
from src.pipelines import route


def _seed_history():
    """写一份最小 profile + roadmap，让首次 ensure 时迁移出历史底账。"""
    config.LEARNER_DIR.mkdir(parents=True, exist_ok=True)
    config.ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
    (config.LEARNER_DIR / "profile.json").write_text(json.dumps({
        "python": {"tech": "Python", "self_level": 4, "related": "熟悉 Java",
                   "goal": "看懂项目代码", "time_budget": "1"},
    }, ensure_ascii=False), encoding="utf-8")
    (config.ROADMAP_DIR / "python.json").write_text(json.dumps({
        "tech": "Python", "goal": "g", "total_hours": 10,
        "stages": [{"id": "s1", "name": "阶段", "goal": "g", "est_hours": 5,
                    "milestones": [
                        {"id": "s1-m1", "desc": "能写函数", "done": True},
                        {"id": "s1-m2", "desc": "能写类", "done": False},
                    ]}],
        "current_stage": "s1", "status": "active",
        "created_at": "2026-09-01 09:00", "updated_at": "2026-09-01 10:00",
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    learner_dir = tmp_path / "learner"
    roadmap_dir = tmp_path / "roadmaps"
    monkeypatch.setattr(config, "LEARNER_DIR", learner_dir)
    monkeypatch.setattr(config, "ROADMAP_DIR", roadmap_dir)
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    _seed_history()
    return tmp_path


def _state(text="技术记忆与异常\n- 偏好[other]：先给运行结果"):
    return {"memory_briefing": {
        "text": text, "included_ids": ["pref:tech:python:style"],
        "dropped": [], "truncated": False, "counts": {},
    }}


# ---------- coach_trim ----------

def test_coach_trim_clears_briefing_when_memory_disabled(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_ENABLED", False)
    updates = graph.coach_trim({
        "mode": "coaching", "tech": "Python", "coach_messages": [],
    })
    assert updates["memory_briefing"] == {}


def test_coach_trim_builds_briefing_delta(dirs, audit_events):
    roadmap = json.loads(
        (config.ROADMAP_DIR / "python.json").read_text(encoding="utf-8"))
    updates = graph.coach_trim({
        "mode": "coaching", "tech": "Python", "coach_messages": [],
        "learner_profile": {"self_level": 4, "related": "熟悉 Java",
                            "goal": "看懂项目代码", "time_budget": "1"},
        "roadmap": roadmap,
    })
    brief = updates["memory_briefing"]
    assert set(brief) == {"text", "included_ids", "dropped", "truncated",
                          "counts", "section_lengths"}
    assert brief["text"]
    # coaching 只点名具体异常里程碑，不重铺路线清单与其他技术底账
    assert "注意：里程碑「能写函数」" in brief["text"]
    assert "跨技术学习底账" not in brief["text"]
    assert brief["included_ids"]
    event = [e for e in audit_events if e.get("event") == "ltm_brief"][-1]
    assert event["sections"] == brief["section_lengths"]


def test_coach_trim_briefing_bounded_by_mode_budget(dirs):
    """简报正文不超过该 mode 的字符预算（超预算整条丢，不切文本）。"""
    brief = graph.coach_trim({
        "mode": "survey", "tech": "Python", "coach_messages": [],
    })["memory_briefing"]
    assert len(brief["text"]) <= config.ltm_brief_limits("survey")["max_chars"]
    assert brief["truncated"] is False


def test_coach_llm_dumps_briefing_at_prompt_boundary(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_DEBUG_DUMP", True)
    captured = {}

    def fake_chat(prompt, messages, tools, call_site=None):
        captured["prompt"] = prompt
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(graph, "chat_with_tools", fake_chat)
    briefing = {
        "text": "技术记忆与异常\n- 偏好[other]：先给运行结果",
        "included_ids": ["pref:tech:python:style"],
        "dropped": [{"id": "fact:tech:python:related", "kind": "fact",
                     "reason": "covered"}],
        "truncated": False,
        "counts": {},
        "section_lengths": {"identity": 3, "coaching_delta": 19},
    }
    state = {
        "mode": "coaching", "tech": "Python", "coach_messages": [],
        "memory_briefing": briefing, "coach_turn_index": 8,
    }
    graph.coach_llm(state)
    path = config.AUDIT_LOG_DIR / "ltm_inject.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["schema"] == 3
    assert row["mode"] == "coaching" and row["tech"] == "Python"
    assert row["turn"] == 8
    assert row["has_ltm"] is True
    assert row["ltm_text"] in captured["prompt"]
    assert row["ltm_appended"] is True
    assert row["prompt_chars"] == len(captured["prompt"])
    assert row["ids"] == ["pref:tech:python:style"]
    assert row["dropped"] == briefing["dropped"]
    assert row["truncated"] is False
    assert row["section_lengths"] == briefing["section_lengths"]


def test_briefing_prompt_uses_evidence_aware_rules():
    state = {"memory_briefing": {"text": "简报正文"}}
    survey = route._survey_prompt(state, "Python")
    assert "未考据的点只说明曾接触过" in survey
    planning = route._planning_prompt({**state, "mode": "planning"}, "Python")
    assert "只把标为「已验收」「已声明」的点视为可跳过" in planning
    assert "已掌握的内容不要重复排入路线" not in planning
    coaching = route._coaching_prompt({**state, "mode": "coaching"}, "Python")
    assert "只补画像/当前路线没有的异常" in coaching


# ---------- 提示词注入 ----------

def test_survey_prompt_includes_briefing_and_rule():
    prompt = route._survey_prompt(_state(), "Python")
    assert "未考据的点只说明曾接触过" in prompt
    assert "- 偏好[other]：先给运行结果" in prompt


def test_planning_prompt_includes_briefing_and_rule():
    prompt = route._planning_prompt({**_state(), "mode": "planning"}, "Python")
    assert "只把标为「已验收」「已声明」的点视为可跳过" in prompt
    assert "- 偏好[other]：先给运行结果" in prompt


def test_coaching_prompt_includes_briefing_and_rule():
    prompt = route._coaching_prompt({**_state(), "mode": "coaching"}, "Python")
    assert "只补画像/当前路线没有的异常" in prompt
    assert "- 偏好[other]：先给运行结果" in prompt


@pytest.mark.parametrize("builder", [
    route._survey_prompt,
    route._planning_prompt,
    route._coaching_prompt,
])
def test_prompts_do_not_render_empty_memory(builder):
    prompt = builder({}, "Python")
    assert "长期记忆简报（跨会话）" not in prompt
    assert "跨会话长期记忆（只补画像" not in prompt


def test_prompt_output_is_stable_for_same_state():
    state = {**_state(), "mode": "coaching"}
    assert route._coaching_prompt(state, "Python") == \
        route._coaching_prompt(state, "Python")


def test_recall_prompt_rules_are_gated_by_flag(monkeypatch):
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", False)
    for builder in (route._survey_prompt, route._planning_prompt,
                    route._coaching_prompt):
        assert "recall_memory" not in builder({}, "Python")
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", True)
    assert "recall_memory" in route._survey_prompt({}, "Python")
    assert "recall_memory" in route._planning_prompt({}, "Python")
    assert "recall_memory" in route._coaching_prompt({}, "Python")
