"""pipelines/route coach 工具单测：generate_roadmap / confirm_roadmap / get_roadmap。

零网络；ROADMAP_DIR 重定向到临时目录，不污染仓库。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_route_tools.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.pipelines.route as route_mod
from src.config import config
from src.domain import roadmap as rm
from src.pipelines.route import CoachCtx, run_coach_tool


def _ctx(state=None):
    return CoachCtx(state or {"tech": "spring-boot", "survey_answers": {}, "learner_profile": {}})


def _gen_args():
    return {
        "goal": "能跑通最小项目",
        "total_hours": 12,
        "revision": "",
        "stages": [
            {"name": "环境搭建", "goal": "跑通 hello", "est_hours": 4,
             "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello"}]},
            {"name": "核心概念", "goal": "掌握核心", "est_hours": 8,
             "milestones": [{"desc": "理解 A"}]},
        ],
    }


def test_generate_roadmap_saves_json_and_md(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    ctx = _ctx()
    out = run_coach_tool("generate_roadmap", _gen_args(), ctx)
    assert out["status"] == "ok"
    assert out["current_stage"] == "s1"
    assert len(out["stages"]) == 2
    assert ctx.updates["roadmap"]["tech"] == "spring-boot"
    assert ctx.updates["roadmap"]["goal"] == "能跑通最小项目"
    assert (tmp_path / "roadmaps" / "spring-boot.json").exists()
    assert (tmp_path / "roadmaps" / "spring-boot-roadmap.md").exists()


def test_generate_roadmap_records_thread_and_profile(tmp_path, monkeypatch):
    """生成路线时记录 session_thread_id + 画像落盘（按 tech 归档，不含诊断原文）。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    ctx = CoachCtx({"tech": "spring-boot",
                    "learner_profile": {"self_level": 5, "bucket": "intermediate", "diagnostics": ["x"]}},
                   thread_id="route-abc")
    out = run_coach_tool("generate_roadmap", _gen_args(), ctx)
    assert out["status"] == "ok"
    assert ctx.updates["roadmap"]["session_thread_id"] == "route-abc"
    from src.adapters import learner as le
    profile = le.load_profile()
    assert "spring-boot" in profile
    assert profile["spring-boot"]["bucket"] == "intermediate"
    assert "diagnostics" not in profile["spring-boot"]


