"""任务简报（context model）：把长期记忆视图按 mode 选成模型真正需要的差集。

边界：
- 纯函数、零 I/O、零框架依赖；不读文件、不调 LLM。
- 只负责「选哪些记忆、以什么粒度出现」；提示词位置/规则行由 pipelines 负责。
- 与 `memory.py` 的分工：memory.py 负责事件折叠/视图语义；本模块负责读取策略。

模式契约（见 Phase 0）：
- survey：当前技术具体证据点 + 其他技术各 top N 具体点 + 全局偏好/约束；
  不放 roadmap、诊断计数、计数-only。
- planning：只放当前技术具体证据点（current/historical、evidence）；
  不放其他技术、诊断、计数-only。
- coaching：只放 roadmap 没有的 delta：具体异常里程碑、技术偏好/事实/未决项、
  全局偏好/约束；不放当前路线清单/完成数、其他技术、历史点、诊断。
"""

from datetime import datetime

from . import memory as memory_domain

MODES = ("survey", "planning", "coaching")

_EVIDENCE_ORDER = {
    "verified": 0,
    "claimed": 1,
    "degraded": 2,
    "unknown": 3,
}
_EVIDENCE_LABEL = {
    "verified": "已验收",
    "claimed": "已声明",
    "degraded": "验收降级",
    "unknown": "未考据",
}
_PROFILE_KEYS = ("self_level", "related", "time_budget", "goal")

_DEFAULT_LIMITS = {
    "survey": {"current_points": 8, "other_points": 3,
               "max_global_items": 8, "max_chars": 1500},
    "planning": {"current_points": 8, "max_global_items": 8,
                 "max_chars": 600},
    "coaching": {"max_global_items": 8, "max_chars": 300},
}


def _limits(mode: str, limits: dict | None) -> dict:
    base = dict(_DEFAULT_LIMITS.get(mode, {}))
    if limits:
        for key, value in limits.items():
            if value is not None:
                base[key] = value
    return base


def _ts(value) -> float:
    dt = memory_domain.parse_time(value)
    return dt.timestamp() if dt else 0.0


def _now_dt(now) -> datetime:
    dt = memory_domain.parse_time(now) or memory_domain.local_now()
    return dt


def _evidence_rank(value) -> int:
    return _EVIDENCE_ORDER.get(value, _EVIDENCE_ORDER["unknown"])


def _evidence_label(value) -> str:
    return _EVIDENCE_LABEL.get(value, _EVIDENCE_LABEL["unknown"])


def _text(value) -> str:
    return memory_domain.clean_text(value)


def _covered(text: str, covered: list[str] | None) -> bool:
    return any(memory_domain.texts_equal(text, item) for item in (covered or []))


def _profile_keys(profile: dict | None) -> set[str]:
    """当前 profile_summary 已经渲染过的字段；briefing 不重复。"""
    profile = profile or {}
    keys: set[str] = set()
    if profile.get("self_level") is not None:
        keys.add("self_level")
    if str(profile.get("related") or "").strip():
        keys.add("related")
    if profile.get("time_budget") is not None:
        keys.add("time_budget")
    if str(profile.get("goal") or "").strip():
        keys.add("goal")
    return keys


def _route_index(roadmap: dict | None) -> dict[str, dict]:
    """当前 roadmap 的归一化 point → milestone；用于 current/historical 判定。"""
    index: dict[str, dict] = {}
    for stage in (roadmap or {}).get("stages") or []:
        for milestone in stage.get("milestones") or []:
            point = _text(milestone.get("desc"))
            if point:
                index[memory_domain.normalize_point(point)] = milestone
    return index


def _route_scope(cap: dict, route_index: dict[str, dict],
                 roadmap: dict | None) -> str:
    if not roadmap:
        return "historical"
    point = memory_domain.normalize_point(cap.get("point"))
    return "current" if point in route_index else "historical"


def _cap_item(cap: dict, route_scope: str, *, tech: str = "") -> dict:
    item = {
        "kind": "capability",
        "id": cap.get("id"),
        "text": _text(cap.get("point")),
        "evidence": cap.get("evidence") or "unknown",
        "route_scope": route_scope,
        "at": cap.get("at"),
    }
    if tech:
        item["tech"] = tech
    return item


