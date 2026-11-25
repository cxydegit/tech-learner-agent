"""domain/memory 纯规则单测：折叠 / 去重与 supersede / 能力投影 / 渲染 / 上限归档。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_domain.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain import memory as mem


def _cap(event_id, point, *, tech="python", op="add", evidence="verified",
         at="2026-09-01T10:00:00", source="system", milestone_id=None):
    return mem.make_event(
        "capability_recorded", at=at, session="t", source=source, event_id=event_id,
        tech=tech, point=point, op=op, evidence=evidence,
        milestone_id=milestone_id)


def _profile(event_id, category, key, text, *, scope="global", tech="",
             at="2026-09-01T10:00:00", source="user"):
    return mem.make_event(
        "profile_recorded", at=at, session="t", source=source, event_id=event_id,
        category=category, scope=scope, tech=tech, key=key, text=text)


def _open(event_id, text, *, op="open", tech="python", item_id=None,
          expires_at=None, at="2026-09-01T10:00:00"):
    return mem.make_event(
        "open_item_recorded", at=at, session="t", source="user", event_id=event_id,
        op=op, tech=tech, item_id=item_id, text=text, expires_at=expires_at)


def _fold(events, *, roadmaps=None, now="2026-11-18T10:00:00", caps=None):
    return mem.fold_events(events, roadmaps=roadmaps, now=now, caps=caps)


# ---------- 能力身份与证据 ----------

def test_capability_identity_uses_normalized_point_and_tech_key():
    """同描述不同里程碑 id / 不同大小写技术名 = 同一能力点。"""
    view, _ = _fold([
        _cap("e1", "能写函数", tech="Python", milestone_id="s1-m1"),
        _cap("e2", "  能写函数 ", tech="python", evidence="claimed",
             milestone_id="s9-m9", at="2026-09-02T10:00:00"),
    ])
    cap = view["capabilities"]["python"]
    assert len(cap["mastered"]) == 1
    assert cap["mastered"][0]["id"].startswith("cap:python:")
    assert cap["mastered"][0]["milestone_id"] == "s9-m9"


def test_evidence_only_upgrades():
    """unknown → verified → claimed：不允许降级。"""
    view, _ = _fold([
        _cap("e1", "点A", evidence="unknown"),
        _cap("e2", "点A", evidence="verified", at="2026-09-02T10:00:00"),
        _cap("e3", "点A", evidence="claimed", at="2026-09-03T10:00:00"),
    ])
    assert view["capabilities"]["python"]["mastered"][0]["evidence"] == "verified"


def test_revoke_removes_and_readd_starts_new_episode():
    view, _ = _fold([
        _cap("e1", "点A", evidence="verified"),
        _cap("e2", "点A", op="revoke", evidence="unknown",
             at="2026-09-02T10:00:00"),
    ])
    assert view["capabilities"]["python"]["mastered"] == []
    view, _ = _fold([
        _cap("e1", "点A", evidence="verified"),
        _cap("e2", "点A", op="revoke", evidence="unknown",
             at="2026-09-02T10:00:00"),
        _cap("e3", "点A", evidence="claimed", at="2026-09-03T10:00:00"),
    ])
    mastered = view["capabilities"]["python"]["mastered"]
    assert len(mastered) == 1
    assert mastered[0]["evidence"] == "claimed"


def test_revoke_only_tombstone_has_no_mastered_entry():
    view, _ = _fold([_cap("e1", "点A", op="revoke", evidence="unknown")])
    assert view["capabilities"]["python"]["mastered"] == []


def test_diagnostic_latest_event_replaces_not_accumulates():
    """重复 generate_roadmap 会重复写诊断；折叠必须取最近一次，不能累加。"""
    def diag(event_id, asked, right, at):
        return mem.make_event("diagnostic_recorded", at=at, session="t",
                              source="system", event_id=event_id,
                              tech="python", asked=asked, right=right)
    view, _ = _fold([
        diag("e1", 2, 1, "2026-09-01T10:00:00"),
        diag("e2", 2, 2, "2026-09-01T10:01:00"),
    ])
    assert view["capabilities"]["python"]["diagnostics"]["asked"] == 2
    assert view["capabilities"]["python"]["diagnostics"]["right"] == 2


# ---------- 偏好 / 事实 / 目标 ----------

def test_same_key_new_text_supersedes_and_preserves_created_at():
    view, _ = _fold([
        _profile("e1", "preference", "analogy", "不要用类比",
                 at="2026-09-01T10:00:00"),
        _profile("e2", "preference", "analogy", "还是用点类比",
                 at="2026-10-01T10:00:00"),
    ])
    assert len(view["preferences"]) == 1
    entry = view["preferences"][0]
    assert entry["text"] == "还是用点类比"
    assert entry["created_at"] == "2026-09-01"
    assert entry["last_confirmed"] == "2026-10-01"
    assert entry["supersedes"] == "e1"
    assert entry["source_event_id"] == "e2"


def test_same_key_same_text_only_refreshes_confirm_time():
    view, _ = _fold([
        _profile("e1", "preference", "analogy", "不要用类比",
                 at="2026-09-01T10:00:00"),
        _profile("e2", "preference", "analogy", "不要用类比",
                 at="2026-10-01T10:00:00"),
    ])
    entry = view["preferences"][0]
    assert entry["last_confirmed"] == "2026-10-01"
    assert entry["supersedes"] is None
    assert entry["source_event_id"] == "e2"


def test_global_and_tech_same_key_are_separate_entries():
    view, _ = _fold([
        _profile("e1", "preference", "analogy", "全局不要类比"),
        _profile("e2", "preference", "analogy", "本技术可以类比",
                 scope="tech", tech="python"),
    ])
    assert len(view["preferences"]) == 2
    ids = {e["id"] for e in view["preferences"]}
    assert ids == {"pref:global:analogy", "pref:tech:python:analogy"}


def test_goal_is_one_per_scope_and_tech():
    view, _ = _fold([
        _profile("e1", "goal", "goal", "目标A", scope="tech", tech="python"),
        _profile("e2", "goal", "goal", "目标B", scope="tech", tech="python",
                 at="2026-10-01T10:00:00"),
    ])
    rows = view["goals"]["by_tech"]["python"]
    assert len(rows) == 1
    assert rows[0]["text"] == "目标B"
    assert rows[0]["supersedes"] == "e1"


def test_forgotten_removes_profile_entry():
    view, _ = _fold([
        _profile("e1", "preference", "analogy", "不要用类比"),
        mem.make_event("memory_forgotten", at="2026-09-02T10:00:00", session="t",
                       source="user", event_id="e2",
                       target_id="pref:global:analogy"),
    ])
    assert view["preferences"] == []


# ---------- 未决事项 ----------

def test_open_item_id_generated_and_close_removes():
    view, _ = _fold([_open("e1", "下次讲装饰器")])
    item_id = view["open_items"][0]["id"]
    assert item_id.startswith("open:python:")
    view, _ = _fold([
        _open("e1", "下次讲装饰器"),
        _open("e2", "下次讲装饰器", op="close", item_id=item_id,
              at="2026-09-02T10:00:00"),
    ])
    assert view["open_items"] == []


# ---------- 上限与归档 ----------

def test_caps_archive_oldest_and_keep_view_bounded():
    events = [
        _profile("e1", "preference", "analogy", "旧偏好",
                 at="2026-09-01T10:00:00"),
        _profile("e2", "preference", "pace", "新偏好",
                 at="2026-10-01T10:00:00"),
    ]
    view, archived = _fold(events, caps={"max_preferences": 1})
    assert len(view["preferences"]) == 1
    assert view["preferences"][0]["key"] == "pace"
    assert len(archived) == 1
    assert archived[0]["kind"] == "preference"
    assert archived[0]["key"] == "analogy"


def test_caps_mastered_per_tech():
    events = [_cap(f"e{i}", f"点{i}", at=f"2026-09-0{i}T10:00:00")
              for i in range(1, 4)]
    view, archived = _fold(events, caps={"max_mastered_per_tech": 2})
    points = [m["point"] for m in view["capabilities"]["python"]["mastered"]]
    assert points == ["点2", "点3"]
    assert [a["point"] for a in archived if a["kind"] == "capability"] == ["点1"]


# ---------- 确定性 ----------

def test_fold_is_deterministic():
    events = [
        _cap("e1", "点A"),
        _profile("e2", "fact", "identity:role", "后端开发"),
        _open("e3", "下次讲装饰器"),
    ]
    a, _ = _fold(events)
    b, _ = _fold(events)
    assert a == b


# ---------- 校验与时间 ----------

def test_validate_profile_item():
    assert mem.validate_profile_item(
        "preference", "global", "", "analogy", "不要类比") is None
    assert "偏好 key" in mem.validate_profile_item(
        "preference", "global", "", "not_a_key", "x")
    assert "ASCII slug" in mem.validate_profile_item(
        "fact", "global", "", "中文key", "x")
    assert "tech 必填" in mem.validate_profile_item(
        "fact", "tech", "", "constraint:env", "x")
    assert "固定为 goal" in mem.validate_profile_item(
        "goal", "global", "", "other", "x")


def test_parse_time_tolerates_naive_and_roadmap_format():
    assert mem.parse_time("2026-09-01 13:45") is not None
    dt = mem.parse_time("2026-09-01T13:45:00")
    assert dt.tzinfo is not None
    assert mem.parse_time("not-a-time") is None