def test_generate_roadmap_without_profile_skips_save(tmp_path, monkeypatch):
    """无画像时生成路线不写 profile.json（正常降级）。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    ctx = CoachCtx({"tech": "spring-boot"})
    out = run_coach_tool("generate_roadmap", _gen_args(), ctx)
    assert out["status"] == "ok"
    from src.adapters import learner as le
    assert le.load_profile() == {}


def test_generate_roadmap_missing_fields_is_error():
    ctx = _ctx()
    out = run_coach_tool("generate_roadmap", {"goal": "g", "total_hours": 5, "stages": []}, ctx)
    assert out["status"] == "error"
    assert not ctx.updates  # 不落盘、不写状态


def test_generate_roadmap_bad_stage_reports_errors():
    ctx = _ctx()
    args = _gen_args()
    args["stages"] = [{"name": "", "goal": "x", "est_hours": 1, "milestones": [{"desc": "d"}]}]
    out = run_coach_tool("generate_roadmap", args, ctx)
    assert out["status"] == "error"
    assert out["errors"]
    assert not ctx.updates


def test_confirm_roadmap_sets_mode_coaching():
    ctx = _ctx({"tech": "t", "roadmap": {"tech": "t", "current_stage": "s1"}})
    out = run_coach_tool("confirm_roadmap", {}, ctx)
    assert out["status"] == "ok"
    assert ctx.updates["mode"] == "coaching"


def test_confirm_roadmap_without_roadmap_is_error():
    ctx = _ctx()
    out = run_coach_tool("confirm_roadmap", {}, ctx)
    assert out["status"] == "error"
    assert not ctx.updates


def test_get_roadmap_none_when_absent():
    out = run_coach_tool("get_roadmap", {}, _ctx())
    assert out["status"] == "ok"
    assert out["roadmap"] is None


def test_get_roadmap_present():
    roadmap = {"tech": "t", "goal": "g", "total_hours": 5, "current_stage": "s1",
               "stages": [{"id": "s1", "name": "a", "goal": "g", "est_hours": 2,
                           "milestones": [{"id": "s1-m1", "desc": "m", "done": False}]}]}
    out = run_coach_tool("get_roadmap", {}, _ctx({"roadmap": roadmap}))
    assert out["current_stage"] == "s1"
    assert out["stages"][0]["milestones"][0]["id"] == "s1-m1"


def test_unknown_tool_is_error():
    out = run_coach_tool("nope", {}, _ctx())
    assert out["status"] == "error"


# ---------- coaching 工具：collect / read / ask ----------

def test_collect_tool(monkeypatch):
    captured = {}

    def fake_collect(tech, focus=None, progress=None):
        captured.update(tech=tech, focus=focus)
        return {"urls": ["a", "b"], "report": "## 资料\n内容", "materials_path": "materials/x.md"}

    monkeypatch.setattr(route_mod, "collect_pipeline", fake_collect)
    out = run_coach_tool("collect", {"tech": "FastAPI", "focus": "异步"}, _ctx())
    assert out["status"] == "ok"
    assert captured["tech"] == "FastAPI"
    assert captured["focus"] == "异步"
    assert out["url_count"] == 2


def test_collect_progress_passthrough(monkeypatch):
    seen = []

    def fake_collect(tech, focus=None, progress=None):
        if progress:
            progress("进度")
        return {"urls": [], "report": "r", "materials_path": "materials/x.md"}

    monkeypatch.setattr(route_mod, "collect_pipeline", fake_collect)
    ctx = _ctx()
    ctx.progress = seen.append
    run_coach_tool("collect", {"tech": "t"}, ctx)
    assert seen == ["进度"]


def test_read_tool_error(monkeypatch):
    monkeypatch.setattr(route_mod, "read_pipeline", lambda url, progress=None: {"error": "抓取失败"})
    out = run_coach_tool("read", {"url": "http://x"}, _ctx())
    assert out["status"] == "error"


def test_read_tool_ok(monkeypatch):
    monkeypatch.setattr(route_mod, "read_pipeline",
                        lambda url, progress=None: {"report": "解读", "title": "T", "report_path": "reports/t.md",
                                                    "notes": [], "error": None})
    out = run_coach_tool("read", {"url": "http://x"}, _ctx())
    assert out["status"] == "ok"
    assert out["title"] == "T"


def test_ask_tool(monkeypatch):
    monkeypatch.setattr(route_mod, "qa_pipeline",
                        lambda q, tech=None, progress=None: {"answer": "答", "sources": [], "no_hit": False})
    out = run_coach_tool("ask", {"question": "什么是X"}, _ctx())
    assert out["status"] == "ok"
    assert out["answer"] == "答"


# ---------- coaching 工具：note / note_commit ----------

_MERGE_RESULT = {
    "new_points": [],
    "merge_candidates": [{"old_path": "a.md", "old_topic": "A", "old_content": "o",
                          "similarity": 0.9, "reason": "r", "topic": "T", "tags": [], "content": "n"}],
    "empty_reason": None, "summary": "s", "raw": "r", "new_count": 0, "merged_count": 1,
}


def test_note_tool_no_merge_persists(monkeypatch):
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda tech, log, materials_path=None, progress=None: {
                            **{k: v for k, v in _MERGE_RESULT.items() if k != "merge_candidates"},
                            "merge_candidates": [], "new_count": 1, "merged_count": 0,
                            "new_points": [{"topic": "T", "tags": [], "content": "c"}]})
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda tech, np, mc, mi: {"results": [{"topic": "T", "path": "t.md", "action": "new"}],
                                                  "new_count": 1, "merged_count": 0})
    out = run_coach_tool("note", {"tech": "t", "content": "学习内容"}, _ctx())
    assert out["status"] == "ok"
    assert out["new_count"] == 1


def test_note_tool_merge_needs_decision(monkeypatch):
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda tech, log, materials_path=None, progress=None: _MERGE_RESULT)
    ctx = _ctx()
    out = run_coach_tool("note", {"tech": "t", "content": "学习内容"}, ctx)
    assert out["status"] == "needs_decision"
    assert "发现 1 条" in out["message"]
    assert ctx.updates["coach_note_pending"]


def test_note_commit_uses_pending(monkeypatch):
    captured = {}
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda tech, np, mc, mi: captured.update(tech=tech, mi=mi) or
                            {"results": [{"topic": "A", "path": "a.md", "action": "merged"}],
                             "new_count": 0, "merged_count": 1})
    state = {"tech": "t", "coach_note_pending": {**_MERGE_RESULT, "_tech": "t"}}
    ctx = CoachCtx(state)
    out = run_coach_tool("note_commit", {"decision": "all"}, ctx)
    assert out["status"] == "ok"
    assert captured["tech"] == "t"
    assert captured["mi"] == {0}  # all → 合并全部
    assert ctx.updates["coach_note_pending"] is None  # 提交后清空


def test_note_commit_without_pending():
    out = run_coach_tool("note_commit", {"decision": "skip"}, _ctx())
    assert out["status"] == "error"


def test_note_commit_parse_indices(monkeypatch):
    monkeypatch.setattr(route_mod, "persist_points",
                        lambda tech, np, mc, mi: {"results": [], "new_count": 0, "merged_count": 0})
    state = {"tech": "t", "coach_note_pending": {**_MERGE_RESULT, "_tech": "t"}}
    ctx = CoachCtx(state)
    run_coach_tool("note_commit", {"decision": "1"}, ctx)
    # 由 note_commit 内部调 parse_merge_decision（"1" → {0}），persist 被调用即通过


# ---------- coaching 工具：update_roadmap ----------

def _roadmap():
    stages, _ = rm.normalize_stages([
        {"name": "环境搭建", "goal": "g", "est_hours": 4,
         "milestones": [{"desc": "安装完成"}, {"desc": "跑通 hello"}]},
        {"name": "核心概念", "goal": "g", "est_hours": 8, "milestones": [{"desc": "理解 A"}]},
    ])
    return rm.build_roadmap("t", "goal", 12, stages)


def test_update_roadmap_completes_and_advances(monkeypatch):
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": True, "missing": [], "reason": ""})
    ctx = CoachCtx({"tech": "t", "roadmap": _roadmap(),
                    "conversation": [{"role": "assistant", "type": "coach",
                                      "content": "先安装依赖，再跑通 hello world"},
                                     {"role": "user", "type": "chat", "content": "都弄好了"}],
                    "coach_messages": [{"role": "user", "content": "继续"}]})
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    # s1 还有 s1-m2 未完成 → 阶段不推进
    assert ctx.updates["roadmap"]["current_stage"] == "s1"
    out2 = run_coach_tool("update_roadmap", {"milestone_id": "s1-m2", "done": True}, ctx)
    assert out2["status"] == "rejected"  # 闸 1：s1-m1 刚勾、还在等用户确认 → 批量勾选拒绝


def test_update_roadmap_unknown_milestone(monkeypatch):
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    ctx = _ctx({"tech": "t", "roadmap": _roadmap()})
    out = run_coach_tool("update_roadmap", {"milestone_id": "s9-m9", "done": True}, ctx)
    assert out["status"] == "error"
    assert "available" in out


def test_update_roadmap_without_roadmap():
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1"}, _ctx())
    assert out["status"] == "error"


def test_update_roadmap_sets_milestone_pending(monkeypatch):
    """勾选里程碑且用户上一条回复不是推进指令 → 设待确认卡点 + note 要求总结询问。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": True, "missing": [], "reason": ""})
    ctx = CoachCtx({"tech": "t", "roadmap": _roadmap(),
                    "conversation": [{"role": "assistant", "type": "coach",
                                      "content": "先安装依赖，再跑通 hello world"}],
                    "coach_messages": [{"role": "assistant", "content": "讲解…"},
                                       {"role": "user", "content": "明白了"}]})
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert ctx.updates["coach_milestone_pending"] == "s1-m1"
    assert "询问" in out["note"]


