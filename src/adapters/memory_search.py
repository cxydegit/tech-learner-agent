"""记忆按需访问的 I/O 层：视图 + 归档 + 事件 → 可检索记录/时间线。

不加载 chromadb / embedding；5.1 只做结构化与文本子串过滤。
I1 保持：本模块只依赖 stdlib 与 memory 适配器。
"""

from ..domain import memory as memory_domain
from .memory import (
    ensure_view,
    forgotten_ids,
    load_archive,
    load_events,
)


def _record(**kwargs) -> dict:
    return kwargs


def _tech_name(view: dict, tech_key: str) -> str:
    cap = (view.get("capabilities") or {}).get(tech_key) or {}
    return str(cap.get("tech") or tech_key)


def _view_records(view: dict, now_dt, forgotten: set[str]) -> list[dict]:
    out: list[dict] = []
    for tech_key, cap in (view.get("capabilities") or {}).items():
        tech = str(cap.get("tech") or tech_key)
        for point in cap.get("mastered") or []:
            item_id = str(point.get("id") or "")
            if item_id in forgotten:
                continue
            out.append(_record(
                id=item_id, canonical_id=item_id, kind="capability",
                text=str(point.get("point") or ""), tech=tech,
                tech_key=tech_key, scope="tech", key=None,
                evidence=point.get("evidence") or "unknown",
                status="active", date=point.get("at"),
                source="view", source_event_id=point.get("source_event_id"),
                milestone_id=point.get("milestone_id"),
            ))
    for kind, bucket in (("preference", "preferences"), ("fact", "facts")):
        for entry in view.get(bucket) or []:
            item_id = str(entry.get("id") or "")
            if item_id in forgotten:
                continue
            tech_key = str(entry.get("tech_key") or "")
            out.append(_record(
                id=item_id, canonical_id=item_id, kind=kind,
                text=str(entry.get("text") or ""),
                tech=str(entry.get("tech") or _tech_name(view, tech_key)),
                tech_key=tech_key,
                scope=entry.get("scope") or ("tech" if tech_key else "global"),
                key=entry.get("key"), evidence=None,
                status="active",
                date=entry.get("last_confirmed") or entry.get("created_at"),
                source="view", source_event_id=entry.get("source_event_id"),
            ))
    goals = view.get("goals") or {}
    for entry in goals.get("global") or []:
        item_id = str(entry.get("id") or "")
        if item_id in forgotten:
            continue
        out.append(_record(
            id=item_id, canonical_id=item_id, kind="goal",
            text=str(entry.get("text") or ""), tech="", tech_key="",
            scope="global", key="goal", evidence=None, status="active",
            date=entry.get("last_confirmed") or entry.get("created_at"),
            source="view", source_event_id=entry.get("source_event_id"),
        ))
    for tech_key, rows in (goals.get("by_tech") or {}).items():
        for entry in rows or []:
            item_id = str(entry.get("id") or "")
            if item_id in forgotten:
                continue
            out.append(_record(
                id=item_id, canonical_id=item_id, kind="goal",
                text=str(entry.get("text") or ""),
                tech=str(entry.get("tech") or _tech_name(view, tech_key)),
                tech_key=tech_key, scope="tech", key="goal", evidence=None,
                status="active",
                date=entry.get("last_confirmed") or entry.get("created_at"),
                source="view", source_event_id=entry.get("source_event_id"),
            ))
    for entry in view.get("open_items") or []:
        item_id = str(entry.get("id") or "")
        if item_id in forgotten:
            continue
        expires = memory_domain.parse_time(entry.get("expires_at"))
        status = "expired" if expires is not None and expires < now_dt else "active"
        tech_key = str(entry.get("tech_key") or "")
        out.append(_record(
            id=item_id, canonical_id=item_id, kind="open_item",
            text=str(entry.get("text") or ""),
            tech=str(entry.get("tech") or _tech_name(view, tech_key)),
            tech_key=tech_key, scope="tech", key=None, evidence=None,
            status=status, date=entry.get("expires_at") or entry.get("created_at"),
            source="view", source_event_id=entry.get("source_event_id"),
        ))
    return out


