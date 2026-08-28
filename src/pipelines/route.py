"""coach agent 循环的提示词 + 工具实现（模块 2 定制化学习路线）。

定位：pipelines 层，prompts 就近存放；graph.py 的 coach 节点组负责循环编排，
本模块只提供"单次往返"需要的东西——按 mode 的系统提示词、各模式可用工具
schema、工具执行（大内容写文件、出入参短，工具可经 ctx.updates 请求状态变更）。

Step 1-3 范围：survey（问卷）→ planning（路线生成/确认）→ coaching（执行陪练，
完整工具在后续阶段接入；现为对话 stub）。
"""

import json
import re

from ..config import config
from ..adapters import learner
from ..adapters.llm import generate_text
from ..domain import roadmap as roadmap_domain
from ..domain import survey
from ..domain.extraction import parse_json_object
from .collect import collect_pipeline
from .note import format_merge_candidates, note_pipeline, parse_merge_decision, persist_points
from .qa import _search_notes, qa_pipeline
from .read import read_pipeline

# ============================================================
# coach 系统提示词（按 mode）
# ============================================================

_FIELD_LABELS = {
    "self_level": "自评熟悉度（0-10）",
    "related": "相关技术",
    "goal": "学习目标",
    "time_budget": "时间预算",
}
_FIELD_HINTS = {
    "self_level": "0-10 之间的数字",
    "related": "自由文本（没有就填「无」）",
    "goal": "「快速上手跑通最小项目」或「深入原理」",
    "time_budget": "每天小时数（如 2）",
}


def _survey_prompt(state, tech: str) -> str:
    answers = state.get("survey_answers") or {}
    field = state.get("survey_field")
    profile = state.get("learner_profile") or {}
    field_label = _FIELD_LABELS.get(field) if field else "动态诊断题"
    answers_txt = "；".join(f"{k}={v}" for k, v in answers.items()) or "（暂无）"
    profile_txt = survey.profile_summary(profile) if profile else "（尚未推导）"
    diag_remaining = max(survey.DIAGNOSTIC_QUESTIONS_MAX - len(answers.get("diagnostics") or []), 0)
    lines = [
        f"你是「技术学习陪练」的水平探测助手。用户要学习：{tech}。",
        "通过一轮简短问卷了解用户水平，为后续定制学习路线收集信息。",
        "",
        "当前问卷状态：",
        f"- 正在收集字段：{field_label}",
        f"- 已收集：{answers_txt}",
        f"- 用户画像（参考调整语气）：{profile_txt}",
        "",
        "规则：",
        f"1. 一次只问一个问题，且只问「正在收集字段」对应的问题：{field_label}。",
    ]
    if field:
        lines.append(f"2. 该字段用户需要回答成：{_FIELD_HINTS.get(field)}。")
        lines.append("3. 只输出问题本身，不要编号、解释或寒暄；根据画像调整语气（小白少用术语、多类比）。")
    else:
        lines.append(f"2. 固定字段已收齐，进入动态出题：基于画像出 1 道与「{tech}」相关的小诊断题（概念题/选择题），检验前置知识；还需 {diag_remaining} 道。")
        lines.append("3. 只输出题目本身；根据画像调整难度与语气。")
    lines.append("4. 若收到一条【问卷校验】内部提示，说明用户上一条回答格式不对，请按提示重新问同一个字段。")
    return "\n".join(lines)


def _planning_prompt(state, tech: str) -> str:
    profile = state.get("learner_profile") or {}
    summary = survey.profile_summary(profile) if profile else "（问卷信息缺失）"
    conv = (state.get("coach_summary") or "").strip()
    conv_block = f"\n此前对话摘要：{conv}\n" if conv else ""
    return (
        f"你是「技术学习陪练」的路线规划助手。用户要学习：{tech}。\n"
        f"用户水平画像：{summary}{conv_block}\n"
        "任务：为用户生成一份个性化、可执行的分阶段学习路线（3-5 个阶段，每阶段含可检验的里程碑）。\n"
        "步骤：\n"
        "1. 调用 generate_roadmap 生成路线（goal / total_hours / stages；stages 含 name/goal/materials/est_hours/milestones）。\n"
        "2. 把生成后的路线（阶段、里程碑、预估时长）用 Markdown 完整呈现给用户，请用户确认或提出修改。\n"
        "3. 用户确认后调用 confirm_roadmap 进入执行阶段；用户提出修改时，带上修改意见重新调用 generate_roadmap。\n"
        "规则：\n"
        "- 路线必须贴合用户水平：技术小白从环境搭建和最小 demo 起步、拆小步；开发者可跳过基础直达要点。\n"
        "- 里程碑必须是可检验的动作（如「本地跑通 hello world」「能用 XX 完成一个接口」），不要空洞目标。\n"
        "- 阶段按依赖排序，每阶段给出预估小时数。\n"
        "- materials 可引用已收集的资料或留空（留空由后续 collect 补充）。\n"
        "- 不要编造资料链接；没有的信息写「待收集」。"
    )