def test_update_roadmap_skips_pending_on_advance_directive(monkeypatch):
    """用户上一条已明确「直接推进」→ 免确认，不设卡点。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": True, "missing": [], "reason": ""})
    ctx = CoachCtx({"tech": "t", "roadmap": _roadmap(),
                    "conversation": [{"role": "assistant", "type": "coach",
                                      "content": "先安装依赖，再跑通 hello world"}],
                    "coach_messages": [{"role": "assistant", "content": "讲解…"},
                                       {"role": "user", "content": "直接进入下一阶段"}]})
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert "coach_milestone_pending" not in ctx.updates


def test_update_roadmap_uncheck_no_pending(monkeypatch):
    """取消勾选（done=False）不设卡点。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    ctx = CoachCtx({"tech": "t", "roadmap": _roadmap(),
                    "coach_messages": [{"role": "user", "content": "勾错了"}]})
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": False}, ctx)
    assert out["status"] == "ok"
    assert "coach_milestone_pending" not in ctx.updates
    assert "取消" in out["note"]


# ---------- coaching 工具：revise_roadmap（修订保留进度） ----------

def test_revise_roadmap_without_roadmap_is_error():
    ctx = _ctx()
    out = run_coach_tool("revise_roadmap", _gen_args(), ctx)
    assert out["status"] == "error"
    assert not ctx.updates  # 无路线：不落盘、不写状态


