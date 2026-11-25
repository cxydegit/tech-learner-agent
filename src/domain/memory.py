"""长期记忆纯业务规则：事件折叠、确定性 id、能力投影、注入渲染。

零 I/O、零框架依赖。事件日志是唯一真源，视图是它的确定性投影：同一份事件按
文件顺序重放，结果逐字段一致——折叠过程不调用 LLM，也不读取处理时刻的
``now()``（``now`` 只作为显式参数用于视图 ``updated_at`` 与 ``expires_at`` 过滤）。

能力身份是 ``(tech_key, 归一化 point)``，不是 ``milestone_id``：路线修订/重新
生成会重排 id，而项目现有的 merge_progress 也是按里程碑描述匹配进度。
"""

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime

from .dedup import sanitize_filename

VIEW_VERSION = 1

# 证据等级（强 → 弱）。同一能力点只允许升级，不允许降级：历史迁移的 unknown
# 不会覆盖后来真实验收出来的 verified。
EVIDENCE_LEVELS = ("verified", "claimed", "degraded", "unknown")
_EVIDENCE_RANK = {"unknown": 0, "degraded": 1, "claimed": 2, "verified": 3}

PROFILE_CATEGORIES = ("preference", "fact", "goal")
SCOPES = ("global", "tech")

# 偏好 key 受控词表：语义判断不了"同义"，就用固定 key 让同义信息收敛到一条。
PREFERENCE_KEYS = (
    "verbosity", "terminology", "analogy", "code_first", "pace",
    "interaction", "language", "format", "other",
)

MAX_TEXT_CHARS = 200

# 摘要里只带最近几个掌握点，且每个点截断：里程碑描述可能很长（实测上百字），
# 不截断会让跨技术底账一行吃掉整个字符预算，后面的技术全被裁掉。
_SUMMARY_POINTS = 3
_SUMMARY_POINT_CHARS = 18

_FACT_KEY_RE = re.compile(r"^[a-z0-9_]+(?::[a-z0-9_]+)*$")


# ============================================================
# 时间与 id
# ============================================================


def local_now() -> datetime:
    """带时区的本地当前时间（新事件的默认时间戳）。"""
    return datetime.now().astimezone()


def parse_time(value) -> datetime | None:
    """解析 ISO 时间；兼容 roadmap 的 ``YYYY-MM-DD HH:MM`` 与无时区旧值。

    无时区值按本地时间处理（与项目其他时间字段的本地 naive 语义一致）。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def format_time(dt: datetime) -> str:
    """事件/视图统一时间格式：带时区的本地 ISO 8601（秒精度）。"""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def format_date(dt: datetime) -> str:
    return dt.date().isoformat()


def _as_datetime(value) -> datetime:
    dt = value if isinstance(value, datetime) else parse_time(value)
    if dt is None:
        dt = local_now()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def new_event_id(at: datetime | None = None) -> str:
    """事件 id：时间前缀便于人读，随机后缀保证唯一。幂等靠折叠而非 id 去重。"""
    dt = _as_datetime(at)
    return f"evt-{dt.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def make_event(event_type: str, *, at=None, session: str = "", source: str = "system",
               event_id: str | None = None, **fields) -> dict:
    """构造事件（公共信封 + 业务字段；None 字段不落盘）。"""
    dt = _as_datetime(at)
    event = {
        "id": event_id or new_event_id(dt),
        "type": event_type,
        "at": format_time(dt),
        "session": session,
        "source": source,
    }
    event.update({k: v for k, v in fields.items() if v is not None})
    return event


# ============================================================
# 名称归一化与确定性 id
# ============================================================


def tech_key(tech: str) -> str:
    """技术存储键：与 roadmap/profile 文件名同一口径（sanitize_filename）。"""
    return sanitize_filename(str(tech or ""))


def clean_text(text) -> str:
    """展示文本：NFKC + 合并空白 + 去首尾空白（保留大小写与标点）。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", " ", value).strip()


def _cmp_text(text) -> str:
    """文本相等判定口径：展示文本再 casefold（不做语义相似度判断）。"""
    return clean_text(text).casefold()


def normalize_point(text) -> str:
    """里程碑描述的身份口径：NFKC + 合并空白 + casefold。"""
    return _cmp_text(text)