def _cap_items(caps: list[dict], route_index: dict[str, dict],
               roadmap: dict | None, limit: int,
               *, tech: str = "", prefer_current: bool = True
               ) -> tuple[list[dict], list[dict]]:
    items = [
        _cap_item(cap, _route_scope(cap, route_index, roadmap), tech=tech)
        for cap in (caps or [])
        if _text(cap.get("point"))
    ]
    def sort_key(item: dict):
        current = 0 if (not prefer_current or item["route_scope"] == "current") else 1
        return (current, _evidence_rank(item["evidence"]),
                -_ts(item.get("at")), item["text"])
    items.sort(key=sort_key)
    dropped: list[dict] = []
    if limit and limit > 0 and len(items) > limit:
        for item in items[limit:]:
            dropped.append({"id": item.get("id"), "kind": "capability",
                            "reason": "n_cut"})
        items = items[:limit]
    return items, dropped


def _planning_cap_items(caps: list[dict], route_index: dict[str, dict],
                        roadmap: dict | None, limit: int,
                        *, tech: str = "") -> tuple[list[dict], list[dict]]:
    """planning 专用：当前路线优先，但给历史未考据点保留待验证名额。

    不能简单按 (current, evidence) 排序截断：当前路线已验收点很多时，
    历史未考据点会被全部挤掉，规划就看不到「哪些要补验证」。
    """
    items = [
        _cap_item(cap, _route_scope(cap, route_index, roadmap), tech=tech)
        for cap in (caps or [])
        if _text(cap.get("point"))
    ]
    def sort_key(item: dict):
        return (_evidence_rank(item["evidence"]), -_ts(item.get("at")),
                item["text"])
    current_ok = sorted(
        [it for it in items if it["route_scope"] == "current"
         and it["evidence"] in ("verified", "claimed")], key=sort_key)
    current_bad = sorted(
        [it for it in items if it["route_scope"] == "current"
         and it["evidence"] not in ("verified", "claimed")], key=sort_key)
    historical_bad = sorted(
        [it for it in items if it["route_scope"] == "historical"
         and it["evidence"] not in ("verified", "claimed")], key=sort_key)
    historical_ok = sorted(
        [it for it in items if it["route_scope"] == "historical"
         and it["evidence"] in ("verified", "claimed")], key=sort_key)

    selected: list[dict] = []
    seen: set[str] = set()

    def take(pool: list[dict], max_add: int | None = None) -> None:
        added = 0
        for item in pool:
            if limit and len(selected) >= limit:
                return
            if max_add is not None and added >= max_add:
                return
            key = item.get("id") or item.get("text")
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            added += 1

    # 给未考据点预留最多 2 个名额（当前路线未考据优先，其次历史未考据），
    # 再让当前已验收点填剩余；不足时按历史已验收 → 当前已验收补齐。
    unverified = len(current_bad) + len(historical_bad)
    reserve = min(2, unverified, limit) if limit and limit > 0 else 0
    current_quota = (limit - reserve) if limit and limit > 0 else None
    take(current_ok, max_add=current_quota)
    take(current_bad)
    take(historical_bad)
    take(historical_ok)
    take(current_ok)
    if limit and limit > 0:
        selected = selected[:limit]
    dropped: list[dict] = []
    if limit and limit > 0 and len(items) > len(selected):
        selected_keys = {item.get("id") or item.get("text") for item in selected}
        for item in items:
            if (item.get("id") or item.get("text")) not in selected_keys:
                dropped.append({"id": item.get("id"), "kind": "capability",
                                "reason": "n_cut"})
    return selected, dropped


def _global_items(view: dict, kinds: tuple[str, ...],
                  covered: list[str] | None, limit: int) -> tuple[list[dict], list[dict]]:
    """全局偏好/事实/目标；只取 scope=global，按条目截断（不切文本）。"""
    rows: list[dict] = []
    dropped: list[dict] = []
    for kind in kinds:
        if kind == "preference":
            source = view.get("preferences") or []
        elif kind == "fact":
            source = view.get("facts") or []
        else:
            source = (view.get("goals") or {}).get("global") or []
        for entry in source:
            if kind != "goal" and (entry.get("scope") or "tech") != "global":
                continue
            text = _text(entry.get("text"))
            if not text:
                continue
            if _covered(text, covered):
                dropped.append({"id": entry.get("id"), "kind": kind,
                                "reason": "covered"})
                continue
            rows.append({
                "kind": kind,
                "id": entry.get("id"),
                "key": entry.get("key") or "",
                "text": text,
                "last_confirmed": entry.get("last_confirmed"),
            })
    kind_order = {kind: i for i, kind in enumerate(kinds)}
    rows.sort(key=lambda it: (kind_order.get(it["kind"], 9),
                             it.get("key") or "", -_ts(it.get("last_confirmed"))))
    if limit and limit > 0 and len(rows) > limit:
        for item in rows[limit:]:
            dropped.append({"id": item.get("id"), "kind": item["kind"],
                            "reason": "limit"})
        rows = rows[:limit]
    return rows, dropped