def test_revise_roadmap_keeps_progress_and_saves(tmp_path, monkeypatch):
    """修订保留已勾选里程碑（按 desc 匹配）、更新 goal/时长并落盘 JSON+MD。"""
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    old = rm.complete_milestone(_roadmap(), "s1-m1")  # 「安装完成」已勾选
    ctx = CoachCtx({"tech": "t", "roadmap": old})
    args = _gen_args()
    args["goal"] = "能独立做一个小工具"
    args["total_hours"] = 20
    out = run_coach_tool("revise_roadmap", args, ctx)
    assert out["status"] == "ok"
    assert out["kept_done"] == 1
    assert out["current_stage"] == "s1"
    merged = ctx.updates["roadmap"]
    assert merged["goal"] == "能独立做一个小工具"
    assert merged["total_hours"] == 20
    # desc 匹配的里程碑保留完成进度，未匹配的不保留
    assert merged["stages"][0]["milestones"][0]["done"] is True
    assert merged["stages"][0]["milestones"][1]["done"] is False
    assert (tmp_path / "roadmaps" / "t.json").exists()
    assert (tmp_path / "roadmaps" / "t-roadmap.md").exists()


def test_revise_roadmap_bad_stages_is_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    old = rm.complete_milestone(_roadmap(), "s1-m1")
    ctx = CoachCtx({"tech": "t", "roadmap": old})
    args = _gen_args()
    args["stages"] = [{"name": "", "goal": "x", "est_hours": 1, "milestones": [{"desc": "d"}]}]
    out = run_coach_tool("revise_roadmap", args, ctx)
    assert out["status"] == "error"
    assert out["errors"]
    assert not ctx.updates


# ---------- 里程碑验收闸门（evidence 双层校验 + 批量勾选护栏） ----------

def _gate_ctx(user_msg="明白了", pending=None):
    """验收闸门测试通用 ctx：对话里有真实内容可引用，最新 user 消息可定制。"""
    return CoachCtx({"tech": "t", "roadmap": _roadmap(),
                     "conversation": [
                         {"role": "assistant", "type": "coach",
                          "content": "任务：本地安装依赖并跑通 hello world"},
                         {"role": "user", "type": "chat", "content": "装好了，hello world 跑通了"},
                     ],
                     "coach_messages": [{"role": "assistant", "content": "讲解…"},
                                        {"role": "user", "content": user_msg}],
                     "coach_milestone_pending": pending})