def _coaching_prompt(state, tech: str) -> str:
    roadmap = state.get("roadmap")
    prog = ""
    if roadmap:
        prog = "\n\n当前路线：\n" + roadmap_domain.roadmap_to_markdown(roadmap)
    # 记忆系统 Step 4：画像直注（修复 coaching 模式不注入画像的缺口，画像与摘要解耦）+ 三舱注入
    profile = state.get("learner_profile") or {}
    profile_block = f"用户画像：{survey.profile_summary(profile)}\n" if profile else ""
    facts = state.get("coach_facts") or []
    facts_block = ""
    if facts:
        facts_block = "已确认的事实与偏好：\n" + "\n".join(f"- {f}" for f in facts) + "\n"
    open_items = state.get("coach_open_items") or []
    open_block = ""
    if open_items:
        open_block = ("未决事项（待跟进，解决后移除）：\n"
                      + "\n".join(f"- [{it['id']}] {it['text']}" for it in open_items) + "\n")
    conv = (state.get("coach_summary") or "").strip()
    conv_block = f"\n此前对话摘要：{conv}\n" if conv else ""
    # 记忆系统 Step 2：确定性读路由——命中知识库时把相关片段注入作答上下文（来源由代码标注）
    kb = state.get("kb_context") or []
    kb_block = ""
    if kb:
        parts = [f"（来源：{it.get('path', '')}）\n{it.get('snippet', '')}" for it in kb]
        kb_block = ("\n\n===== 知识库相关片段（按来源标注） =====\n"
                    + "\n\n".join(parts)
                    + "\n===== 片段结束 =====\n"
                    "优先依据上述片段作答，引用时标注来源；片段未覆盖的部分可用你自己的知识补充，"
                    "但必须明确区分「来自你笔记的内容」与「基于经验的讲解」。")
    return (
        f"你是「技术学习陪练」的执行陪练。用户学习：{tech}。{prog}\n"
        f"{profile_block}{facts_block}{open_block}{conv_block}{kb_block}\n\n"
        "你可以用工具推进学习：collect（收集资料）/ read（解读文档）/ note（沉淀笔记）/ "
        "ask（问已学笔记）/ get_roadmap / update_roadmap（勾选里程碑）。"
        "原则：先给用户清晰的下一步，用户确认后再用工具；工具结果要提炼成对用户有用的信息，"
        "不要原样堆砌。里程碑完成时用 update_roadmap 勾选。"
    )


def coach_system_prompt(state) -> str:
    """按 mode 挑选 coach 系统提示词（survey / planning / coaching）。"""
    mode = state.get("mode") or "survey"
    tech = state.get("tech") or ""
    if mode == "planning":
        return _planning_prompt(state, tech)
    if mode == "coaching":
        return _coaching_prompt(state, tech)
    return _survey_prompt(state, tech)


# ============================================================
# 工具 schema（OpenAI function calling 格式）
# ============================================================

GENERATE_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_roadmap",
        "description": "根据用户水平画像生成或修订学习路线（阶段+里程碑）。修订时把改动说明写进 revision，stages 给出完整新结构。",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "学习总目标（一句话，可复用问卷目标）"},
                "total_hours": {"type": "integer", "description": "预估总学习时长（小时）"},
                "revision": {"type": "string", "description": "修订说明；首次生成填空字符串"},
                "stages": {
                    "type": "array",
                    "description": "学习阶段列表（3-5 个，按依赖排序）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "阶段名（如 环境搭建 / 核心概念 / 实战项目 / 进阶调优）"},
                            "goal": {"type": "string", "description": "本阶段目标"},
                            "materials": {"type": "string", "description": "推荐资料（可留空由后续 collect 补充）"},
                            "est_hours": {"type": "integer", "description": "本阶段预估小时数"},
                            "milestones": {
                                "type": "array",
                                "description": "可检验的里程碑（完成一个可检验动作才算通关）",
                                "items": {"type": "object",
                                          "properties": {"desc": {"type": "string"}},
                                          "required": ["desc"]},
                            },
                        },
                        "required": ["name", "goal", "est_hours", "milestones"],
                    },
                },
            },
            "required": ["goal", "total_hours", "stages"],
        },
    },
}