def _archive_records(rows: list[dict], forgotten: set[str]) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        item_id = str(row.get("id") or "")
        if item_id in forgotten:
            continue
        kind = str(row.get("kind") or "capability")
        tech = str(row.get("tech") or "")
        tech_key = str(row.get("tech_key") or memory_domain.tech_key(tech))
        out.append(_record(
            id=item_id, canonical_id=item_id, kind=kind,
            text=str(row.get("text") or row.get("point") or ""),
            tech=tech, tech_key=tech_key,
            scope=row.get("scope") or ("tech" if tech else "global"),
            key=row.get("key"),
            evidence=row.get("evidence") or ("unknown" if kind == "capability" else None),
            status="archived",
            date=row.get("archived_at") or row.get("at") or row.get("last_confirmed"),
            source="archive", source_event_id=row.get("source_event_id"),
            milestone_id=row.get("milestone_id"),
        ))
    return out


def _history_records(events: list[dict], active: list[dict],
                     archived: list[dict], forgotten: set[str]) -> list[dict]:
    active_by_id = {str(row.get("id")): row for row in active}
    archived_by_id = {str(row.get("id")): row for row in archived}
    active_source = {str(row.get("id")): row.get("source_event_id") for row in active}
    archived_source = {str(row.get("id")): row.get("source_event_id") for row in archived}
    out: list[dict] = []

    # 1) 已撤销能力：只保留每个 canonical id 的最后一条事件；active/archive 覆盖的不重复。
    latest_cap: dict[str, dict] = {}
    for event in events:
        if event.get("type") != "capability_recorded":
            continue
        tech = str(event.get("tech") or "")
        point = str(event.get("point") or "")
        cid = memory_domain.capability_id(tech, point)
        if not point or cid in active_by_id or cid in archived_by_id or cid in forgotten:
            continue
        latest_cap[cid] = event
    for cid, event in latest_cap.items():
        if event.get("op") != "revoke":
            continue
        tech = str(event.get("tech") or "")
        out.append(_record(
            id=cid, canonical_id=cid, kind="capability",
            text=str(event.get("point") or ""), tech=tech,
            tech_key=memory_domain.tech_key(tech), scope="tech", key=None,
            evidence=event.get("evidence") or "unknown", status="revoked",
            date=event.get("at"), source="event",
            source_event_id=event.get("id"),
            milestone_id=event.get("milestone_id"),
        ))

    # 2) 被取代的偏好/事实/目标：active/archive 的 source_event_id 是当前值，其余为历史。
    latest_profile: dict[str, dict] = {}
    for event in events:
        if event.get("type") != "profile_recorded":
            continue
        category = str(event.get("category") or "")
        scope = str(event.get("scope") or "tech")
        tech = str(event.get("tech") or "")
        key = str(event.get("key") or "")
        cid = memory_domain.profile_id(category, scope, tech, key)
        if cid in forgotten:
            continue
        if cid in active_by_id:
            if str(event.get("id")) == str(active_source.get(cid)):
                continue
            status = "superseded"
        elif cid in archived_by_id:
            if str(event.get("id")) == str(archived_source.get(cid)):
                continue
            status = "superseded"
        else:
            # 没有当前值：留最后一条作为 historical，避免把整条链都塞进结果。
            latest_profile[cid] = event
            continue
        out.append(_record(
            id=str(event.get("id")), canonical_id=cid, kind=category,
            text=str(event.get("text") or ""), tech=tech,
            tech_key=memory_domain.tech_key(tech), scope=scope, key=key,
            evidence=None, status=status, date=event.get("at"),
            source="event", source_event_id=event.get("id"),
        ))
    for cid, event in latest_profile.items():
        category = str(event.get("category") or "")
        scope = str(event.get("scope") or "tech")
        tech = str(event.get("tech") or "")
        out.append(_record(
            id=str(event.get("id")), canonical_id=cid, kind=category,
            text=str(event.get("text") or ""), tech=tech,
            tech_key=memory_domain.tech_key(tech), scope=scope,
            key=event.get("key"), evidence=None, status="historical",
            date=event.get("at"), source="event",
            source_event_id=event.get("id"),
        ))

    # 3) 已关闭未决项：active/archive 覆盖的不重复。
    open_text: dict[str, str] = {}
    latest_open: dict[str, dict] = {}
    for event in events:
        if event.get("type") != "open_item_recorded":
            continue
        tech = str(event.get("tech") or "")
        item_id = str(event.get("item_id") or "")
        if not item_id:
            continue
        if event.get("op") == "open":
            open_text[item_id] = str(event.get("text") or "")
        if item_id in active_by_id or item_id in archived_by_id or item_id in forgotten:
            continue
        latest_open[item_id] = event
    for item_id, event in latest_open.items():
        if event.get("op") != "close":
            continue
        tech = str(event.get("tech") or "")
        out.append(_record(
            id=item_id, canonical_id=item_id, kind="open_item",
            text=str(event.get("text") or open_text.get(item_id) or ""),
            tech=tech, tech_key=memory_domain.tech_key(tech),
            scope="tech", key=None, evidence=None, status="closed",
            date=event.get("at"), source="event",
            source_event_id=event.get("id"),
        ))
    return out


