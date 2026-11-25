"""记忆按需访问（5.0/5.1）：结构化过滤 + 事件时间线，零 I/O、零向量。

5.0 的查询契约：
- lookup：按 tech/kind/status/时间/文本子串过滤当前、归档、撤销/取代历史；
- timeline：按 tech/时间过滤事件序列，回答“上次/后来/撤销过什么”；
- 5.1 不做 BM25/语义排序；`query` 只是确定性文本子串过滤。

状态语义：
active / archived / revoked / superseded / closed / expired / historical。
结果必须带 status/evidence/date/source，避免把历史或未考据当成当前已掌握。
"""

import re
from datetime import datetime

from . import memory as memory_domain

KINDS = ("capability", "preference", "fact", "goal", "open_item")
STATUSES = ("active", "archived", "revoked", "superseded", "closed",
            "expired", "historical")
_STATUS_ORDER = {
    "active": 0,
    "archived": 1,
    "revoked": 2,
    "superseded": 3,
    "closed": 4,
    "expired": 5,
    "historical": 6,
}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _parse_bound(value, *, end: bool = False) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    dt = memory_domain.parse_time(raw)
    if dt and end and len(raw) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return dt


def _ts(value) -> float:
    dt = memory_domain.parse_time(value)
    return dt.timestamp() if dt else 0.0


def _date(value) -> datetime | None:
    return memory_domain.parse_time(value)


def _statuses(values) -> set[str]:
    wanted = {value.strip().lower() for value in _as_list(values) if value}
    if "any" in wanted:
        return set()
    if "history" in wanted:
        wanted.discard("history")
        wanted.update({"revoked", "superseded", "closed", "expired",
                       "historical"})
    return wanted


def _normalize_query(value) -> str:
    return memory_domain.clean_text(value).casefold()


def _compact_tech(value) -> str:
    """技术名宽松匹配：忽略空格/连字符/顿号等，如 Spring AI == SpringAI。"""
    return re.sub(r"[\s\-_/、,，.。]+", "", str(value or "").casefold())


def filter_records(records: list[dict], *, query=None, tech: str | None = None,
                   kinds=None, statuses=None, evidence=None, keys=None,
                   date_from=None, date_to=None, limit: int = 5) -> list[dict]:
    """结构化过滤记忆记录；5.1 的 query 只是文本子串，不做相关性排序。"""
    tech_key = memory_domain.tech_key(tech) if tech else None
    tech_compact = _compact_tech(tech) if tech else None
    wanted_kinds = {value.strip().lower() for value in _as_list(kinds) if value}
    wanted_statuses = _statuses(statuses)
    wanted_evidence = {value.strip().lower() for value in _as_list(evidence) if value}
    wanted_keys = {value.strip() for value in _as_list(keys) if value}
    if "any" in wanted_evidence:
        wanted_evidence = set()
    q = _normalize_query(query)
    start = _parse_bound(date_from)
    end = _parse_bound(date_to, end=True)
    out: list[dict] = []
    for record in records or []:
        # tech 过滤包含该技术 + 全局条目：全局偏好/约束对所有技术生效。
        # 技术名宽松匹配：Spring AI / SpringAI、skill、plugin / skill-plugin 都能对上。
        if (tech_key and record.get("tech_key") != tech_key
                and record.get("scope") != "global"
                and _compact_tech(record.get("tech")) != tech_compact
                and _compact_tech(record.get("tech_key")) != tech_compact):
            continue
        if wanted_kinds and record.get("kind") not in wanted_kinds:
            continue
        if wanted_statuses and record.get("status") not in wanted_statuses:
            continue
        if wanted_evidence and record.get("evidence") not in wanted_evidence:
            continue
        if wanted_keys and record.get("key") not in wanted_keys:
            continue
        if q:
            haystack = " ".join(str(record.get(key) or "") for key in
                                ("text", "key", "tech")).casefold()
            if q not in haystack:
                continue
        dt = _date(record.get("date"))
        if start and (dt is None or dt < start):
            continue
        if end and (dt is None or dt > end):
            continue
        out.append(record)
    out.sort(key=lambda r: (_STATUS_ORDER.get(r.get("status"), 99),
                            -_ts(r.get("date")), str(r.get("id") or "")))
    if limit and limit > 0:
        out = out[:limit]
    return out


def filter_timeline(events: list[dict], *, query=None, tech: str | None = None,
                    date_from=None, date_to=None,
                    limit: int = 20) -> list[dict]:
    """按文件顺序过滤事件；limit 取最近 N 条，返回仍按时间正序。"""
    tech_key = memory_domain.tech_key(tech) if tech else None
    q = _normalize_query(query)
    start = _parse_bound(date_from)
    end = _parse_bound(date_to, end=True)
    out: list[dict] = []
    for event in events or []:
        if tech_key and event.get("tech_key") != tech_key:
            continue
        if q:
            haystack = " ".join(str(event.get(key) or "") for key in
                                ("text", "type", "op", "tech")).casefold()
            if q not in haystack:
                continue
        dt = _date(event.get("date"))
        if start and (dt is None or dt < start):
            continue
        if end and (dt is None or dt > end):
            continue
        out.append(event)
    if limit and limit > 0:
        out = out[-limit:]
    return out


def _record_line(record: dict) -> str:
    tech = record.get("tech") or "全局"
    kind = record.get("kind") or "-"
    key = record.get("key")
    kind_label = f"[{kind}][{key}]" if key and kind in ("preference", "fact") else f"[{kind}]"
    bits = [f"[{record.get('status') or '-'}]{kind_label} "
            f"{tech}：{record.get('text') or ''}"]
    meta = []
    if record.get("evidence"):
        meta.append(f"证据={record['evidence']}")
    if record.get("date"):
        meta.append(str(record["date"]))
    if record.get("id"):
        meta.append(f"id={record['id']}")
    if meta:
        bits.append("（" + "；".join(meta) + "）")
    return "- " + "".join(bits)


def render_records(records: list[dict], *, max_chars: int = 800) -> tuple[str, list[dict]]:
    """按条目渲染；超预算整条不展示，不切文本。"""
    if not records:
        return "没有找到匹配的长期记忆。", []
    lines: list[str] = []
    used: list[dict] = []
    for record in records:
        line = _record_line(record)
        candidate = "\n".join(lines + [line])
        if max_chars and len(candidate) > max_chars:
            break
        lines.append(line)
        used.append(record)
    if not used:
        return (f"找到 {len(records)} 条，但单条超过结果预算；"
                "请缩小查询范围或提高 LTM_RECALL_MAX_CHARS。"), []
    return "\n".join(lines), used


def _timeline_line(event: dict) -> str:
    tech = event.get("tech") or "全局"
    bits = [f"{event.get('date') or '-'} [{event.get('type') or '-'}] {tech}："
            f"{event.get('text') or ''}"]
    if event.get("op"):
        bits.append(f"（op={event['op']}）")
    if event.get("id"):
        bits.append(f"（id={event['id']}）")
    return "- " + "".join(bits)


def render_timeline(events: list[dict], *, max_chars: int = 1200) -> tuple[str, list[dict]]:
    if not events:
        return "没有找到匹配的记忆事件。", []
    lines: list[str] = []
    used: list[dict] = []
    for event in events:
        line = _timeline_line(event)
        candidate = "\n".join(lines + [line])
        if max_chars and len(candidate) > max_chars:
            break
        lines.append(line)
        used.append(event)
    if not used:
        return (f"找到 {len(events)} 条事件，但单条超过结果预算；"
                "请缩小时间范围或提高 LTM_RECALL_MAX_CHARS。"), []
    return "\n".join(lines), used