CONFIRM_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "confirm_roadmap",
        "description": "用户已确认路线，进入执行阶段。调用前必须先把路线完整呈现给用户并获得明确确认。",
        "parameters": {"type": "object", "properties": {}},
    },
}

GET_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_roadmap",
        "description": "获取当前学习路线（阶段 / 当前阶段 / 里程碑进度）。",
        "parameters": {"type": "object", "properties": {}},
    },
}

COLLECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "collect",
        "description": "搜索并筛选某个技术的学习资料，生成资料清单报告（保存到 materials/）。耗时较长，会推送进度。",
        "parameters": {"type": "object", "properties": {
            "tech": {"type": "string", "description": "技术名称（如 Spring Boot）"},
            "focus": {"type": "string", "description": "可选，关注点（如 异步编程）"},
        }, "required": ["tech"]},
    },
}

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "抓取并解读一篇技术文档（URL），生成结构化解读报告（保存到 reports/）。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "文档完整 URL"},
        }, "required": ["url"]},
    },
}

NOTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note",
        "description": "把一段学习内容沉淀为知识笔记（差量提取；有相似笔记时返回候选待用户确认，用户决定后再调 note_commit）。content 必须短（≤2000 字）。",
        "parameters": {"type": "object", "properties": {
            "tech": {"type": "string", "description": "技术名称"},
            "content": {"type": "string", "description": "本次要沉淀的学习内容（较短，≤2000 字）"},
        }, "required": ["tech", "content"]},
    },
}

NOTE_COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note_commit",
        "description": "用户对相似笔记候选做出决定后提交沉淀。decision 传用户原话（如 all / 1,3 / skip）。",
        "parameters": {"type": "object", "properties": {
            "decision": {"type": "string", "description": "用户原话：all 全合并 / 编号逗号分隔逐条（如 1,3）/ skip 全部跳过"},
        }, "required": ["decision"]},
    },
}

ASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask",
        "description": "向用户的知识笔记库提问并综合回答（跨笔记联想检索，带来源标注）。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "要问的问题"},
        }, "required": ["question"]},
    },
}

UPDATE_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_roadmap",
        "description": "勾选/取消勾选一个里程碑（完成一个可检验的动作后调用），自动推进当前阶段。",
        "parameters": {"type": "object", "properties": {
            "milestone_id": {"type": "string", "description": "里程碑 id（如 s1-m1）"},
            "done": {"type": "boolean", "description": "true 勾选 / false 取消"},
        }, "required": ["milestone_id"]},
    },
}

COACH_TOOLS_BY_MODE: dict[str, list[dict]] = {
    "survey": [],
    "planning": [GENERATE_ROADMAP_SCHEMA, CONFIRM_ROADMAP_SCHEMA, GET_ROADMAP_SCHEMA],
    "coaching": [COLLECT_SCHEMA, READ_SCHEMA, NOTE_SCHEMA, NOTE_COMMIT_SCHEMA, ASK_SCHEMA,
                 GET_ROADMAP_SCHEMA, UPDATE_ROADMAP_SCHEMA],
}


# ============================================================
# 工具实现（出入参短；大内容写文件）
# ============================================================


class CoachCtx:
    """coach 工具执行上下文（graph 层构造）。updates 由工具写入，graph 节点合并进状态。

    progress 为 Web 端流式进度回调（graph 层经 _get_progress 从注册表取，CLI 为 None 静默）。
    """

    def __init__(self, state: dict, *, progress=None, thread_id: str | None = None):
        self.state = state
        self.updates: dict = {}
        self.progress = progress
        self.thread_id = thread_id  # 当前图会话的 thread_id（路线恢复定位用）

    # 属性读取：先查本批工具产生的 updates（同一 assistant 消息的多个 tool_calls 可见彼此
    # 的更新），再回退到 graph 状态——保证同批多次调用（如连勾两个里程碑）不读到旧值。
    @property
    def tech(self) -> str:
        return self.updates.get("tech", self.state.get("tech") or "")

    @property
    def survey_answers(self) -> dict:
        return self.state.get("survey_answers") or {}

    @property
    def learner_profile(self) -> dict:
        return self.state.get("learner_profile") or {}

    @property
    def materials_path(self) -> str | None:
        return self.updates.get("materials_path", self.state.get("materials_path"))

    @property
    def roadmap(self):
        return self.updates.get("roadmap", self.state.get("roadmap"))


