"""adapters/memory 单测：迁移 / 对账 / 重建 / 容错 / 幂等（全部零网络）。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_store.py -v
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.adapters import memory as store
from src.config import config
from src.domain import memory as mem

NOW = "2026-11-18T10:00:00"


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


def _write_profile(profile: dict) -> None:
    config.LEARNER_DIR.mkdir(parents=True, exist_ok=True)
    (config.LEARNER_DIR / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8")


def _write_roadmap(tech: str, milestones: list[tuple[str, bool]], *,
                   updated_at: str = "2026-09-01 10:00") -> None:
    config.ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
    roadmap = {
        "tech": tech,
        "goal": "g",
        "total_hours": 10,
        "stages": [{
            "id": "s1", "name": "阶段", "goal": "g", "est_hours": 5,
            "milestones": [
                {"id": f"s1-m{i}", "desc": desc, "done": done}
                for i, (desc, done) in enumerate(milestones, 1)
            ],
        }],
        "current_stage": "s1",
        "status": "active",
        "created_at": "2026-09-01 09:00",
        "updated_at": updated_at,
    }
    (config.ROADMAP_DIR / f"{mem.tech_key(tech)}.json").write_text(
        json.dumps(roadmap, ensure_ascii=False), encoding="utf-8")


def _event_lines() -> list[dict]:
    path = store.events_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------- 迁移 ----------

def test_bootstrap_migrates_profile_and_done_milestones(dirs):
    _write_profile({"python": {
        "tech": "Python", "self_level": 4, "related": "熟悉 Java",
        "goal": "看懂项目代码", "time_budget": "1", "bucket": "intermediate",
        "roadmap_path": "x",
    }})
    _write_roadmap("Python", [("能写函数", True), ("能写类", False)])

    view = store.ensure_view(now=NOW)
    cap = view["capabilities"]["python"]
    assert cap["tech"] == "Python"
    assert cap["route_present"] is True
    assert "完成 1/2 里程碑" in cap["summary"]
    assert [m["point"] for m in cap["mastered"]] == ["能写函数"]
    assert cap["mastered"][0]["evidence"] == "unknown"

    facts = {e["key"]: e["text"] for e in view["facts"]
             if e.get("tech_key") == "python"}
    assert facts["self_level"] == "4"
    assert facts["related"] == "熟悉 Java"
    assert facts["time_budget"] == "1"
    assert view["goals"]["by_tech"]["python"][0]["text"] == "看懂项目代码"
    # bucket 与 roadmap_path 不迁移
    assert "bucket" not in facts
    assert "roadmap_path" not in facts
    assert all(e["source"] == "migration" for e in _event_lines())


def test_bootstrap_is_idempotent(dirs):
    _write_profile({"python": {"tech": "Python", "self_level": 3,
                               "goal": "g", "related": "r",
                               "time_budget": "1"}})
    _write_roadmap("Python", [("点A", True)])
    store.ensure_view(now=NOW)
    first = len(_event_lines())
    store.ensure_view(now=NOW)
    assert len(_event_lines()) == first


def test_missing_profile_and_roadmaps_yields_empty_view(dirs):
    view = store.ensure_view(now=NOW)
    assert mem.view_is_empty(view)


def test_bootstrap_runs_when_data_appears_after_empty_view(dirs):
    """首次启用时还没有数据，之后 profile/roadmap 出现：仍应补迁移。"""
    assert mem.view_is_empty(store.ensure_view(now=NOW))
    _write_profile({"python": {"tech": "Python", "self_level": 3,
                               "goal": "g", "related": "r",
                               "time_budget": "1"}})
    _write_roadmap("Python", [("点A", True)])
    view = store.ensure_view(now=NOW)
    assert view["capabilities"]["python"]["mastered"]


# ---------- 对账 ----------

def test_reconcile_backfills_newly_done_milestone(dirs):
    _write_roadmap("Python", [("点A", False)])
    store.ensure_view(now=NOW)
    assert store.ensure_view(now=NOW)["capabilities"]["python"]["mastered"] == []

    _write_roadmap("Python", [("点A", True)], updated_at="2026-10-01 10:00")
    view = store.ensure_view(now=NOW)
    assert [m["point"] for m in view["capabilities"]["python"]["mastered"]] == ["点A"]


def test_reconcile_revokes_when_roadmap_undone(dirs):
    _write_roadmap("Python", [("点A", True)])
    store.ensure_view(now=NOW)
    _write_roadmap("Python", [("点A", False)], updated_at="2026-10-01 10:00")
    view = store.ensure_view(now=NOW)
    assert view["capabilities"]["python"]["mastered"] == []


def test_reconcile_tombstone_wins_over_roadmap_done(dirs):
    """用户 revoke 后，路线里的 done=true 不能再把能力加回来。"""
    _write_roadmap("Python", [("点A", True)])
    store.ensure_view(now=NOW)
    store.record_events([mem.make_event(
        "capability_recorded", at="2026-10-01T10:00:00", session="t",
        source="system", tech="Python", point="点A", op="revoke")])
    view = store.ensure_view(now=NOW)
    assert view["capabilities"]["python"]["mastered"] == []
    # 再跑一次对账仍然不补
    view = store.ensure_view(now=NOW)
    assert view["capabilities"]["python"]["mastered"] == []


# ---------- 重建与容错 ----------

def test_rebuild_after_view_deleted_is_identical(dirs):
    _write_profile({"python": {"tech": "Python", "self_level": 4,
                               "goal": "g", "related": "r",
                               "time_budget": "1"}})
    _write_roadmap("Python", [("点A", True)])
    first = store.ensure_view(now=NOW)
    store.view_path().unlink()
    second = store.ensure_view(now=NOW)
    assert first == second


def test_events_file_loss_recovers_active_state_from_view(dirs):
    """事件日志被删：从视图重建最小事件集，active 记忆不能被空日志覆盖掉。"""
    _write_profile({"python": {"tech": "Python", "self_level": 4,
                               "goal": "看懂项目代码", "related": "熟悉 Java",
                               "time_budget": "1"}})
    _write_roadmap("Python", [("点A", True)])
    before = store.ensure_view(now=NOW)
    store.events_path().unlink()

    after = store.ensure_view(now=NOW)
    assert {e["key"] for e in after["facts"]} == {e["key"] for e in before["facts"]}
    assert [m["point"] for m in after["capabilities"]["python"]["mastered"]] == ["点A"]
    assert after["goals"]["by_tech"]["python"][0]["text"] == "看懂项目代码"
    assert store.events_path().exists()
    assert all(e["session"] == "recovery" for e in _event_lines())


def test_record_events_after_log_loss_keeps_old_and_new(dirs):
    _write_profile({"python": {"tech": "Python", "self_level": 4,
                               "goal": "g", "related": "r",
                               "time_budget": "1"}})
    store.ensure_view(now=NOW)
    store.events_path().unlink()

    store.record_events([mem.make_event(
        "profile_recorded", at="2026-10-01T10:00:00", session="t",
        source="user", category="preference", scope="global", key="analogy",
        text="不要用类比")])
    view = store.ensure_view(now=NOW)
    assert {e["key"] for e in view["facts"]} == {"self_level", "related", "time_budget"}
    assert view["preferences"][0]["text"] == "不要用类比"


def test_corrupt_view_is_rebuilt_from_events(dirs):
    _write_roadmap("Python", [("点A", True)])
    store.ensure_view(now=NOW)
    store.view_path().write_text("{not json", encoding="utf-8")
    view = store.ensure_view(now=NOW)
    assert view["capabilities"]["python"]["mastered"]


def test_corrupt_event_line_is_skipped(dirs):
    _write_roadmap("Python", [("点A", True)])
    store.ensure_view(now=NOW)
    with store.events_path().open("a", encoding="utf-8") as fh:
        fh.write("{broken line\n")
    view = store.ensure_view(now=NOW)
    assert view["capabilities"]["python"]["mastered"]


def test_atomic_write_leaves_no_temp_file_after_success(dirs):
    _write_roadmap("Python", [("点A", True)])
    store.ensure_view(now=NOW)
    temps = list(config.LEARNER_DIR.glob("memory.json.tmp.*"))
    assert temps == []


# ---------- 写入与归档 ----------

def test_record_events_appends_and_folds_immediately(dirs):
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", category="preference", scope="global", key="analogy",
        text="不要用类比")])
    view = store.ensure_view(now=NOW)
    assert view["preferences"][0]["text"] == "不要用类比"


def test_cap_overflow_writes_archive(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_MAX_PREFERENCES", 1)
    store.record_events([
        mem.make_event("profile_recorded", at="2026-09-01T10:00:00", session="t",
                       source="user", category="preference", scope="global",
                       key="analogy", text="旧偏好"),
        mem.make_event("profile_recorded", at="2026-10-01T10:00:00", session="t",
                       source="user", category="preference", scope="global",
                       key="pace", text="新偏好"),
    ])
    view = store.ensure_view(now=NOW)
    assert [e["key"] for e in view["preferences"]] == ["pace"]
    archived = [json.loads(line) for line in
                store.archive_path().read_text(encoding="utf-8").splitlines()]
    assert archived[0]["key"] == "analogy"
    assert archived[0]["kind"] == "preference"


def test_ltm_disabled_skips_all_io(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_ENABLED", False)
    view = store.ensure_view(now=NOW)
    assert mem.view_is_empty(view)
    assert not store.events_path().exists()
    assert not store.view_path().exists()


# ---------- Phase 3：forget / archive / rebuild ----------

def test_forget_preference_is_logical_delete(dirs):
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", category="preference", scope="global", key="analogy",
        text="不要用类比")])
    result = store.forget("pref:global:analogy", now=NOW)
    assert result["status"] == "ok" and result["kind"] == "preference"
    assert store.ensure_view(now=NOW)["preferences"] == []
    # 逻辑删除：原始事件仍在日志里
    types = [e["type"] for e in _event_lines()]
    assert "profile_recorded" in types and "memory_forgotten" in types
    # 再删一次：已经不在 active 视图里
    assert store.forget("pref:global:analogy", now=NOW)["status"] == "error"


def test_forget_goal_and_open_item(dirs):
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", category="goal", scope="tech", tech="Python",
        key="goal", text="看懂代码")])
    assert store.forget("goal:tech:python", now=NOW)["status"] == "ok"
    assert store.ensure_view(now=NOW)["goals"]["by_tech"] == {}

    item_id = mem.open_item_id("Python", "下次讲装饰器")
    store.record_events([mem.make_event(
        "open_item_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", op="open", tech="Python", item_id=item_id,
        text="下次讲装饰器")])
    assert store.forget(item_id, now=NOW)["status"] == "ok"
    assert store.ensure_view(now=NOW)["open_items"] == []


def test_forget_rejects_capability_and_unknown_id(dirs):
    _write_roadmap("Python", [("点A", True)])
    view = store.ensure_view(now=NOW)
    cap_id = view["capabilities"]["python"]["mastered"][0]["id"]
    out = store.forget(cap_id, now=NOW)
    assert out["status"] == "error" and "update_roadmap" in out["error"]
    assert store.ensure_view(now=NOW)["capabilities"]["python"]["mastered"]

    assert store.forget("pref:global:missing", now=NOW)["status"] == "error"


def test_forget_archived_entry_keeps_it_out_of_hot_view(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_MAX_PREFERENCES", 1)
    store.record_events([
        mem.make_event("profile_recorded", at="2026-09-01T10:00:00", session="t",
                       source="user", category="preference", scope="global",
                       key="analogy", text="旧偏好"),
        mem.make_event("profile_recorded", at="2026-10-01T10:00:00", session="t",
                       source="user", category="preference", scope="global",
                       key="pace", text="新偏好"),
    ])
    out = store.forget("pref:global:analogy", now=NOW)
    assert out["status"] == "ok" and out["from_archive"] is True
    # 再删掉当前唯一的热条目，旧归档条目也不能回到热视图
    store.forget("pref:global:pace", now=NOW)
    assert store.ensure_view(now=NOW)["preferences"] == []


def test_forget_then_new_event_reactivates(dirs):
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", category="preference", scope="global", key="analogy",
        text="不要用类比")])
    store.forget("pref:global:analogy", now=NOW)
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-10-01T10:00:00", session="t",
        source="user", category="preference", scope="global", key="analogy",
        text="还是用点类比")])
    prefs = store.ensure_view(now=NOW)["preferences"]
    assert len(prefs) == 1 and prefs[0]["text"] == "还是用点类比"


def test_event_texts_reads_superseded_old_value(dirs):
    store.record_events([
        mem.make_event("profile_recorded", at="2026-09-01T10:00:00", session="t",
                       source="user", event_id="e1", category="preference",
                       scope="global", key="analogy", text="不要用类比"),
        mem.make_event("profile_recorded", at="2026-10-01T10:00:00", session="t",
                       source="user", event_id="e2", category="preference",
                       scope="global", key="analogy", text="还是用点类比"),
    ])
    entry = store.ensure_view()["preferences"][0]
    assert entry["supersedes"] == "e1"
    assert store.event_texts({entry["supersedes"]}) == {"e1": "不要用类比"}


def test_forgotten_ids_tracks_logical_delete(dirs):
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", category="preference", scope="global", key="analogy",
        text="不要用类比")])
    assert store.forgotten_ids() == set()
    store.forget("pref:global:analogy", now=NOW)
    assert store.forgotten_ids() == {"pref:global:analogy"}


def test_load_archive(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_MAX_PREFERENCES", 1)
    store.record_events([
        mem.make_event("profile_recorded", at="2026-09-01T10:00:00", session="t",
                       source="user", category="preference", scope="global",
                       key="analogy", text="旧偏好"),
        mem.make_event("profile_recorded", at="2026-10-01T10:00:00", session="t",
                       source="user", category="preference", scope="global",
                       key="pace", text="新偏好"),
    ])
    rows, bad = store.load_archive()
    assert bad == 0
    assert len(rows) == 1 and rows[0]["key"] == "analogy"


def test_debug_dump_writes_prompt_boundary(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_DEBUG_DUMP", True)
    blocks = {"briefing": "全局偏好与约束\n- 偏好[analogy]：不要用类比"}
    ids = ["pref:global:analogy"]
    dropped = [{"id": "fact:tech:python:related", "kind": "fact",
                "reason": "covered"}]
    ltm_text = "RULE\n" + blocks["briefing"]
    prompt = "SYSTEM\n\n" + ltm_text
    store.dump_prompt_injection(
        mode="survey", tech="Python", session="t-1",
        prompt=prompt, ltm_text=ltm_text, blocks=blocks,
        section_lengths={"identity": 23, "background": 0}, ids=ids, turn=3,
        dropped=dropped, truncated=False)
    path = config.AUDIT_LOG_DIR / "ltm_inject.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["schema"] == 3
    assert row["mode"] == "survey" and row["tech"] == "Python"
    assert row["session"] == "t-1"
    assert row["turn"] == 3
    assert row["has_ltm"] is True
    assert row["ltm_chars"] == len(ltm_text)
    assert row["ltm_text"] == ltm_text
    assert row["ltm_appended"] is True
    assert row["prompt_chars"] == len(prompt)
    assert row["blocks"] == blocks
    assert row["block_lengths"] == {k: len(v) for k, v in blocks.items()}
    assert row["section_lengths"] == {"identity": 23, "background": 0}
    assert "pref:global:analogy" in row["ids"]
    assert row["dropped"] == dropped
    assert row["truncated"] is False


def test_debug_dump_off_by_default(dirs):
    store.dump_prompt_injection(
        mode="survey", tech="Python", session="t-1",
        prompt="SYSTEM", ltm_text="", blocks={}, ids=[])
    assert not (config.AUDIT_LOG_DIR / "ltm_inject.jsonl").exists()


def test_explicit_rebuild_restores_view(dirs):
    store.record_events([mem.make_event(
        "profile_recorded", at="2026-09-01T10:00:00", session="t",
        source="user", category="fact", scope="global", key="identity:role",
        text="后端开发")])
    store.view_path().write_text('{"capabilities": {}}', encoding="utf-8")
    view = store.rebuild(now=NOW)
    assert view["facts"][0]["text"] == "后端开发"