def load_records(*, now=None) -> list[dict]:
    """读取 active + archive + 历史记录；按 (id,status) 去重，forget 已排除。"""
    view = ensure_view(now=now)
    now_dt = memory_domain.parse_time(now) or memory_domain.local_now()
    archive_rows, _ = load_archive()
    events, _ = load_events()
    forgotten = forgotten_ids()
    active = _view_records(view, now_dt, forgotten)
    archived = _archive_records(archive_rows, forgotten)
    history = _history_records(events, active, archived, forgotten)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for record in [*active, *archived, *history]:
        key = (str(record.get("id")), str(record.get("status")))
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def _event_target(event: dict) -> str | None:
    kind = event.get("type")
    if kind == "capability_recorded":
        return memory_domain.capability_id(
            str(event.get("tech") or ""), str(event.get("point") or ""))
    if kind == "profile_recorded":
        return memory_domain.profile_id(
            str(event.get("category") or ""), str(event.get("scope") or "tech"),
            str(event.get("tech") or ""), str(event.get("key") or ""))
    if kind == "open_item_recorded":
        return str(event.get("item_id") or "") or None
    return None


def load_timeline(*, now=None) -> list[dict]:
    """事件时间线（文件顺序）；forget 命中的正文事件不返回。"""
    ensure_view(now=now)
    events, _ = load_events()
    forgotten = forgotten_ids()
    open_text: dict[str, str] = {}
    for event in events:
        if event.get("type") == "open_item_recorded" and event.get("op") == "open":
            item_id = str(event.get("item_id") or "")
            if item_id:
                open_text[item_id] = str(event.get("text") or "")
    out: list[dict] = []
    for event in events:
        kind = event.get("type")
        target = _event_target(event)
        if target and target in forgotten:
            continue
        tech = str(event.get("tech") or "")
        tech_key = memory_domain.tech_key(tech)
        if kind == "capability_recorded":
            text = str(event.get("point") or "")
            event_type = "capability"
            op = event.get("op")
        elif kind == "profile_recorded":
            text = str(event.get("text") or "")
            event_type = str(event.get("category") or "profile")
            op = None
        elif kind == "open_item_recorded":
            text = str(event.get("text") or open_text.get(str(event.get("item_id") or "")) or "")
            event_type = "open_item"
            op = event.get("op")
        elif kind == "diagnostic_recorded":
            text = f"诊断 {event.get('asked') or 0} 题中 {event.get('right') or 0} 对"
            event_type = "diagnostic"
            op = None
        elif kind == "memory_forgotten":
            text = f"忘记 {event.get('target_id') or ''}"
            event_type = "forgotten"
            op = None
        else:
            continue
        out.append(_record(
            id=event.get("id"), type=event_type,
            text=text, op=op, tech=tech, tech_key=tech_key,
            evidence=event.get("evidence"), date=event.get("at"),
            source="event", key=event.get("key"),
            target_id=event.get("target_id"),
        ))
    return out