def _generate_roadmap(args: dict, ctx: CoachCtx) -> dict:
    goal = str(args.get("goal") or "").strip()
    total_hours = args.get("total_hours")
    stages = args.get("stages") or []
    if not goal or not isinstance(total_hours, int) or total_hours <= 0 or not stages:
        return {"status": "error", "error": "缺少 goal / total_hours / stages",
                "hint": "请按 generate_roadmap 的 schema 补全后重试"}
    norm, errors = roadmap_domain.normalize_stages(stages)
    if not norm:
        return {"status": "error", "errors": errors or ["没有合法阶段"],
                "hint": "请修正阶段结构（每阶段至少 1 个里程碑）后重新调用 generate_roadmap"}
    roadmap = roadmap_domain.build_roadmap(ctx.tech, goal, total_hours, norm)
    # 记录生成路线的会话线程：CLI「按技术名继续上次陪练」靠它定位恢复
    if ctx.thread_id:
        roadmap["session_thread_id"] = ctx.thread_id
    jp = learner.save_roadmap(roadmap)
    ctx.updates["roadmap"] = roadmap
    ctx.updates["roadmap_path"] = str(jp)
    # 画像落盘（profile.json 按 tech 归档；不含诊断题原文）
    if ctx.learner_profile:
        entry = {k: v for k, v in ctx.learner_profile.items() if k != "diagnostics"}
        entry["roadmap_path"] = str(jp)
        try:
            learner.save_tech_profile(ctx.tech, entry)
        except Exception:  # noqa: BLE001 —— 画像落盘失败不阻断路线生成
            pass
    return {
        "status": "ok",
        "roadmap_path": str(jp),
        "stages": [{"id": s["id"], "name": s["name"], "est_hours": s["est_hours"]} for s in roadmap["stages"]],
        "current_stage": roadmap["current_stage"],
        "total_hours": roadmap["total_hours"],
        "note": "路线已保存。请把路线完整呈现给用户确认；用户确认后调用 confirm_roadmap。",
    }


def _confirm_roadmap(args: dict, ctx: CoachCtx) -> dict:
    if not ctx.roadmap:
        return {"status": "error", "error": "还没有路线，请先调用 generate_roadmap"}
    ctx.updates["mode"] = "coaching"
    return {"status": "ok", "current_stage": ctx.roadmap.get("current_stage"),
            "note": "路线已确认，进入执行阶段（coaching）。"}


def _get_roadmap(args: dict, ctx: CoachCtx) -> dict:
    r = ctx.roadmap
    if not r:
        return {"status": "ok", "roadmap": None, "note": "还没有生成路线"}
    return {
        "status": "ok", "tech": r.get("tech"), "goal": r.get("goal"),
        "total_hours": r.get("total_hours"), "current_stage": r.get("current_stage"),
        "stages": [{"id": s.get("id"), "name": s.get("name"), "goal": s.get("goal"),
                    "est_hours": s.get("est_hours"),
                    "milestones": [{"id": m.get("id"), "desc": m.get("desc"), "done": m.get("done")}
                                   for m in s.get("milestones") or []]}
                   for s in r.get("stages") or []],
    }


def _collect(args: dict, ctx: CoachCtx) -> dict:
    tech = str(args.get("tech") or "").strip() or ctx.tech
    if not tech:
        return {"status": "error", "error": "collect 需要 tech"}
    focus = str(args.get("focus") or "").strip() or None
    try:
        result = collect_pipeline(tech, focus, progress=ctx.progress)
    except Exception as e:  # noqa: BLE001 —— 回喂模型修正
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    ctx.updates["coach_doc"] = {"path": result["materials_path"], "type": "collect"}
    return {"status": "ok", "materials_path": result["materials_path"],
            "url_count": len(result["urls"]),
            "report_excerpt": (result["report"] or "")[:800],
            "note": "资料报告已保存，可据此推进学习。"}


def _read(args: dict, ctx: CoachCtx) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"status": "error", "error": "read 需要 url"}
    try:
        result = read_pipeline(url, progress=ctx.progress)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    if result.get("error"):
        return {"status": "error", "error": result["error"]}
    ctx.updates["coach_doc"] = {"path": result["report_path"], "type": "read"}
    return {"status": "ok", "report_path": result["report_path"], "title": result.get("title"),
            "report_excerpt": (result["report"] or "")[:800]}


