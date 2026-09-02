"""学习路线（roadmap）纯业务规则：结构校验 / 构建 / 进度推进 / Markdown 渲染。

零 I/O、零框架依赖。ID（阶段 s1/s2、里程碑 s1-m1）与时间戳一律由代码确定性生成，
模型只提供内容字段（name/goal/materials/est_hours/milestones.desc）——
契合 PROMPT_DESIGN「确定性信息由代码生成，绝不交给模型」。

内部结构（roadmaps/<tech>.json 的机器态）：
    {
        "tech": "spring-boot",
        "goal": "能跑通一个最小项目",
        "total_hours": 40,
        "stages": [
            {"id": "s1", "name": "环境搭建", "goal": "...", "materials": "...",
             "est_hours": 4,
             "milestones": [{"id": "s1-m1", "desc": "本地能跑 hello world", "done": false}]}
        ],
        "current_stage": "s1",
        "status": "active",          # active / completed
        "created_at": "...",
        "updated_at": "...",
    }
"""

import json
import re
from datetime import datetime

from .extraction import parse_json_object

_MIN_STAGES = 1
_MIN_MILESTONES = 1

_TOP_REQUIRED = ("tech", "goal", "total_hours", "stages")
_STAGE_REQUIRED = ("name", "goal", "est_hours", "milestones")


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def _deepcopy(data):
    """纯数据深拷贝（路线内只含 JSON 可序列化值，走序列化最稳）。"""
    return json.loads(json.dumps(data, ensure_ascii=False))


def normalize_stages(raw_stages: list) -> tuple[list[dict], list[str]]:
    """把模型输出的 stages 规整为带确定性 id 的内部结构。

    合法字段：name / goal / est_hours（int） / materials（可选） /
    milestones（非空列表，每项含 desc）。非法 stage 整体丢弃并记 errors；
    合法 stage 的里程碑自动分配 id（s{n}-m{m}）并置 done=False。

    Returns:
        (合法 stages, 错误列表)
    """
    stages: list[dict] = []
    errors: list[str] = []
    for i, raw in enumerate(raw_stages, 1):
        if not isinstance(raw, dict):
            errors.append(f"阶段 {i} 不是对象")
            continue
        name = str(raw.get("name") or "").strip()
        goal = str(raw.get("goal") or "").strip()
        est_hours = raw.get("est_hours")
        milestones_raw = raw.get("milestones")

        stage_errors: list[str] = []
        if not name:
            stage_errors.append("缺少 name")
        if not goal:
            stage_errors.append("缺少 goal")
        if not isinstance(est_hours, int) or est_hours <= 0:
            stage_errors.append("est_hours 必须是正整数")
        if not isinstance(milestones_raw, list) or not milestones_raw:
            stage_errors.append("缺少非空 milestones")

        milestones: list[dict] = []
        for j, ms in enumerate(milestones_raw or [], 1):
            desc = str(ms.get("desc") or "").strip() if isinstance(ms, dict) else ""
            if desc:
                milestones.append({"id": f"s{len(stages) + 1}-m{j}", "desc": desc, "done": False})
        if len(milestones) < _MIN_MILESTONES:
            stage_errors.append("没有合法里程碑（每个阶段至少 1 个）")

        if stage_errors:
            errors.append(f"阶段 {i}：{'；'.join(stage_errors)}")
            continue

        stage = {
            "id": f"s{len(stages) + 1}",  # 按合法阶段计数连续分配，非法阶段被丢弃后不留空洞
            "name": name,
            "goal": goal,
            "est_hours": est_hours,
            "milestones": milestones,
        }
        if str(raw.get("materials") or "").strip():
            stage["materials"] = str(raw.get("materials")).strip()
        stages.append(stage)
    return stages, errors


def build_roadmap(tech: str, goal: str, total_hours: int, stages: list[dict],
                  *, created_at: str | None = None) -> dict:
    """用规范化后的 stages 组装完整路线（确定性字段由代码生成）。

    Args:
        tech: 技术名（原始大小写）
        goal: 学习总目标
        total_hours: 预估总时长（小时）
        stages: normalize_stages 的输出（非空）

    Returns:
        完整路线 dict（current_stage 指向第一阶段，status=active）
    """
    now = created_at or _now()
    return {
        "tech": tech,
        "goal": goal,
        "total_hours": total_hours,
        "stages": stages,
        "current_stage": stages[0]["id"],
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }


