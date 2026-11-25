"""Phase 2：remember 工具 / 里程碑证据写入 / 问卷画像同步（全部零网络）。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_tools.py -v
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.adapters import memory as store
from src.config import config
from src.domain import roadmap as rm
from src.pipelines import route as route_mod
from src.pipelines.route import CoachCtx, run_coach_tool


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


def _ctx(state=None, tech="Python"):
    base = {"tech": tech, "survey_answers": {}, "learner_profile": {},
            "coach_messages": [], "conversation": []}
    base.update(state or {})
    return CoachCtx(base, thread_id="t-1")


def _roadmap(tech="Python", descs=("能写函数",)):
    stages, _ = rm.normalize_stages([{
        "name": "阶段", "goal": "g", "est_hours": 2,
        "milestones": [{"desc": d} for d in descs],
    }])
    return rm.build_roadmap(tech, "g", 2, stages)


def _events():
    return [json.loads(line) for line in
            store.events_path().read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _gen_args():
    return {
        "goal": "能跑通最小项目", "total_hours": 12, "revision": "",
        "stages": [{"name": "环境搭建", "goal": "跑通 hello", "est_hours": 4,
                    "milestones": [{"desc": "安装完成"}]}],
    }


# ============================================================
# remember：profile
# ============================================================


def test_remember_preference_creates_entry(dirs):
    out = run_coach_tool("remember", {
        "kind": "profile", "category": "preference", "scope": "global",
        "key": "analogy", "text": "不喜欢用类比",
    }, _ctx())
    assert out["status"] == "ok" and out["action"] == "created"
    assert out["id"] == "pref:global:analogy"
    view = store.ensure_view()
    assert view["preferences"][0]["text"] == "不喜欢用类比"
    event = _events()[-1]
    assert event["type"] == "profile_recorded" and event["source"] == "user"


def test_remember_invalid_key_writes_nothing(dirs):
    out = run_coach_tool("remember", {
        "kind": "profile", "category": "preference", "scope": "global",
        "key": "not_a_key", "text": "x",
    }, _ctx())
    assert out["status"] == "error"
    assert "偏好 key" in out["error"]
    assert not store.events_path().exists()


def test_remember_fact_requires_ascii_slug(dirs):
    out = run_coach_tool("remember", {
        "kind": "profile", "category": "fact", "scope": "global",
        "key": "中文key", "text": "x",
    }, _ctx())
    assert out["status"] == "error"
    assert "ASCII slug" in out["error"]


def test_remember_tech_scope_requires_matching_tech(dirs):
    missing = run_coach_tool("remember", {
        "kind": "profile", "category": "fact", "scope": "tech",
        "key": "constraint:env", "text": "不能装 Docker",
    }, _ctx())
    assert missing["status"] == "error" and "需要 tech" in missing["error"]

    mismatch = run_coach_tool("remember", {
        "kind": "profile", "category": "fact", "scope": "tech",
        "tech": "Rust", "key": "constraint:env", "text": "不能装 Docker",
    }, _ctx())
    assert mismatch["status"] == "error" and "当前技术" in mismatch["error"]


def test_remember_goal_supersedes_and_reports_conflict(dirs):
    first = run_coach_tool("remember", {
        "kind": "profile", "category": "goal", "scope": "tech",
        "tech": "Python", "key": "goal", "text": "看懂项目代码",
    }, _ctx())
    assert first["action"] == "created"

    second = run_coach_tool("remember", {
        "kind": "profile", "category": "goal", "scope": "tech",
        "tech": "Python", "key": "goal", "text": "独立写个小工具",
    }, _ctx())
    assert second["action"] == "updated"
    assert "看懂项目代码" in second["conflict"]
    assert "独立写个小工具" in second["conflict"]
    view = store.ensure_view()
    rows = view["goals"]["by_tech"]["python"]
    assert len(rows) == 1 and rows[0]["text"] == "独立写个小工具"
    assert rows[0]["supersedes"]


def test_remember_same_text_is_idempotent(dirs):
    args = {"kind": "profile", "category": "preference", "scope": "global",
            "key": "analogy", "text": "不喜欢用类比"}
    run_coach_tool("remember", args, _ctx())
    out = run_coach_tool("remember", args, _ctx())
    assert out["action"] == "confirmed" and out["conflict"] is None
    assert len(store.ensure_view()["preferences"]) == 1


# ============================================================
# remember：open_item
# ============================================================


def test_remember_open_item_open_and_close(dirs):
    out = run_coach_tool("remember", {
        "kind": "open_item", "op": "open", "tech": "Python",
        "text": "下次讲装饰器",
    }, _ctx())
    assert out["status"] == "ok" and out["action"] == "opened"
    item_id = out["item_id"]
    assert item_id.startswith("open:python:")
    assert out["expires_at"]
    view = store.ensure_view()
    assert view["open_items"][0]["id"] == item_id

    closed = run_coach_tool("remember", {
        "kind": "open_item", "op": "close", "tech": "Python",
        "item_id": item_id,
    }, _ctx())
    assert closed["status"] == "ok" and closed["action"] == "closed"
    assert store.ensure_view()["open_items"] == []


def test_remember_open_item_rejects_foreign_id(dirs):
    out = run_coach_tool("remember", {
        "kind": "open_item", "op": "close", "tech": "Python",
        "item_id": "open:rust:abcdef1234",
    }, _ctx())
    assert out["status"] == "error" and "不属于当前技术" in out["error"]


def test_remember_open_item_custom_expiry(dirs):
    out = run_coach_tool("remember", {
        "kind": "open_item", "op": "open", "tech": "Python",
        "text": "下次讲装饰器", "expires_at": "2026-12-01T00:00:00",
    }, _ctx())
    assert out["status"] == "ok"
    assert out["expires_at"].startswith("2026-12-01")
    assert store.ensure_view()["open_items"][0]["expires_at"].startswith("2026-12-01")


def test_remember_disabled_returns_error(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_ENABLED", False)
    out = run_coach_tool("remember", {
        "kind": "profile", "category": "preference", "scope": "global",
        "key": "analogy", "text": "x",
    }, _ctx())
    assert out["status"] == "error" and "已关闭" in out["error"]


# ============================================================
# 里程碑证据写入
# ============================================================


def _update_ctx(roadmap, user_text="继续"):
    return CoachCtx({
        "tech": roadmap["tech"], "roadmap": roadmap,
        "coach_messages": [{"role": "user", "content": user_text}],
        "conversation": [{"role": "user", "type": "chat", "content": user_text}],
    }, thread_id="t-1")


def _verify(verified):
    return lambda *a, **k: {"verified": verified, "missing": [],
                            "reason": "test", "suggested_id": None}


def test_update_roadmap_writes_verified_event(dirs, monkeypatch):
    monkeypatch.setattr(route_mod, "verify_milestone", _verify(True))
    ctx = _update_ctx(_roadmap())
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    mastered = store.ensure_view()["capabilities"]["python"]["mastered"]
    assert len(mastered) == 1 and mastered[0]["evidence"] == "verified"
    assert _events()[-1]["source"] == "system"
    assert _events()[-1]["op"] == "add"


def test_update_roadmap_completion_claim_is_claimed(dirs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("豁免时不应调用验收")
    monkeypatch.setattr(route_mod, "verify_milestone", boom)
    ctx = _update_ctx(_roadmap(), user_text="我搞定了")
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert store.ensure_view()["capabilities"]["python"]["mastered"][0]["evidence"] == "claimed"


def test_update_roadmap_verify_disabled_is_claimed(dirs, monkeypatch):
    monkeypatch.setattr(config, "ROUTE_MILESTONE_VERIFY", False)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    ctx = _update_ctx(_roadmap())
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert store.ensure_view()["capabilities"]["python"]["mastered"][0]["evidence"] == "claimed"


def test_update_roadmap_verifier_unavailable_is_degraded(dirs, monkeypatch):
    monkeypatch.setattr(route_mod, "verify_milestone", _verify(None))
    ctx = _update_ctx(_roadmap())
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert store.ensure_view()["capabilities"]["python"]["mastered"][0]["evidence"] == "degraded"


def test_update_roadmap_uncheck_revokes_and_recheck_reactivates(dirs, monkeypatch):
    monkeypatch.setattr(route_mod, "verify_milestone", _verify(True))
    ctx = _update_ctx(_roadmap())
    run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert store.ensure_view()["capabilities"]["python"]["mastered"]

    run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": False}, ctx)
    assert store.ensure_view()["capabilities"]["python"]["mastered"] == []

    # Phase 2 的显式 add 事件能突破墓碑：重新验收后能力点恢复
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    mastered = store.ensure_view()["capabilities"]["python"]["mastered"]
    assert len(mastered) == 1 and mastered[0]["evidence"] == "verified"


def test_update_roadmap_rejected_writes_no_event(dirs, monkeypatch):
    monkeypatch.setattr(route_mod, "verify_milestone", _verify(False))
    ctx = _update_ctx(_roadmap())
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "rejected"
    assert store.ensure_view()["capabilities"].get("python", {}).get("mastered", []) == []


def test_update_roadmap_memory_failure_does_not_break_tool(dirs, monkeypatch):
    monkeypatch.setattr(route_mod, "verify_milestone", _verify(True))
    monkeypatch.setattr(route_mod.memory_store, "record_events",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    ctx = _update_ctx(_roadmap())
    out = run_coach_tool("update_roadmap",
                         {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"


# ============================================================
# 问卷画像同步
# ============================================================


def _profile():
    return {
        "tech": "Python", "self_level": 4, "related": "熟悉 Java",
        "goal": "看懂项目代码", "time_budget": "1", "bucket": "intermediate",
        "diagnostics": [{"grade": "right"}, {"grade": "wrong"}],
    }


def test_generate_roadmap_syncs_profile_and_diagnostics(dirs):
    ctx = CoachCtx({"tech": "Python", "learner_profile": _profile()},
                   thread_id="t-1")
    out = run_coach_tool("generate_roadmap", _gen_args(), ctx)
    assert out["status"] == "ok"

    view = store.ensure_view()
    facts = {e["key"]: e["text"] for e in view["facts"]
             if e.get("tech_key") == "python"}
    assert facts == {"self_level": "4", "related": "熟悉 Java", "time_budget": "1"}
    assert view["goals"]["by_tech"]["python"][0]["text"] == "看懂项目代码"
    diag = view["capabilities"]["python"]["diagnostics"]
    assert diag["asked"] == 2 and diag["right"] == 1


def test_generate_roadmap_repeated_diagnostics_not_accumulated(dirs):
    ctx = CoachCtx({"tech": "Python", "learner_profile": _profile()},
                   thread_id="t-1")
    run_coach_tool("generate_roadmap", _gen_args(), ctx)
    run_coach_tool("generate_roadmap", _gen_args(), ctx)
    view = store.ensure_view()
    diag = view["capabilities"]["python"]["diagnostics"]
    assert diag["asked"] == 2 and diag["right"] == 1
    assert len(view["goals"]["by_tech"]["python"]) == 1


# ============================================================
# 工具与提示词
# ============================================================


def test_remember_tool_available_in_all_modes():
    for mode in ("survey", "planning", "coaching"):
        names = [tool["function"]["name"]
                 for tool in route_mod.COACH_TOOLS_BY_MODE[mode]]
        assert "remember" in names


@pytest.mark.parametrize("builder", [
    route_mod._survey_prompt,
    route_mod._planning_prompt,
    route_mod._coaching_prompt,
])
def test_prompts_mention_remember(builder):
    assert "remember" in builder({}, "Python")


# ============================================================
# recall_memory：5.1 按需访问（结构化）
# ============================================================


def test_recall_tool_is_gated_by_flag(monkeypatch):
    # gating 两种状态都显式设置：默认值可改（当前默认开启），本用例只验证 flag 生效。
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", False)
    names = {t["function"]["name"] for t in route_mod.coach_tools_for_mode("coaching")}
    assert "recall_memory" not in names
    monkeypatch.setattr(config, "LTM_ENABLED", True)
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", True)
    names = {t["function"]["name"] for t in route_mod.coach_tools_for_mode("coaching")}
    assert "recall_memory" in names


def test_recall_tool_returns_revoked_capability(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", True)
    from src.adapters import memory as mem_store
    from src.domain import memory as mem
    mem_store.record_events([
        mem.make_event("capability_recorded", at="2026-10-01", session="t",
                       source="system", tech="Python", point="能写函数",
                       op="add", evidence="verified"),
        mem.make_event("capability_recorded", at="2026-10-02", session="t",
                       source="user", tech="Python", point="能写函数",
                       op="revoke"),
    ])
    out = run_coach_tool("recall_memory", {
        "tech": "Python", "kind": "capability", "status": "revoked",
    }, _ctx())
    assert out["status"] == "ok"
    assert "能写函数" in out["text"]
    assert "revoked" in out["text"]


def test_recall_tool_filters_evidence(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", True)
    from src.adapters import memory as mem_store
    from src.domain import memory as mem
    mem_store.record_events([
        mem.make_event("capability_recorded", at="2026-10-01", session="t",
                       source="system", tech="Python", point="已验收点",
                       op="add", evidence="verified"),
        mem.make_event("capability_recorded", at="2026-10-02", session="t",
                       source="migration", tech="Python", point="未考据点",
                       op="add", evidence="unknown"),
    ])
    out = run_coach_tool("recall_memory", {
        "tech": "Python", "kind": "capability", "evidence": "unknown",
    }, _ctx())
    assert out["status"] == "ok"
    assert "未考据点" in out["text"]
    assert "已验收点" not in out["text"]
    assert "证据=unknown" in out["text"]


def test_recall_arg_audit_does_not_dump_full_query():
    query = "这是一段很长的用户原话" * 10
    fields = route_mod.tool_arg_fields("recall_memory", {
        "query": query, "tech": "Python",
    })
    assert fields["query_chars"] == len(query)
    assert fields["query_preview"] == query[:20]
    assert query not in json.dumps(fields, ensure_ascii=False)


def test_recall_tool_filters_key(dirs, monkeypatch):
    monkeypatch.setattr(config, "LTM_RECALL_ENABLED", True)
    from src.adapters import memory as mem_store
    from src.domain import memory as mem
    mem_store.record_events([
        mem.make_event("profile_recorded", at="2026-10-01", session="t",
                       source="user", category="fact", scope="tech",
                       tech="Python", key="self_level", text="5"),
        mem.make_event("profile_recorded", at="2026-10-01", session="t",
                       source="user", category="fact", scope="tech",
                       tech="Python", key="time_budget", text="1"),
    ])
    out = run_coach_tool("recall_memory", {
        "tech": "Python", "kind": "fact", "status": "active",
        "key": "self_level",
    }, _ctx())
    assert out["status"] == "ok"
    assert "self_level" in out["text"]
    assert "time_budget" not in out["text"]