def _ask(args: dict, ctx: CoachCtx) -> dict:
    question = str(args.get("question") or "").strip()
    if not question:
        return {"status": "error", "error": "ask 需要 question"}
    try:
        result = qa_pipeline(question, tech=ctx.tech or None, progress=ctx.progress)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "answer": result["answer"],
            "sources": [{"path": s.get("path"), "topic": s.get("topic"),
                         "similarity": s.get("similarity")} for s in result["sources"]],
            "no_hit": result["no_hit"]}


def _note(args: dict, ctx: CoachCtx) -> dict:
    tech = str(args.get("tech") or "").strip() or ctx.tech
    content = str(args.get("content") or "").strip()
    if not tech or not content:
        return {"status": "error", "error": "note 需要 tech 和 content（≤2000 字）"}
    try:
        result = note_pipeline(tech, content, materials_path=ctx.materials_path, progress=ctx.progress)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    if result.get("empty_reason"):
        return {"status": "empty", "reason": result["empty_reason"]}
    if result["merge_candidates"]:
        # 相似候选暂存状态，等用户决定后再 note_commit（复用现有 format/parse 纯函数）
        ctx.updates["coach_note_pending"] = {**result, "_tech": tech}
        return {"status": "needs_decision",
                "message": format_merge_candidates(result["merge_candidates"]),
                "note": "把上面的候选呈现给用户，让用户回复 all（全合并）/ 编号逐条（如 1,3）/ skip（跳过）；用户决定后调用 note_commit，decision 传用户原话。"}
    persisted = persist_points(tech, result["new_points"], [], set())
    out = {"status": "ok", "new_count": persisted["new_count"], "merged_count": 0,
           "results": [{"topic": r["topic"], "path": r["path"], "action": r["action"]}
                       for r in persisted["results"]]}
    warning = _index_warning(persisted)
    if warning:
        out["warning"] = warning
    return out


def _index_warning(persisted: dict) -> str:
    """索引失败提示（P3.1）：沉淀结果里有 index_ok=False 时生成给 agent 转述的警告。

    索引失败不阻断沉淀（笔记已在磁盘），但必须可见——8-19 事故的教训：静默失败
    让 4 篇笔记「保存成功但检索不到」，缺口留存 6 天。
    """
    failed = [r["topic"] for r in persisted.get("results", []) if r.get("index_ok") is False]
    if not failed:
        return ""
    return f"⚠️ RAG 索引更新失败：{'、'.join(failed)}（笔记已保存，稍后运行 index 会自动补齐）"


def _note_commit(args: dict, ctx: CoachCtx) -> dict:
    pending = ctx.state.get("coach_note_pending")
    if not pending:
        return {"status": "error", "error": "没有待确认的笔记，请先调用 note"}
    decision = str(args.get("decision") or "").strip()
    tech = pending.get("_tech") or ctx.tech
    indices = parse_merge_decision(decision, len(pending["merge_candidates"]))
    persisted = persist_points(tech, pending["new_points"], pending["merge_candidates"], indices)
    ctx.updates["coach_note_pending"] = None  # 提交后清掉，避免重复提交
    # 记忆系统 Step 3：合并时发现的矛盾以新内容为准修正，报告透出给 agent 转述用户
    conflict_reports = persisted.get("conflict_reports") or []
    out = {"status": "ok", "new_count": persisted["new_count"], "merged_count": persisted["merged_count"],
           "conflict_reports": conflict_reports,
           "results": [{"topic": r["topic"], "path": r["path"], "action": r["action"]}
                       for r in persisted["results"]]}
    warning = _index_warning(persisted)
    if warning:
        out["warning"] = warning
    return out


# ============================================================
# 记忆系统 Step 1：确定性写触发（学习内容自动沉淀）
# 纯函数：对话缓冲 → note_pipeline → 返回决策，落库/暂存由 graph 节点编排。
# ============================================================


def _sweep_buffer_text(buffer: list[dict]) -> str:
    """把沉淀缓冲（[{role, content}]）拼成 note 管道的输入文本（只取 user/assistant 纯文本）。"""
    parts = []
    for m in buffer:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            parts.append(f"{m['role']}：{m['content']}")
    return "\n".join(parts)