def validate_roadmap(roadmap: dict) -> list[str]:
    """校验路线整体结构是否合法（已落盘路线加载后的防御性校验）。

    Returns:
        错误列表，空列表 = 合法
    """
    errors: list[str] = []
    for k in _TOP_REQUIRED:
        if k not in roadmap:
            errors.append(f"缺少顶层字段 {k}")
    if errors:
        return errors
    if not isinstance(roadmap["stages"], list) or len(roadmap["stages"]) < _MIN_STAGES:
        errors.append("stages 必须是非空列表")
        return errors
    for s in roadmap["stages"]:
        for k in _STAGE_REQUIRED:
            if k not in s:
                errors.append(f"阶段 {s.get('id')} 缺少字段 {k}")
        if not isinstance(s.get("milestones"), list) or not s["milestones"]:
            errors.append(f"阶段 {s.get('id')} 缺少非空 milestones")
    return errors


def parse_roadmap_raw(raw: str, tech: str) -> tuple[dict | None, list[str]]:
    """解析模型输出的路线 JSON 文本并规范化。

    Returns:
        (roadmap | None, errors)：成功返回完整路线，失败返回 None + 错误列表
    """
    obj = parse_json_object(raw)
    if not obj:
        return None, ["模型输出不是合法 JSON 对象"]
    if not isinstance(obj.get("stages"), list) or not obj["stages"]:
        return None, ["stages 必须是非空列表"]
    goal = str(obj.get("goal") or "").strip()
    total_hours = obj.get("total_hours")
    if not goal:
        return None, ["缺少 goal"]
    if not isinstance(total_hours, int) or total_hours <= 0:
        return None, ["total_hours 必须是正整数"]
    stages, errors = normalize_stages(obj["stages"])
    if not stages:
        return None, errors or ["没有合法阶段"]
    return build_roadmap(tech, goal, total_hours, stages), []


def find_milestone(roadmap: dict, milestone_id: str) -> tuple[int, int] | None:
    """按里程碑 id 查找 (stage_index, milestone_index)；找不到返回 None。"""
    for si, stage in enumerate(roadmap.get("stages") or []):
        for mi, ms in enumerate(stage.get("milestones") or []):
            if ms.get("id") == milestone_id:
                return si, mi
    return None


def stage_done(roadmap: dict, stage: dict) -> bool:
    """阶段内所有里程碑是否完成（空里程碑视为未完成）。"""
    ms = stage.get("milestones") or []
    return bool(ms) and all(m.get("done") for m in ms)


def complete_milestone(roadmap: dict, milestone_id: str, done: bool = True) -> dict:
    """勾选/取消勾选里程碑，自动推进当前阶段。

    - 当前阶段全部里程碑完成后自动推进到下一阶段；
    - 最后阶段全部完成后 status=completed。
    找不到里程碑抛 KeyError（非法 id 由调用方捕获转成工具错误反馈给模型）。

    Args:
        roadmap: 原路线（不修改，返回新 dict）
        milestone_id: 里程碑 id（如 s1-m1）
        done: True 勾选 / False 取消

    Returns:
        更新后的路线副本
    """
    loc = find_milestone(roadmap, milestone_id)
    if loc is None:
        raise KeyError(milestone_id)
    si, mi = loc
    updated = _deepcopy(roadmap)
    updated["stages"][si]["milestones"][mi]["done"] = done

    # 当前阶段全部完成 → 推进；最后阶段完成 → 整条路线完成
    cur = updated.get("current_stage")
    stages = updated["stages"]
    if stages[si].get("id") == cur and stage_done(updated, stages[si]):
        if si + 1 < len(stages):
            updated["current_stage"] = stages[si + 1]["id"]
        else:
            updated["status"] = "completed"
    # 取消勾选时若路线已标 completed（勾满后又被取消），回退状态并指回第一个未完成阶段——
    # 否则状态与进度不一致（真实事故：显示「✅ 已完成」但 s2 里程碑未勾，误导 coaching）
    if not done and updated.get("status") == "completed":
        updated["status"] = "active"
        first_unfinished = next((s["id"] for s in stages if not stage_done(updated, s)), None)
        if first_unfinished:
            updated["current_stage"] = first_unfinished
    updated["updated_at"] = _now()
    return updated


