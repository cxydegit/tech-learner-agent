"""按需记忆访问（5.1）：结构化过滤、历史状态、时间线、forget 语义。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.adapters import memory as store
from src.adapters import memory_search as search_store
from src.config import config
from src.domain import memory as mem
from src.domain import memory_search as search


def _record(**kwargs):
    base = {
        "id": "x", "kind": "capability", "text": "point", "tech": "Python",
        "tech_key": "python", "scope": "tech", "key": None, "evidence": None,
        "status": "active", "date": "2026-11-01", "source": "view",
    }
    base.update(kwargs)
    return base


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    learner_dir = tmp_path / "learner"
    roadmap_dir = tmp_path / "roadmaps"
    learner_dir.mkdir()
    roadmap_dir.mkdir()
    monkeypatch.setattr(config, "LEARNER_DIR", learner_dir)
    monkeypatch.setattr(config, "ROADMAP_DIR", roadmap_dir)
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    return tmp_path


def test_filter_records_by_tech_kind_status_and_query():
    records = [
        _record(id="cap", tech="Python", tech_key="python",
                text="字符串切片规则", status="active"),
        _record(id="pref", kind="preference", tech="", tech_key="",
                scope="global", text="不要用类比", status="active"),
        _record(id="old", kind="preference", tech="", tech_key="",
                scope="global", text="旧偏好", status="superseded",
                date="2026-10-01"),
    ]
    assert [r["id"] for r in search.filter_records(
        records, tech="Python", kinds=["capability"])] == ["cap"]
    assert {r["id"] for r in search.filter_records(
        records, tech="Python", statuses=["active"])} == {"cap", "pref"}
    assert [r["id"] for r in search.filter_records(
        records, query="类比", statuses=["active"])] == ["pref"]
    assert [r["id"] for r in search.filter_records(
        records, statuses=["superseded"])] == ["old"]
    # active 排在历史前
    assert [r["id"] for r in search.filter_records(records, limit=10)] == [
        "cap", "pref", "old"]


def test_filter_records_tech_includes_global_entries():
    records = [
        _record(id="global", kind="preference", tech="", tech_key="",
                scope="global", text="不要用类比"),
        _record(id="local", kind="preference", tech="Python", tech_key="python",
                scope="tech", text="时间紧"),
        _record(id="other", kind="preference", tech="React", tech_key="react",
                scope="tech", text="先看例子"),
    ]
    assert {r["id"] for r in search.filter_records(
        records, tech="Python")} == {"global", "local"}


def test_filter_records_date_range_is_inclusive_for_date_only():
    records = [
        _record(id="a", date="2026-11-01T10:00:00+08:00"),
        _record(id="b", date="2026-11-22T10:00:00+08:00"),
        _record(id="c", date="2026-11-23T10:00:00+08:00"),
    ]
    assert [r["id"] for r in search.filter_records(
        records, date_from="2026-11-22", date_to="2026-11-22")] == ["b"]


def test_render_records_drops_whole_items_not_text():
    long_text = "很长的一条记忆" * 20
    records = [
        _record(id="a", text=long_text),
        _record(id="b", text="短记忆"),
    ]
    text, used = search.render_records(records, max_chars=40)
    assert used == []
    assert "单条超过结果预算" in text
    short_text, short_used = search.render_records(
        [records[1]], max_chars=200)
    assert short_used == [records[1]]
    assert "短记忆" in short_text


def test_filter_timeline_limits_to_recent_but_keeps_order():
    events = [
        {"id": "1", "date": "2026-11-01", "tech_key": "python", "text": "a"},
        {"id": "2", "date": "2026-11-02", "tech_key": "python", "text": "b"},
        {"id": "3", "date": "2026-11-03", "tech_key": "react", "text": "c"},
    ]
    hits = search.filter_timeline(events, tech="Python", limit=1)
    assert [e["id"] for e in hits] == ["2"]


def test_adapter_records_include_history_and_exclude_forgotten(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_MAX_PREFERENCES", 50)
    monkeypatch.setattr(config, "LTM_MAX_FACTS", 50)
    monkeypatch.setattr(config, "LTM_MAX_MASTERED_PER_TECH", 40)
    monkeypatch.setattr(config, "LTM_MAX_OPEN_ITEMS", 50)
    monkeypatch.setattr(config, "LTM_MAX_GOALS", 20)
    old_pref = mem.make_event(
        "profile_recorded", at="2026-10-01", session="t", source="user",
        category="preference", scope="global", key="analogy", text="旧偏好")
    new_pref = mem.make_event(
        "profile_recorded", at="2026-10-02", session="t", source="user",
        category="preference", scope="global", key="analogy", text="新偏好")
    archived_pref = mem.make_event(
        "profile_recorded", at="2026-10-03", session="t", source="user",
        category="preference", scope="global", key="verbosity", text="归档偏好")
    cap_add = mem.make_event(
        "capability_recorded", at="2026-10-04", session="t", source="system",
        tech="Python", point="能写函数", op="add", evidence="verified")
    cap_revoke = mem.make_event(
        "capability_recorded", at="2026-10-05", session="t", source="user",
        tech="Python", point="能写函数", op="revoke")
    open_text = "下次讲异常"
    item_id = mem.open_item_id("Python", open_text)
    open_event = mem.make_event(
        "open_item_recorded", at="2026-10-06", session="t", source="user",
        tech="Python", item_id=item_id, text=open_text, op="open")
    close_event = mem.make_event(
        "open_item_recorded", at="2026-10-07", session="t", source="user",
        tech="Python", item_id=item_id, op="close")
    forget_pref = mem.make_event(
        "profile_recorded", at="2026-10-08", session="t", source="user",
        category="fact", scope="global", key="constraint:secret",
        text="不该被召回")
    store.record_events([old_pref, new_pref, archived_pref, cap_add,
                         cap_revoke, open_event, close_event, forget_pref])
    store.forget("fact:global:constraint:secret")

    records = search_store.load_records()
    by_id = {r["id"]: r for r in records}
    assert by_id["pref:global:analogy"]["status"] == "active"
    assert by_id["pref:global:analogy"]["text"] == "新偏好"
    assert any(r["kind"] == "preference" and r["status"] == "superseded"
               and r["text"] == "旧偏好" for r in records)
    assert any(r["kind"] == "capability" and r["status"] == "revoked"
               and r["text"] == "能写函数" for r in records)
    assert any(r["kind"] == "open_item" and r["status"] == "closed"
               and r["text"] == open_text for r in records)
    assert all(r["text"] != "不该被召回" for r in records)


def test_adapter_archive_records_are_visible(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_MAX_PREFERENCES", 1)
    monkeypatch.setattr(config, "LTM_MAX_FACTS", 50)
    monkeypatch.setattr(config, "LTM_MAX_MASTERED_PER_TECH", 40)
    monkeypatch.setattr(config, "LTM_MAX_OPEN_ITEMS", 50)
    monkeypatch.setattr(config, "LTM_MAX_GOALS", 20)
    old = mem.make_event(
        "profile_recorded", at="2026-10-01", session="t", source="user",
        category="preference", scope="global", key="analogy", text="旧偏好")
    new = mem.make_event(
        "profile_recorded", at="2026-10-02", session="t", source="user",
        category="preference", scope="global", key="verbosity", text="新偏好")
    store.record_events([old, new])
    records = search_store.load_records()
    archived = [r for r in records if r["status"] == "archived"]
    assert archived and archived[0]["text"] == "旧偏好"


def test_adapter_timeline_excludes_forgotten_text_but_shows_forget_event(dirs):
    event = mem.make_event(
        "profile_recorded", at="2026-10-01", session="t", source="user",
        category="fact", scope="global", key="constraint:secret",
        text="不该出现在时间线")
    store.record_events([event])
    store.forget("fact:global:constraint:secret")
    events = search_store.load_timeline()
    assert all("不该出现在时间线" not in (e.get("text") or "") for e in events)
    assert any(e.get("type") == "forgotten"
               and "fact:global:constraint:secret" in (e.get("text") or "")
               for e in events)


def test_filter_records_by_evidence():
    records = [
        _record(id="verified", evidence="verified"),
        _record(id="unknown", evidence="unknown"),
    ]
    assert [r["id"] for r in search.filter_records(
        records, evidence=["unknown"])] == ["unknown"]
    assert {r["id"] for r in search.filter_records(
        records, evidence=["any"])} == {"verified", "unknown"}


def test_filter_records_by_key():
    records = [
        _record(id="level", kind="fact", key="self_level", text="5"),
        _record(id="budget", kind="fact", key="time_budget", text="1"),
    ]
    assert [r["id"] for r in search.filter_records(
        records, keys=["self_level"])] == ["level"]
    assert {r["id"] for r in search.filter_records(
        records, keys=["self_level", "time_budget"])} == {"level", "budget"}


def test_filter_records_tech_alias_matches_spring_ai():
    records = [
        _record(id="spring", tech="SpringAI", tech_key="springai"),
        _record(id="other", tech="React", tech_key="react"),
    ]
    assert [r["id"] for r in search.filter_records(
        records, tech="Spring AI")] == ["spring"]