def run_memory_sweep(tech: str, buffer: list[dict], progress=None) -> dict:
    """确定性写触发：把自上次沉淀以来的对话文本喂给 note 管道沉淀。

    Args:
        tech: 技术名
        buffer: 消息对列表 [{role, content}]（user/assistant 纯文本）
        progress: 可选进度回调

    Returns:
        {
          "action": "skip" | "persisted" | "pending",
          "count": int,            # persisted: 新建知识点数
          "pending": dict | None,  # pending: coach_note_pending 负载（含 _tech/_auto）
          "message": str | None,   # 追加给 agent 的 system 提示（可选）
        }
    """
    if not tech or not buffer:
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    text = _sweep_buffer_text(buffer)
    if not text.strip():
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    if progress:
        progress("🗂️ 正在沉淀学习内容...")
    result = note_pipeline(tech, text, progress=progress)
    if result.get("empty_reason"):
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    # LLM 输出条目全无效（topic/正文为空被过滤）→ 两个桶都空，等价于无新内容，按 skip 处理
    if not result.get("new_points") and not result.get("merge_candidates"):
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    if result["merge_candidates"]:
        # 有相似候选：新点与候选一起暂存，等用户决定后经 note_commit 一次落库（与 note 工具流一致）
        pending = {**result, "_tech": tech, "_auto": True}
        msg = (f"（内部）检测到 {len(result['new_points'])} 个新知识点、"
               f"{len(result['merge_candidates'])} 个与旧笔记相似的候选，"
               "请在回复中呈现候选并请用户决定（all / 编号逗号分隔 / skip），"
               "用户决定后调用 note_commit，decision 传用户原话。")
        return {"action": "pending", "count": len(result["new_points"]),
                "pending": pending, "message": msg}
    persisted = persist_points(tech, result["new_points"], [], set())
    msg = f"（内部）已自动沉淀 {persisted['new_count']} 个新知识点。"
    warning = _index_warning(persisted)
    if warning:
        msg += f" {warning}（请在回复中提醒用户）"
    return {"action": "persisted", "count": persisted["new_count"],
            "pending": None, "message": msg}


# ============================================================
# 记忆系统 Step 2：确定性读路由（提问先查库）
# 纯函数：元问题廉价闸门 + 知识库检索 + 相似度阈值过滤 → 命中片段列表，注入 coaching 提示词。
# ============================================================

# 廉价闸门关键词：明显过程 / 元问题（继续、现在到哪、路线对吗…）确定性跳过，不查库。
# 判定沿用 exit_intent 的「残留检查」思路：短文本整体由元关键词 + 语气词组成才算元问题，
# 避免「这个写法对不对」这类含学习内容的提问被误判跳过。
_META_RE = re.compile(
    r"(继续|接着|下一步|然后呢|接下来|之后呢|还有吗|现在到哪|到哪了|现在在哪|"
    r"什么进度|进度|当前阶段|在哪一步|第几阶段|学了多少|完成了吗|"
    r"这个路线|路线对吗|路线正确吗|路线合理吗|对不对|对吗|对不|"
    r"明白了|懂了|知道了|好的|可以|行|嗯|开始吧|就这样|先这样|"
    r"继续说|讲下去|再说一遍|什么意思|听不懂|举个例子)",
    re.IGNORECASE,
)
_META_MAX_LEN = 12  # 元问题判定只对短文本生效（长回答含学习内容，交给检索判断）
_META_PARTICLES = "吧了呀呢嘛啊哦哈嗯~～。，,.！!？? "


def _is_meta_question(text: str) -> bool:
    """确定性判定：整句是否仅为过程 / 元问题（零成本廉价闸门，命中即不查库）。"""
    t = (text or "").strip()
    if not t or len(t) > _META_MAX_LEN:
        return False
    prev = None
    while prev != t:  # 迭代剥离所有元关键词（「好的，继续」等复合短语一次清干净）
        prev = t
        t = _META_RE.sub("", t).strip()
    return not t.strip(_META_PARTICLES)


def _hit_relevance(h: dict) -> float | None:
    """命中的「可标定相关度」：可用于绝对阈值比较的分数。

    P1 起 hybrid 的 ``similarity`` 是归一化相对分（top 恒 1.0，见 rrf_fuse），
    与 ROUTE_KB_INJECT_SIM 这类绝对阈值**不可比**；``dense_similarity`` 才是融合时
    保留的原始余弦。优先级：
    - hybrid 且进过 dense 榜单：dense_similarity（真实余弦）
    - hybrid 的 BM25 独有命中：无标定分 → None（宁可不注入，不拿归一化分凑数）
    - 纯 dense 路径（QA_USE_HYBRID 关闭）：similarity 即余弦
    """
    if h.get("dense_similarity") is not None:
        return h["dense_similarity"]
    if "bm25_score" in h:
        return None
    return h.get("similarity")