def test_update_roadmap_verification_rejects_uncovered(monkeypatch):
    """对话记录无实质完成内容 → LLM 验收拒绝，缺失项回喂（不落盘、不设 pending）。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": False, "missing": ["安装依赖未完成"],
                                      "reason": "只提到跑通，没提安装"})
    ctx = _gate_ctx()
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "rejected"
    assert out["missing"] == ["安装依赖未完成"]
    assert "roadmap" not in ctx.updates and "coach_milestone_pending" not in ctx.updates


def test_update_roadmap_verification_pass(monkeypatch):
    """验收通过 → 落盘 + 设 pending（用户无推进指令时）。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": True, "missing": [], "reason": "内容覆盖"})
    ctx = _gate_ctx()
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert ctx.updates["roadmap"]["stages"][0]["milestones"][0]["done"] is True
    assert ctx.updates["coach_milestone_pending"] == "s1-m1"


def test_update_roadmap_verifier_degrades_open(monkeypatch):
    """验收器自身故障（verified=None）→ 降级放行，不卡死流程。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": None, "missing": [], "reason": "异常"})
    ctx = _gate_ctx()
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"


def test_update_roadmap_claim_exempts_verification(monkeypatch):
    """用户明确声明完成（对话外完成）→ 跳过验收直接勾选。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: (_ for _ in ()).throw(AssertionError("豁免时不应调 LLM 验收")))
    ctx = _gate_ctx(user_msg="我在本地都搞定了，直接勾吧")
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"
    assert ctx.updates["coach_milestone_pending"] == "s1-m1"


def test_update_roadmap_disabled_by_config(monkeypatch):
    """ROUTE_MILESTONE_VERIFY=False → 跳过验收（逃生舱）。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(config, "ROUTE_MILESTONE_VERIFY", False)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: (_ for _ in ()).throw(AssertionError("关闭时不应调 LLM 验收")))
    ctx = _gate_ctx()
    out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out["status"] == "ok"


def test_update_roadmap_batch_guard(monkeypatch):
    """同回合第二个勾选被拒绝（截图事故：一轮勾满所有里程碑架空确认闸门）。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: {"verified": True, "missing": [], "reason": ""})
    ctx = _gate_ctx()
    out1 = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out1["status"] == "ok"
    out2 = run_coach_tool("update_roadmap", {"milestone_id": "s1-m2", "done": True}, ctx)
    assert out2["status"] == "rejected"
    assert "一轮只能勾一个" in out2["error"]
    assert ctx.updates["roadmap"]["stages"][0]["milestones"][1]["done"] is False  # 第二个未落盘


# ---------- 验收 helper：送审文本 ----------

def test_transcript_text_caps_tail():
    conv = [{"role": "user", "content": "0" * 100},
            {"role": "system", "content": "内部"},  # 非对话角色过滤
            {"role": "assistant", "content": "1" * 100}]
    out = route_mod._transcript_text(conv, 50)
    assert len(out) == 50
    assert out.endswith("1" * 50)


def test_update_roadmap_blocks_after_two_rejections(monkeypatch):
    """同回合被拒 2 次后封锁 update_roadmap（真实事故：无限重试烧光工具预算，用户问题没被回答）。"""
    monkeypatch.setattr(route_mod.learner, "save_roadmap", lambda r: r)
    calls = []
    monkeypatch.setattr(route_mod, "verify_milestone",
                        lambda d, t: calls.append(d) or {"verified": False,
                                                         "missing": ["缺"], "reason": "未完成"})
    ctx = _gate_ctx()
    for i in range(2):
        out = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
        assert out["status"] == "rejected", i
    # 第 3 次：节流闸拦截，不再调验收（剩余预算留给回答用户）
    out3 = run_coach_tool("update_roadmap", {"milestone_id": "s1-m1", "done": True}, ctx)
    assert out3["status"] == "blocked"
    assert "停止调用 update_roadmap" in out3["instruction"]
    assert len(calls) == 2  # 第 3 次未调 LLM 验收
    assert ctx.updates["coach_verify_rejects"] == 2


def test_transcript_text_caps_tail():
    conv = [{"role": "user", "content": "0" * 100},
            {"role": "system", "content": "内部"},  # 非对话角色过滤
            {"role": "assistant", "content": "1" * 100}]
    out = route_mod._transcript_text(conv, 50)
    assert len(out) == 50
    assert out.endswith("1" * 50)