def _tech_profile_items(view: dict, tech_key: str, profile: dict | None,
                        covered: list[str] | None,
                        limit: int) -> tuple[list[dict], list[dict]]:
    """coaching 用：技术偏好/事实，跳过 profile 已渲染字段与 coach_facts 已覆盖文本。"""
    keys = _profile_keys(profile)
    rows: list[dict] = []
    dropped: list[dict] = []
    for source, kind in (((view.get("preferences") or []), "preference"),
                         ((view.get("facts") or []), "fact")):
        for entry in source:
            if (entry.get("scope") or "tech") != "tech":
                continue
            if entry.get("tech_key") != tech_key:
                continue
            key = entry.get("key") or ""
            if key in keys or key == "goal":
                dropped.append({"id": entry.get("id"), "kind": kind,
                                "reason": "covered"})
                continue
            text = _text(entry.get("text"))
            if not text:
                continue
            if _covered(text, covered):
                dropped.append({"id": entry.get("id"), "kind": kind,
                                "reason": "covered"})
                continue
            rows.append({
                "kind": kind,
                "id": entry.get("id"),
                "key": key,
                "text": text,
                "last_confirmed": entry.get("last_confirmed"),
            })
    rows.sort(key=lambda it: (0 if it["kind"] == "preference" else 1,
                             it.get("key") or "", -_ts(it.get("last_confirmed"))))
    if limit and limit > 0 and len(rows) > limit:
        for item in rows[limit:]:
            dropped.append({"id": item.get("id"), "kind": item["kind"],
                            "reason": "limit"})
        rows = rows[:limit]
    return rows, dropped