def run_kb_retrieve(tech: str, question: str) -> list[dict]:
    """确定性读路由：提问先查库，命中相关片段列表 [{path, snippet}]（无命中返回空）。

    两级闸门：
    - 廉价闸门（零成本）：明显过程/元问题（继续、现在到哪、路线对吗…）直接跳过，不查库；
    - 质量闸门（可标定相似度）：复用 qa 混合检索（限定 tech），按 _hit_relevance
      （hybrid 用保留的 dense 原始余弦、BM25 独有命中不过闸）≥ ROUTE_KB_INJECT_SIM
      取前 ROUTE_KB_SNIPPETS 条，片段截断到 QA_SNIPPET_CHARS。混合检索的归一化
      similarity 不能当绝对门槛。
    检索异常（RAG 未索引 / Chroma 异常）优雅降级为空——模型用自己的知识正常回答。
    """
    question = (question or "").strip()
    if not tech or not question:
        return []
    if _is_meta_question(question):
        return []
    try:
        hits = _search_notes(question, config.QA_TOP_K, tech)
    except Exception:  # noqa: BLE001 —— RAG 未索引 / Chroma 异常时降级为空
        return []
    out: list[dict] = []
    for h in hits:
        rel = _hit_relevance(h)
        if rel is None or rel < config.ROUTE_KB_INJECT_SIM:
            continue
        snippet = (h.get("document") or "").strip()
        if not snippet:
            continue
        out.append({"path": h.get("path") or "", "snippet": snippet[:config.QA_SNIPPET_CHARS]})
        if len(out) >= config.ROUTE_KB_SNIPPETS:
            break
    return out


def _update_roadmap(args: dict, ctx: CoachCtx) -> dict:
    r = ctx.roadmap
    if not r:
        return {"status": "error", "error": "还没有路线，请先走问卷 + 路线规划"}
    milestone_id = str(args.get("milestone_id") or "").strip()
    done = bool(args.get("done", True))
    if not milestone_id:
        return {"status": "error", "error": "update_roadmap 需要 milestone_id（如 s1-m1）"}
    try:
        updated = roadmap_domain.complete_milestone(r, milestone_id, done)
    except KeyError:
        ids = [m["id"] for s in (r.get("stages") or [])
               for m in (s.get("milestones") or [])]
        return {"status": "error", "error": f"未知里程碑 {milestone_id}", "available": ids}
    learner.save_roadmap(updated)
    ctx.updates["roadmap"] = updated
    prog = roadmap_domain.stage_progress(updated, updated["current_stage"])
    return {"status": "ok", "milestone_id": milestone_id, "done": done,
            "current_stage": updated["current_stage"],
            "progress": f"{prog['done']}/{prog['total']}",
            "roadmap_status": updated.get("status")}


_TOOL_IMPL = {
    "generate_roadmap": _generate_roadmap,
    "confirm_roadmap": _confirm_roadmap,
    "get_roadmap": _get_roadmap,
    "collect": _collect,
    "read": _read,
    "ask": _ask,
    "note": _note,
    "note_commit": _note_commit,
    "update_roadmap": _update_roadmap,
}


def run_coach_tool(name: str, args: dict, ctx: CoachCtx) -> dict:
    """按名分发工具；未知工具返回错误（护栏层保证不会出现）。"""
    fn = _TOOL_IMPL.get(name)
    if fn is None:
        return {"status": "error", "error": f"未知工具: {name}"}
    return fn(args, ctx)


# ============================================================
# 记忆系统 Step 4：三舱记忆整理（事实/未决/脉络）
# 核心原则：LLM 只看新消息产增量；已积累的记忆永不再过 LLM 的手（衰减结构性归零）。
# 提示词已提交用户审核并确认。
# ============================================================