def texts_equal(a, b) -> bool:
    """两段记忆文本是否算“同一内容”（折叠与冲突报告共用同一口径）。"""
    return _cmp_text(a) == _cmp_text(b)


def _sha10(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def capability_id(tech: str, point: str) -> str:
    return f"cap:{tech_key(tech)}:{_sha10(normalize_point(point))}"


def profile_id(category: str, scope: str, tech: str, key: str) -> str:
    """确定性条目 id：global 用 ``pref:global:<key>``，tech 用 ``pref:tech:<tk>:<key>``。"""
    suffix = f":{tech_key(tech)}" if scope == "tech" else ""
    if category == "goal":
        return f"goal:{scope}{suffix}"
    prefix = "pref" if category == "preference" else "fact"
    return f"{prefix}:{scope}{suffix}:{key}"


def open_item_id(tech: str, text: str) -> str:
    return f"open:{tech_key(tech)}:{_sha10(_cmp_text(text))}"


# ============================================================
# 校验（remember 工具 / 迁移共用）
# ============================================================


def validate_profile_item(category: str, scope: str, tech: str, key: str,
                          text: str) -> str | None:
    """校验偏好/事实/目标条目；返回错误文案，合法返回 None。"""
    if category not in PROFILE_CATEGORIES:
        return "category 必须是 preference / fact / goal"
    if scope not in SCOPES:
        return "scope 必须是 global / tech"
    if scope == "tech" and not str(tech or "").strip():
        return "scope=tech 时 tech 必填"
    text = str(text or "").strip()
    if not text:
        return "text 不能为空"
    if len(text) > MAX_TEXT_CHARS:
        return f"text 不能超过 {MAX_TEXT_CHARS} 字"
    key = str(key or "").strip()
    if not key:
        return "key 不能为空"
    if category == "preference" and key not in PREFERENCE_KEYS:
        return "偏好 key 必须是：" + " / ".join(PREFERENCE_KEYS)
    if category == "fact" and not _FACT_KEY_RE.match(key):
        return "事实 key 必须是 ASCII slug（如 constraint:java_version）"
    if category == "goal" and key != "goal":
        return "目标 key 固定为 goal"
    return None


# ============================================================
# 折叠：事件日志 → 物化视图
# ============================================================


def empty_view() -> dict:
    return {
        "version": VIEW_VERSION,
        "updated_at": "",
        "last_event_id": None,
        "capabilities": {},
        "preferences": [],
        "facts": [],
        "goals": {"global": [], "by_tech": {}},
        "open_items": [],
    }


def view_is_empty(view: dict) -> bool:
    if not view:
        return True
    goals = view.get("goals") or {}
    return not (view.get("capabilities") or view.get("preferences") or view.get("facts")
                or goals.get("global") or goals.get("by_tech") or view.get("open_items"))


def evidence_rank(level: str) -> int:
    return _EVIDENCE_RANK.get(str(level or ""), 0)


def _limit(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _make_profile_entry(category: str, scope: str, tech: str, key: str, text: str,
                        source: str, source_event_id: str, at_date: str,
                        old: dict | None) -> dict:
    """按同 key 归并：同文本只刷新确认时间；不同文本覆盖并记录 supersedes 链。"""
    same = old is not None and _cmp_text(old.get("text")) == _cmp_text(text)
    if old is None:
        created = at_date
        supersedes = None
    elif same:
        created = old.get("created_at") or at_date
        supersedes = old.get("supersedes")
    else:
        created = old.get("created_at") or at_date
        supersedes = old.get("source_event_id")
    return {
        "id": profile_id(category, scope, tech, key),
        "source_event_id": source_event_id,
        "key": key,
        "text": text,
        "scope": scope,
        "tech": tech,
        "tech_key": tech_key(tech) if scope == "tech" else "",
        "source": source,
        "created_at": created,
        "last_confirmed": at_date,
        "status": "active",
        "supersedes": supersedes,
    }


def _entry_sort_key(entry: dict) -> tuple:
    return (0 if entry.get("scope") == "global" else 1,
            entry.get("tech_key") or "", entry.get("key") or "")


def _cap_list(items: list[dict], limit: int, kind: str, archived: list[dict],
              now_iso: str) -> list[dict]:
    """按域上限裁剪：最旧（last_confirmed / created_at 最早）的进归档，视图保持有界。"""
    if not limit or len(items) <= limit:
        return items
    ordered = sorted(
        items,
        key=lambda e: (str(e.get("last_confirmed") or e.get("created_at") or ""),
                       str(e.get("id") or "")))
    keep_ids = {e.get("id") for e in ordered[-limit:]}
    kept = []
    for entry in items:
        if entry.get("id") in keep_ids:
            kept.append(entry)
        else:
            archived.append({**entry, "kind": kind, "archived_at": now_iso})
    return kept


def _short_point(point: str) -> str:
    text = clean_text(point)
    if len(text) <= _SUMMARY_POINT_CHARS:
        return text
    return text[:_SUMMARY_POINT_CHARS - 1].rstrip() + "…"


def _roadmap_last_date(roadmap: dict | None) -> str | None:
    if not roadmap:
        return None
    for field in ("updated_at", "created_at"):
        dt = parse_time(roadmap.get(field))
        if dt:
            return format_date(dt)
    return None


def fold_events(events: list[dict], *, roadmaps: dict[str, dict] | None = None,
                now=None, caps: dict | None = None) -> tuple[dict, list[dict]]:
    """把事件日志确定性折叠成物化视图。

    Args:
        events: 事件列表（按日志文件顺序；顺序即真相，不按 ``at`` 重排）
        roadmaps: ``{tech_key: roadmap}``，用于能力投影里补总里程碑数与完成度
        now: 视图 ``updated_at`` 与归档时间（测试传固定值保证逐字段可复现）
        caps: 每个域的上限（0 / 缺省 = 不限；由 adapters 从 config 传入）

    Returns:
        (view, archived)，archived 为超上限条目（adapter 追加到归档冷存储）。
    """
    now_dt = _as_datetime(now)
    now_iso = format_time(now_dt)
    caps = caps or {}
    max_pref = _limit(caps.get("max_preferences"))
    max_facts = _limit(caps.get("max_facts"))
    max_mastered = _limit(caps.get("max_mastered_per_tech"))
    max_open = _limit(caps.get("max_open_items"))
    max_goals = _limit(caps.get("max_goals"))

    cap_state: dict[tuple[str, str], dict] = {}
    diagnostics: dict[str, dict] = {}
    profile_map: dict[tuple[str, str, str, str], dict] = {}
    goal_map: dict[tuple[str, str], dict] = {}
    open_items: dict[str, dict] = {}
    tech_names: dict[str, str] = {}
    last_event_id: str | None = None

    def register_tech(tech: str) -> str:
        tk = tech_key(tech)
        if tk:
            tech_names[tk] = str(tech or "").strip() or tech_names.get(tk, tk)
        return tk

    for event in events or []:
        if not isinstance(event, dict):
            continue
        source_event_id = str(event.get("id") or "")
        if source_event_id:
            last_event_id = source_event_id
        event_type = str(event.get("type") or "")
        at_dt = parse_time(event.get("at")) or now_dt
        at_date = format_date(at_dt)
        source = str(event.get("source") or "unknown")

        if event_type == "capability_recorded":
            tech = str(event.get("tech") or "").strip()
            point = clean_text(event.get("point"))
            op = str(event.get("op") or "")
            if not tech or not point or op not in ("add", "revoke"):
                continue
            tk = register_tech(tech)
            if not tk:
                continue
            key = (tk, normalize_point(point))
            rec = cap_state.get(key)
            if rec is None:
                rec = {
                    "id": capability_id(tech, point),
                    "tech": tech,
                    "tech_key": tk,
                    "point": point,
                    "evidence": "unknown",
                    "active": False,
                    "at": at_date,
                    "first_at": at_date,
                    "last_activity": at_date,
                    "milestone_id": None,
                    "source": source,
                    "source_event_id": source_event_id,
                }
                cap_state[key] = rec
            rec["tech"] = tech
            rec["point"] = point
            rec["last_activity"] = at_date
            rec["last_confirmed"] = at_date
            rec["source"] = source
            rec["source_event_id"] = source_event_id
            if event.get("milestone_id"):
                rec["milestone_id"] = str(event["milestone_id"])
            if op == "add":
                evidence = str(event.get("evidence") or "unknown")
                if evidence not in EVIDENCE_LEVELS:
                    evidence = "unknown"
                if not rec["active"]:
                    rec["active"] = True
                    rec["evidence"] = evidence
                    rec["first_at"] = at_date
                elif evidence_rank(evidence) > evidence_rank(rec["evidence"]):
                    rec["evidence"] = evidence
                rec["at"] = at_date
            else:
                rec["active"] = False

        elif event_type == "diagnostic_recorded":
            tech = str(event.get("tech") or "").strip()
            tk = register_tech(tech)
            asked = _as_int(event.get("asked"))
            right = _as_int(event.get("right"))
            if not tk or asked is None or right is None:
                continue
            asked = max(asked, 0)
            right = min(max(right, 0), asked)
            # 最近一次问卷的诊断结果覆盖旧值（不累加）：重复调用 generate_roadmap
            # 会重复写同一批诊断，累加会把“2 题 1 对”变成“6 题 3 对”。
            diagnostics[tk] = {"asked": asked, "right": right, "at": at_date}

        elif event_type == "profile_recorded":
            category = str(event.get("category") or "")
            scope = str(event.get("scope") or "")
            key = str(event.get("key") or "").strip()
            text = clean_text(event.get("text"))
            if category not in PROFILE_CATEGORIES or scope not in SCOPES or not key or not text:
                continue
            if scope == "tech":
                tech = str(event.get("tech") or "").strip()
                tk = register_tech(tech)
                if not tk:
                    continue
            else:
                tech, tk = "", ""
            if category == "goal":
                gkey = (scope, tk)
                goal_map[gkey] = _make_profile_entry(
                    "goal", scope, tech, "goal", text, source, source_event_id,
                    at_date, goal_map.get(gkey))
            else:
                pkey = (category, scope, tk, key)
                profile_map[pkey] = _make_profile_entry(
                    category, scope, tech, key, text, source, source_event_id,
                    at_date, profile_map.get(pkey))

        elif event_type == "open_item_recorded":
            op = str(event.get("op") or "")
            item_id = str(event.get("item_id") or "").strip()
            if op == "close":
                if item_id:
                    open_items.pop(item_id, None)
                continue
            if op != "open":
                continue
            tech = str(event.get("tech") or "").strip()
            tk = register_tech(tech)
            text = clean_text(event.get("text"))
            if not tk or not text:
                continue
            if not item_id:
                item_id = open_item_id(tech, text)
            old = open_items.get(item_id) or {}
            open_items[item_id] = {
                "id": item_id,
                "source_event_id": source_event_id,
                "tech": tech,
                "tech_key": tk,
                "text": text,
                "created_at": old.get("created_at") or at_date,
                "last_confirmed": at_date,
                "expires_at": str(event.get("expires_at") or "") or None,
                "status": "open",
            }

        elif event_type == "memory_forgotten":
            target = str(event.get("target_id") or "").strip()
            if target:
                for key, entry in list(profile_map.items()):
                    if entry.get("id") == target:
                        profile_map.pop(key, None)
                for key, entry in list(goal_map.items()):
                    if entry.get("id") == target:
                        goal_map.pop(key, None)
                open_items.pop(target, None)
            else:
                category = str(event.get("category") or "")
                scope = str(event.get("scope") or "")
                key = str(event.get("key") or "").strip()
                tk = tech_key(event.get("tech") or "") if scope == "tech" else ""
                if category in ("preference", "fact") and scope in SCOPES and key:
                    profile_map.pop((category, scope, tk, key), None)
                elif category == "goal" and scope in SCOPES:
                    goal_map.pop((scope, tk), None)
            # v1 不处理能力点：能力否认走 update_roadmap(done=false)。

    archived: list[dict] = []
    preferences = _cap_list(
        sorted([e for k, e in profile_map.items() if k[0] == "preference"],
               key=_entry_sort_key), max_pref, "preference", archived, now_iso)
    facts = _cap_list(
        sorted([e for k, e in profile_map.items() if k[0] == "fact"],
               key=_entry_sort_key), max_facts, "fact", archived, now_iso)

    goals_global: list[dict] = []
    goals_by_tech: dict[str, list[dict]] = {}
    for (scope, tk), entry in sorted(goal_map.items(),
                                     key=lambda kv: (kv[0][0], kv[0][1])):
        if scope == "global":
            goals_global.append(entry)
        else:
            goals_by_tech.setdefault(tk, []).append(entry)
    goals_global = _cap_list(goals_global, max_goals, "goal", archived, now_iso)
    for tk in list(goals_by_tech):
        goals_by_tech[tk] = _cap_list(goals_by_tech[tk], max_goals, "goal",
                                      archived, now_iso)

    open_list = _cap_list(
        sorted(open_items.values(),
               key=lambda e: (e.get("tech_key") or "", e.get("created_at") or "",
                              e.get("id") or "")),
        max_open, "open_item", archived, now_iso)

    roadmaps = roadmaps or {}
    all_techs = set(tech_names) | set(roadmaps) | set(diagnostics) | set(goals_by_tech)
    all_techs |= {e.get("tech_key") for e in preferences + facts + open_list
                  if e.get("tech_key")}
    cap_blocks: dict[str, dict] = {}

    for tk in sorted(all_techs):
        if not tk:
            continue
        roadmap = roadmaps.get(tk)
        recs = [rec for (key, rec) in cap_state.items() if key[0] == tk]
        mastered = sorted([r for r in recs if r["active"]],
                          key=lambda r: (r.get("at") or "", r.get("point") or ""))
        if max_mastered and len(mastered) > max_mastered:
            keep_ids = {r["id"] for r in sorted(
                mastered, key=lambda r: (r.get("at") or "", r["id"]))[-max_mastered:]}
            kept = []
            for rec in mastered:
                if rec["id"] in keep_ids:
                    kept.append(rec)
                else:
                    archived.append({**rec, "kind": "capability",
                                     "archived_at": now_iso})
            mastered = kept

        total = done = 0
        if roadmap:
            for stage in roadmap.get("stages") or []:
                for milestone in stage.get("milestones") or []:
                    total += 1
                    if milestone.get("done"):
                        done += 1

        tech_display = (tech_names.get(tk)
                        or (str(roadmap.get("tech") or "") if roadmap else "")
                        or tk)
        diag = diagnostics.get(tk) or {}

        dates = [r.get("last_activity") for r in recs if r.get("last_activity")]
        dates += [e.get("last_confirmed") for e in preferences + facts
                  if e.get("tech_key") == tk and e.get("last_confirmed")]
        dates += [e.get("last_confirmed") for e in goals_by_tech.get(tk) or []
                  if e.get("last_confirmed")]
        if diag.get("at"):
            dates.append(diag["at"])
        last_active = max(dates) if dates else _roadmap_last_date(roadmap)

        parts: list[str] = []
        if total:
            parts.append(f"完成 {done}/{total} 里程碑")
        elif mastered:
            parts.append(f"已记录 {len(mastered)} 个掌握点")
        else:
            parts.append("暂无学习记录")
        if mastered:
            points = "、".join(_short_point(m["point"])
                               for m in mastered[-_SUMMARY_POINTS:])
            if len(mastered) > _SUMMARY_POINTS:
                points += " 等"
            parts.append(f"已掌握：{points}")
        if diag.get("asked"):
            parts.append(f"诊断 {diag['asked']} 题中 {diag['right']} 对")
        if last_active:
            parts.append(f"最近学习 {last_active}")
        if not roadmap and recs:
            parts.append("（路线已不在）")

        block = {
            "tech": tech_display,
            "last_active": last_active,
            "summary": "；".join(parts),
            "mastered": [{
                "id": rec["id"],
                "source_event_id": rec["source_event_id"],
                "point": rec["point"],
                "evidence": rec["evidence"],
                "at": rec["at"],
                "first_at": rec["first_at"],
                "milestone_id": rec.get("milestone_id"),
                "status": "active",
            } for rec in mastered],
            "route_present": bool(roadmap),
        }
        if diag.get("asked"):
            block["diagnostics"] = {
                "asked": diag["asked"], "right": diag["right"],
                "at": diag.get("at"),
            }
        cap_blocks[tk] = block

    return {
        "version": VIEW_VERSION,
        "updated_at": now_iso,
        "last_event_id": last_event_id,
        "capabilities": cap_blocks,
        "preferences": preferences,
        "facts": facts,
        "goals": {"global": goals_global, "by_tech": goals_by_tech},
        "open_items": open_list,
    }, archived