def merge_progress(old: dict, new: dict) -> dict:
    """把旧路线已完成的里程碑进度合并进新路线（修订保留进度）。

    里程碑无唯一键（normalize_stages 只产 id/desc/done），故**按 desc（描述）完全匹配**：
    新路线中描述相同的里程碑继承旧勾选状态——阶段增删、改名、调时长都不影响已完成进度。
    合并后 current_stage 校正为第一个有未完成里程碑的阶段（全部完成 → status=completed）。

    Args:
        old: 旧路线（只读）
        new: 新路线（build_roadmap 产出，里程碑全 done=False、current_stage=s1）

    Returns:
        合并后的新路线副本（不修改入参）
    """
    result = _deepcopy(new)
    old_done = {
        m.get("desc")
        for s in (old.get("stages") or [])
        for m in (s.get("milestones") or [])
        if m.get("done")
    }
    for stage in result.get("stages") or []:
        for m in stage.get("milestones") or []:
            if m.get("desc") in old_done:
                m["done"] = True
    # 校正当前阶段到第一个未完成阶段（build 已置 s1；全完成则保持首阶段但 status=completed）
    first_unfinished = next((s["id"] for s in (result.get("stages") or [])
                             if not stage_done(result, s)), None)
    if first_unfinished:
        result["current_stage"] = first_unfinished
        result["status"] = "active"
    else:
        result["status"] = "completed"
    result["updated_at"] = _now()
    return result


def stage_progress(roadmap: dict, stage_id: str | None = None) -> dict:
    """统计某阶段（默认当前阶段）的里程碑完成进度。

    Returns:
        {"stage_id", "name", "done", "total", "pct"}（total=0 时 pct=0）
    """
    stages = roadmap.get("stages") or []
    sid = stage_id or roadmap.get("current_stage")
    stage = next((s for s in stages if s.get("id") == sid), None) or {}
    ms = stage.get("milestones") or []
    done = sum(1 for m in ms if m.get("done"))
    total = len(ms)
    return {"stage_id": sid, "name": stage.get("name", ""), "done": done,
            "total": total, "pct": round(done / total * 100) if total else 0}


def roadmap_to_markdown(roadmap: dict) -> str:
    """渲染成人可读的 Markdown（roadmaps/<tech>-roadmap.md 的「Markdown 是源」产物）。"""
    tech = roadmap.get("tech") or ""
    lines = [
        f"# {tech} 学习路线",
        "",
        f"> 目标：{roadmap.get('goal') or ''}",
        (f"> 预估总时长：{roadmap.get('total_hours') or 0} 小时 ｜ "
        f"状态：{'✅ 已完成' if roadmap.get('status') == 'completed' else '进行中'}"),
        f"> 当前阶段：{_stage_name(roadmap, roadmap.get('current_stage'))}",
        "",
        "## 阶段总览",
        "",
    ]
    for s in roadmap.get("stages") or []:
        mark = "✅" if stage_done(roadmap, s) else "⬜"
        cur = "（当前）" if s.get("id") == roadmap.get("current_stage") else ""
        lines.append(f"- {mark} **{s.get('name')}**{cur}（约 {s.get('est_hours')} 小时）— {s.get('goal') or ''}")
    lines += ["", "## 里程碑", ""]
    for s in roadmap.get("stages") or []:
        lines.append(f"### {s.get('id')} {s.get('name')}")
        if s.get("materials"):
            lines.append(f"- 资料：{s['materials']}")
        for m in s.get("milestones") or []:
            box = "[x]" if m.get("done") else "[ ]"
            lines.append(f"- {box} {m.get('desc')}")
        lines.append("")
    return "\n".join(lines)


def _stage_name(roadmap: dict, stage_id: str | None) -> str:
    for s in roadmap.get("stages") or []:
        if s.get("id") == stage_id:
            return s.get("name") or stage_id or ""
    return stage_id or ""


def extract_hours(text: str) -> float | None:
    """从自由文本中提取数字小时数（问卷/对话里用户说「每天2小时」用）。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*小时", text or "")
    return float(m.group(1)) if m else None