def _open_items(view: dict, tech_key: str, now_dt: datetime,
                covered: list[str] | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    dropped: list[dict] = []
    for entry in view.get("open_items") or []:
        if entry.get("tech_key") != tech_key:
            continue
        text = _text(entry.get("text"))
        if not text:
            continue
        expires = memory_domain.parse_time(entry.get("expires_at"))
        if expires is not None and expires < now_dt:
            dropped.append({"id": entry.get("id"), "kind": "open_item",
                            "reason": "expired"})
            continue
        if _covered(text, covered):
            dropped.append({"id": entry.get("id"), "kind": "open_item",
                            "reason": "covered"})
            continue
        items.append({
            "kind": "open_item",
            "id": entry.get("id"),
            "text": text,
            "expires_at": entry.get("expires_at"),
        })
    items.sort(key=lambda it: (it.get("expires_at") or "", it.get("id") or ""))
    return items, dropped


def _route_exceptions(roadmap: dict | None, caps: list[dict]) -> list[dict]:
    """当前 roadmap 已 done、但证据不是 verified / 没有有效证据的里程碑。

    只点名具体里程碑，不输出完成数；这是 coaching 相对 roadmap 的 delta。
    """
    if not roadmap:
        return []
    strongest: dict[str, dict] = {}
    for cap in caps or []:
        point = memory_domain.normalize_point(cap.get("point"))
        if not point:
            continue
        old = strongest.get(point)
        if old is None or _evidence_rank(cap.get("evidence")) < _evidence_rank(old.get("evidence")):
            strongest[point] = cap
    out: list[dict] = []
    for stage in roadmap.get("stages") or []:
        for milestone in stage.get("milestones") or []:
            if not milestone.get("done"):
                continue
            desc = _text(milestone.get("desc"))
            if not desc:
                continue
            cap = strongest.get(memory_domain.normalize_point(desc))
            if cap is None:
                out.append({
                    "kind": "exception",
                    "id": f"route:{milestone.get('id') or ''}",
                    "milestone_id": milestone.get("id"),
                    "text": desc,
                    "evidence": "missing",
                    "reason": "记忆中没有有效证据（可能已撤销）",
                })
            elif cap.get("evidence") != "verified":
                level = cap.get("evidence") or "unknown"
                out.append({
                    "kind": "exception",
                    "id": f"route:{milestone.get('id') or ''}",
                    "milestone_id": milestone.get("id"),
                    "text": desc,
                    "evidence": level,
                    "reason": f"证据为{_evidence_label(level)}，未通过验收",
                })
    return out


def _add_dropped(dropped: list[dict], item: dict, reason: str) -> None:
    dropped.append({
        "id": item.get("id"),
        "kind": item.get("kind"),
        "reason": reason,
    })


def _filter_covered(items: list[dict], covered: list[str],
                    dropped: list[dict]) -> list[dict]:
    """跨 section 去重：同一文本只出现一次；重复项记 duplicate。"""
    out: list[dict] = []
    for item in items:
        text = item.get("text") or ""
        if _covered(text, covered):
            _add_dropped(dropped, item, "duplicate")
            continue
        out.append(item)
        covered.append(text)
    return out


def _render_item(item: dict, mode: str) -> str:
    kind = item.get("kind")
    text = item.get("text") or ""
    if kind == "preference":
        return f"- 偏好[{item.get('key') or '-'}]：{text}"
    if kind == "fact":
        return f"- 约束[{item.get('key') or '-'}]：{text}"
    if kind == "goal":
        return f"- 目标：{text}"
    if kind == "open_item":
        suffix = f"（{item['expires_at']} 到期）" if item.get("expires_at") else ""
        return f"- 未决[{item.get('id') or '-'}]：{text}{suffix}"
    if kind == "exception":
        return f"- 注意：里程碑「{text}」路线标记完成，但{item.get('reason') or '证据不足'}"
    if kind == "capability":
        label = _evidence_label(item.get("evidence"))
        if mode == "planning":
            scope = "当前路线" if item.get("route_scope") == "current" else "历史路线"
            return f"- [{scope}·{label}] {text}"
        if item.get("tech"):
            return f"- {item['tech']}｜[{label}] {text}"
        return f"- [{label}] {text}"
    return f"- {text}"


def _render_sections(sections: list[dict], mode: str, max_chars: int,
                     dropped: list[dict]) -> tuple[str, list[str], dict[str, int]]:
    lines: list[str] = []
    included: list[str] = []
    section_lengths: dict[str, int] = {}
    for section in sections:
        kind = section.get("kind") or ""
        items = section.get("items") or []
        if not items:
            section_lengths[kind] = 0
            continue
        title = section.get("title") or ""
        rendered: list[str] = []
        if title:
            rendered.append(title)
        for item in items:
            line = _render_item(item, mode)
            candidate = "\n".join(lines + rendered + [line])
            if max_chars and len(candidate) > max_chars:
                _add_dropped(dropped, item, "budget")
                continue
            rendered.append(line)
            item_id = item.get("id")
            if item_id and item_id not in included:
                included.append(item_id)
        if len(rendered) > (1 if title else 0):
            lines.extend(rendered)
            section_lengths[kind] = len("\n".join(rendered))
        else:
            section_lengths[kind] = 0
    return "\n".join(lines), included, section_lengths


def build_briefing(view: dict, *, mode: str, tech: str,
                   profile: dict | None = None,
                   roadmap: dict | None = None,
                   coach_facts: list[str] | None = None,
                   coach_open_items: list[dict] | None = None,
                   now=None, limits: dict | None = None) -> dict:
    """按 mode 生成任务简报。

    Returns:
        {mode, tech, sections, text, included_ids, dropped, counts, truncated}
    """
    if mode not in MODES:
        raise ValueError(f"unknown briefing mode: {mode}")
    view = view or {}
    limits = _limits(mode, limits)
    max_chars = int(limits.get("max_chars") or 0)
    tk = memory_domain.tech_key(tech)
    caps_map = view.get("capabilities") or {}
    current_cap = caps_map.get(tk) or {}
    current_caps = current_cap.get("mastered") or []
    route_index = _route_index(roadmap)
    now_dt = _now_dt(now)
    covered_texts = [*(coach_facts or []),
                     *[(it.get("text") or "") for it in (coach_open_items or [])
                       if isinstance(it, dict)]]
    dropped: list[dict] = []
    sections: list[dict] = []

    evidence_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {"current": 0, "historical": 0}
    for cap in current_caps:
        level = cap.get("evidence") or "unknown"
        evidence_counts[level] = evidence_counts.get(level, 0) + 1
        scope = _route_scope(cap, route_index, roadmap)
        route_counts[scope] = route_counts.get(scope, 0) + 1

    if mode == "survey":
        global_items, global_dropped = _global_items(
            view, ("preference", "fact"), covered_texts,
            int(limits.get("max_global_items") or 0))
        dropped.extend(global_dropped)
        global_items = _filter_covered(global_items, covered_texts, dropped)
        sections.append({"kind": "identity", "title": "全局偏好与约束",
                         "items": global_items})
        current_items, current_dropped = _cap_items(
            current_caps, route_index, roadmap,
            int(limits.get("current_points") or 0), prefer_current=True)
        dropped.extend(current_dropped)
        current_items = _filter_covered(current_items, covered_texts, dropped)
        sections.append({"kind": "current",
                         "title": f"当前技术（{current_cap.get('tech') or tech}）",
                         "items": current_items})
        background: list[dict] = []
        other_techs = [(key, cap) for key, cap in caps_map.items() if key != tk]
        other_techs.sort(key=lambda kv: (-_ts(kv[1].get("last_active")), kv[0]))
        for key, cap in other_techs:
            points, points_dropped = _cap_items(
                cap.get("mastered") or [], {}, None,
                int(limits.get("other_points") or 0),
                tech=cap.get("tech") or key, prefer_current=False)
            dropped.extend(points_dropped)
            background.extend(points)
        background = _filter_covered(background, covered_texts, dropped)
        sections.append({"kind": "background", "title": "其他技术背景",
                         "items": background})
    elif mode == "planning":
        global_items, global_dropped = _global_items(
            view, ("goal", "preference"), covered_texts,
            int(limits.get("max_global_items") or 0))
        dropped.extend(global_dropped)
        global_items = _filter_covered(global_items, covered_texts, dropped)
        sections.append({"kind": "identity", "title": "全局目标与偏好",
                         "items": global_items})
        current_items, current_dropped = _planning_cap_items(
            current_caps, route_index, roadmap,
            int(limits.get("current_points") or 0))
        dropped.extend(current_dropped)
        current_items = _filter_covered(current_items, covered_texts, dropped)
        sections.append({"kind": "current",
                         "title": f"当前技术（{current_cap.get('tech') or tech}）",
                         "items": current_items})
    else:  # coaching
        global_items, global_dropped = _global_items(
            view, ("preference", "fact"), covered_texts,
            int(limits.get("max_global_items") or 0))
        dropped.extend(global_dropped)
        global_items = _filter_covered(global_items, covered_texts, dropped)
        sections.append({"kind": "identity", "title": "全局偏好与约束",
                         "items": global_items})
        # 全局条目已经出现过的文本，技术条目不重复。
        tech_items, tech_dropped = _tech_profile_items(
            view, tk, profile, covered_texts,
            int(limits.get("max_global_items") or 0))
        dropped.extend(tech_dropped)
        tech_items = _filter_covered(tech_items, covered_texts, dropped)
        open_items, open_dropped = _open_items(view, tk, now_dt, covered_texts)
        dropped.extend(open_dropped)
        open_items = _filter_covered(open_items, covered_texts, dropped)
        exceptions = _route_exceptions(roadmap, current_caps)
        exceptions = _filter_covered(exceptions, covered_texts, dropped)
        delta_items = [*tech_items, *open_items, *exceptions]
        sections.append({"kind": "coaching_delta", "title": "技术记忆与异常",
                         "items": delta_items})

    text, included_ids, section_lengths = _render_sections(
        sections, mode, max_chars, dropped)
    return {
        "mode": mode,
        "tech": current_cap.get("tech") or tech,
        "sections": sections,
        "text": text,
        "section_lengths": section_lengths,
        "included_ids": included_ids,
        "dropped": dropped,
        "counts": {
            "evidence": evidence_counts,
            "route": route_counts,
            "points": len(current_caps),
        },
        "truncated": False,
    }


def render_briefing(briefing: dict) -> str:
    """稳定渲染；空简报返回空串。"""
    return (briefing or {}).get("text") or ""