CONSOLIDATE_MEMORY_PROMPT = """你是学习会话的记忆整理助手。我会给你：现有「稳定事实」列表、现有「未决事项」列表（带编号）、以及一批刚发生的对话。请为长期记忆做一次增量整理。

## 输出
只输出一个 JSON 对象，不要任何解释或 ```json 代码块标记：
{"facts_add": ["新稳定事实，每条一句话"],
 "open_add": ["新未决事项，每条一句话"],
 "resolved": [已解决的未决事项编号，如 2],
 "context": "最近学习进展的一段话（150 字以内）"}

## 判定标准
- facts_add（稳定事实=整个会话都该记住的）：用户画像的变化（水平/目标/时间预算）、学习偏好（如「喜欢类比」「少贴长代码」）、对陪练的纠正（如「不要用英文术语」）、重要决定（如「先跳过 Maven 直接学 Gradle」）。与「现有稳定事实」重复或同义的不输出；寒暄、过程性内容、普通问答不输出。
- open_add（未决事项=仍开放的问题/承诺/待确认）：用户提出但还没解决的问题、陪练答应之后要讲的主题、待用户确认的事。
- resolved：现有未决事项中，已被新对话回答/完成/用户明确不再关心的编号。没有则输出 []。
- context：只写最近学了什么、当前焦点、下一步意向；画像、决定、未决事项不要写进来（它们有独立位置，写进 context 会被反复重写）。

## 正例
- 用户说「这个类比好懂，后面多举例子」→ facts_add: ["用户反馈类比讲解效果好，希望多用类比"]
- 陪练讲完了「[2] 待讲：事务传播」→ resolved: [2]
- 用户问「AOP 切面执行顺序还没搞懂」→ open_add: ["AOP 切面执行顺序还没搞懂"]

## 反例（不要这样做）
- 把「用户问了 X」塞进 facts_add——已回答的普通问答不是事实
- 在 context 里复述画像或决定
- 输出解释文字、markdown 或编造未给出的编号
"""


def consolidate_memory(existing: dict, messages: list[dict], tech: str) -> dict:
    """三舱记忆增量整理：LLM 提取增量 → 确定性应用（去重追加 / 按 id 淘汰 / 机械上限）。

    Args:
        existing: 现有三舱 {"facts": [str], "open_items": [{"id": int, "text": str}],
                  "summary": str}
        messages: 本次被压缩掉的旧消息（只取 user/assistant 文本，工具机器态不入）
        tech: 技术名

    Returns:
        应用增量后的完整三舱 {"facts", "open_items", "summary"}。
        LLM 失败 / JSON 解析失败 → 三舱原样返回（宁可少记一窗增量，不拿既有积累冒险）。
    """
    facts = list(existing.get("facts") or [])
    open_items = list(existing.get("open_items") or [])
    old_summary = existing.get("summary") or ""

    parts = []
    for m in messages:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            parts.append(f"{m['role']}：{m['content']}")
    block = "\n".join(parts[-50:])  # 单次整理输入封顶
    if not block.strip():
        return {"facts": facts, "open_items": open_items, "summary": old_summary}

    facts_txt = "\n".join(f"- {f}" for f in facts) or "（无）"
    open_txt = "\n".join(f"- [{it['id']}] {it['text']}" for it in open_items) or "（无）"
    user_content = (f"技术：{tech}\n\n"
                    f"===== 现有稳定事实 =====\n{facts_txt}\n"
                    f"===== 现有未决事项 =====\n{open_txt}\n"
                    f"===== 刚发生的对话 =====\n{block}")
    try:
        raw = generate_text(CONSOLIDATE_MEMORY_PROMPT, user_content)
    except Exception:  # noqa: BLE001 —— LLM 不可用时三舱原样保留
        return {"facts": facts, "open_items": open_items, "summary": old_summary}
    obj = parse_json_object(raw)
    if not obj:
        return {"facts": facts, "open_items": open_items, "summary": old_summary}

    # 确定性应用：facts 去重追加
    known = {f.strip() for f in facts}
    for f in obj.get("facts_add") or []:
        f = str(f).strip()
        if f and f not in known:
            facts.append(f)
            known.add(f)
    # open 追加（id 全局递增）
    next_id = max((it.get("id") or 0) for it in open_items) if open_items else 0
    for t in obj.get("open_add") or []:
        t = str(t).strip()
        if t:
            next_id += 1
            open_items.append({"id": next_id, "text": t})
    # resolved 按 id 确定性淘汰（不存在的 id 忽略）
    resolved: set[int] = set()
    for r in obj.get("resolved") or []:
        try:
            resolved.add(int(r))
        except (TypeError, ValueError):
            continue
    open_items = [it for it in open_items if it.get("id") not in resolved]

    # 机械上限：facts / open 超限丢最旧；脉络舱字符上限兜底（防膨胀，原死配置补上机械约束）
    facts = facts[-config.COACH_FACTS_MAX:]
    open_items = sorted(open_items, key=lambda it: it.get("id") or 0)[-config.COACH_OPEN_MAX:]
    ctx = str(obj.get("context") or "").strip()
    summary = ctx[:config.COACH_SUMMARY_MAX_CHARS] if ctx else old_summary
    return {"facts": facts, "open_items": open_items, "summary": summary}
