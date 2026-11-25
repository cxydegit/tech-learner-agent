"""长期记忆存储：事件日志 / 物化视图 / 归档（文件 I/O 层）。

三个文件：
- ``learner/memory_events.jsonl``：append-only 事件日志，唯一真源；
- ``learner/memory.json``：确定性折叠出的物化视图，长期记忆的唯一读取来源；
- ``learner/memory_archive.jsonl``：超上限条目的冷存储（热路径不读）。

写入一律「先 append 事件 → 再整体折叠 → 临时文件 + os.replace 原子替换视图」。
折叠失败不推进视图（``last_event_id`` 对不上时下次读取会重建），事件不丢。
单写者假设：模块级 ``threading.Lock`` 串行化写入与重建。
"""

import json
import logging
import os
import threading
from pathlib import Path

from ..config import config
from ..domain import memory as memory_domain
from . import learner
from .audit import audit, current_context

_LOCK = threading.Lock()


# ============================================================
# 路径
# ============================================================


def events_path() -> Path:
    return config.LEARNER_DIR / "memory_events.jsonl"


def view_path() -> Path:
    return config.LEARNER_DIR / "memory.json"


def archive_path() -> Path:
    return config.LEARNER_DIR / "memory_archive.jsonl"


# ============================================================
# 基础读写
# ============================================================


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """读 JSONL；损坏行跳过并计数（个人工具，单行坏了不该拖垮整份记忆）。"""
    rows: list[dict] = []
    bad = 0
    if not path.exists():
        return rows, bad
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows, bad
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001 —— 单行损坏按坏行计数
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 —— 损坏视图按缺失处理，从事件日志重建
        return None


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """整份 JSONL 原子写入（仅用于首次迁移：事件文件此时为空/不存在）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
    os.replace(tmp, path)


# ============================================================
# 路线上下文
# ============================================================


def _all_roadmaps() -> dict[str, dict]:
    """读所有路线 JSON：``{tech_key: roadmap}``。损坏/非路线文件跳过。"""
    out: dict[str, dict] = {}
    if not config.ROADMAP_DIR.exists():
        return out
    for path in sorted(config.ROADMAP_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 损坏路线不参与对账
            continue
        if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
            continue
        tech = str(data.get("tech") or "").strip()
        key = memory_domain.tech_key(tech)
        if key:
            out[key] = data
    return out


def _roadmap_time(roadmap: dict | None, *, prefer: str = "updated"):
    """路线里可用的最近时间（历史数据没有逐里程碑时间，只能取整份路线的时间）。"""
    if not roadmap:
        return None
    fields = (("updated_at", "created_at") if prefer == "updated"
              else ("created_at", "updated_at"))
    for field in fields:
        dt = memory_domain.parse_time(roadmap.get(field))
        if dt:
            return dt
    return None


def _caps() -> dict:
    return {
        "max_preferences": config.LTM_MAX_PREFERENCES,
        "max_facts": config.LTM_MAX_FACTS,
        "max_mastered_per_tech": config.LTM_MAX_MASTERED_PER_TECH,
        "max_open_items": config.LTM_MAX_OPEN_ITEMS,
        "max_goals": config.LTM_MAX_GOALS,
    }


# ============================================================
# 首次迁移（profile + roadmap → 事件）
# ============================================================


def _migration_events(profile: dict, roadmaps: dict[str, dict], now) -> list[dict]:
    """从现有 profile.json / roadmaps/*.json 生成初始事件（source=migration）。"""
    events: list[dict] = []

    for _, entry in sorted((profile or {}).items()):
        if not isinstance(entry, dict):
            continue
        tech = str(entry.get("tech") or "").strip()
        if not tech:
            continue
        roadmap = roadmaps.get(memory_domain.tech_key(tech))
        at = _roadmap_time(roadmap, prefer="created") or now
        session = "migration"

        def add(category: str, key: str, text, *, tech=tech, at=at) -> None:
            value = str(text).strip() if text is not None else ""
            if not value:
                return
            events.append(memory_domain.make_event(
                "profile_recorded", at=at, session=session, source="migration",
                category=category, scope="tech", tech=tech, key=key, text=value))

        add("goal", "goal", entry.get("goal"))
        if entry.get("self_level") is not None:
            add("fact", "self_level", entry.get("self_level"))
        add("fact", "related", entry.get("related"))
        if entry.get("time_budget") is not None:
            add("fact", "time_budget", entry.get("time_budget"))
        # bucket 是 self_level 的派生值、roadmap_path 是文件位置，都不进记忆。

    for _, roadmap in sorted(roadmaps.items()):
        tech = str(roadmap.get("tech") or "").strip()
        if not tech:
            continue
        at = _roadmap_time(roadmap, prefer="updated") or now
        for stage in roadmap.get("stages") or []:
            for milestone in stage.get("milestones") or []:
                if not milestone.get("done"):
                    continue
                point = memory_domain.clean_text(milestone.get("desc"))
                if not point:
                    continue
                events.append(memory_domain.make_event(
                    "capability_recorded", at=at, session="migration",
                    source="migration", tech=tech, point=point,
                    milestone_id=milestone.get("id"), op="add",
                    evidence="unknown"))
    return events


def _bootstrap_if_needed(now) -> list[dict]:
    """首次启用：事件日志为空且还没有有效视图时才迁移；返回需要写入的事件。"""
    if events_path().exists() and events_path().stat().st_size > 0:
        return []
    if view_path().exists():
        view = _read_json(view_path())
        if view and _view_entry_count(view) > 0:
            return []
    profile = learner.load_profile()
    roadmaps = _all_roadmaps()
    return _migration_events(profile, roadmaps, now)


# ============================================================
# 一致性修复（reconcile）
# ============================================================


def _latest_ops(events: list[dict]) -> dict[tuple[str, str], str]:
    """每个能力点最近一条事件的 op（add / revoke）；墓碑规则靠它判断。"""
    latest: dict[tuple[str, str], str] = {}
    for event in events:
        if event.get("type") != "capability_recorded":
            continue
        tech = str(event.get("tech") or "").strip()
        point = memory_domain.clean_text(event.get("point"))
        op = str(event.get("op") or "")
        if not tech or not point or op not in ("add", "revoke"):
            continue
        latest[(memory_domain.tech_key(tech),
                memory_domain.normalize_point(point))] = op
    return latest


def _view_entry_count(view: dict) -> int:
    caps = sum(len(cap.get("mastered") or [])
               for cap in (view.get("capabilities") or {}).values())
    goals = view.get("goals") or {}
    return (caps + len(view.get("preferences") or []) + len(view.get("facts") or [])
            + len(goals.get("global") or [])
            + sum(len(rows) for rows in (goals.get("by_tech") or {}).values())
            + len(view.get("open_items") or []))


def _recovery_events(view: dict, now) -> list[dict]:
    """事件日志丢失时的应急恢复：从视图的 active 条目重建最小事件集。

    只能恢复「当前生效」的状态——superseded / forgotten 历史、撤销墓碑、原来源
    事件 id 都已随日志丢失，无法凭空重建。来源标 migration、session=recovery，
    便于事后识别这批事件不是原始记录。
    """
    events: list[dict] = []
    for key, cap in (view.get("capabilities") or {}).items():
        tech = cap.get("tech") or key
        for item in cap.get("mastered") or []:
            events.append(memory_domain.make_event(
                "capability_recorded", at=item.get("at") or view.get("updated_at") or now,
                session="recovery", source="migration", tech=tech,
                point=item.get("point"), milestone_id=item.get("milestone_id"),
                op="add", evidence=item.get("evidence") or "unknown"))

    def add_profile(entry: dict, category: str) -> None:
        events.append(memory_domain.make_event(
            "profile_recorded",
            at=entry.get("last_confirmed") or view.get("updated_at") or now,
            session="recovery", source=entry.get("source") or "migration",
            category=category, scope=entry.get("scope") or "global",
            tech=entry.get("tech") or "", key=entry.get("key") or "goal",
            text=entry.get("text") or ""))

    for entry in view.get("preferences") or []:
        add_profile(entry, "preference")
    for entry in view.get("facts") or []:
        add_profile(entry, "fact")
    goals = view.get("goals") or {}
    for entry in goals.get("global") or []:
        add_profile(entry, "goal")
    for rows in (goals.get("by_tech") or {}).values():
        for entry in rows:
            add_profile(entry, "goal")
    for item in view.get("open_items") or []:
        events.append(memory_domain.make_event(
            "open_item_recorded",
            at=item.get("created_at") or item.get("last_confirmed")
            or view.get("updated_at") or now,
            session="recovery", source="user", op="open",
            tech=item.get("tech") or "", item_id=item.get("id"),
            text=item.get("text") or "", expires_at=item.get("expires_at")))
    return events


def _recover_events_locked(now) -> list[dict]:
    """日志为空但视图非空 → 从视图重建事件并原子写回；返回恢复出的事件。

    这是损毁恢复，不是正常路径：完整历史仍以原事件日志为准，恢复后的视图
    会丢掉 supersede 链与墓碑，必须留 WARNING 审计让用户知道发生过什么。
    """
    view = _read_json(view_path())
    # 只有 roadmap 投影、没有任何实际记忆条目时不算“日志丢失”，不需要恢复。
    if not view or _view_entry_count(view) == 0:
        return []
    recovered = _recovery_events(view, now)
    if not recovered:
        return []
    _write_jsonl_atomic(events_path(), recovered)
    audit("ltm_events_recovered", level=logging.WARNING,
          events=len(recovered), view_entries=_view_entry_count(view),
          last_event_id=view.get("last_event_id"))
    return recovered


def _reconcile_events(events: list[dict], roadmaps: dict[str, dict],
                      now) -> list[dict]:
    """路线现状与事件日志对账：补漏、撤销、墓碑优先。

    - ``done=true`` 且该 point 没有任何事件 → 补 ``add``（历史迁移口径 unknown）；
    - ``done=true`` 但最近一条是 ``revoke`` → 不补（用户否认不能被路线覆盖）；
    - ``done=false`` 但最近一条是 ``add`` → 补 ``revoke``。
    """
    latest = _latest_ops(events)
    new_events: list[dict] = []
    for _, roadmap in sorted(roadmaps.items()):
        tech = str(roadmap.get("tech") or "").strip()
        if not tech:
            continue
        at = _roadmap_time(roadmap, prefer="updated") or now
        for stage in roadmap.get("stages") or []:
            for milestone in stage.get("milestones") or []:
                point = memory_domain.clean_text(milestone.get("desc"))
                if not point:
                    continue
                key = (memory_domain.tech_key(tech),
                       memory_domain.normalize_point(point))
                op = latest.get(key)
                done = bool(milestone.get("done"))
                if done and op is None:
                    new_events.append(memory_domain.make_event(
                        "capability_recorded", at=at, session="reconcile",
                        source="migration", tech=tech, point=point,
                        milestone_id=milestone.get("id"), op="add",
                        evidence="unknown"))
                    latest[key] = "add"
                elif not done and op == "add":
                    new_events.append(memory_domain.make_event(
                        "capability_recorded", at=at, session="reconcile",
                        source="migration", tech=tech, point=point,
                        milestone_id=milestone.get("id"), op="revoke"))
                    latest[key] = "revoke"
    return new_events


# ============================================================
# 折叠与重建
# ============================================================


def _append_archive(records: list[dict]) -> None:
    """超上限条目追加到冷存储；按 (kind, id) 幂等去重。"""
    if not records:
        return
    path = archive_path()
    existing: set[tuple] = set()
    rows, _ = _read_jsonl(path)
    for row in rows:
        existing.add((row.get("kind"), row.get("id")))
    fresh = []
    for record in records:
        key = (record.get("kind"), record.get("id"))
        if key in existing:
            continue
        existing.add(key)
        fresh.append(record)
    if fresh:
        _append_jsonl(path, fresh)
        audit("ltm_archive", count=len(fresh))


def _rebuild_locked(events: list[dict], roadmaps: dict[str, dict], now) -> dict:
    """折叠事件 → 写视图（调用方已持锁）。"""
    view, archived = memory_domain.fold_events(
        events, roadmaps=roadmaps, now=now, caps=_caps())
    _atomic_write_json(view_path(), view)
    _append_archive(archived)
    audit("ltm_view_write", events=len(events),
          last_event_id=view.get("last_event_id"),
          capabilities=len(view.get("capabilities") or {}),
          preferences=len(view.get("preferences") or []),
          facts=len(view.get("facts") or []),
          archived=len(archived))
    return view


def _ensure_locked(now) -> dict:
    """调用方已持锁：迁移 → 对账 → 按需重建 → 返回视图。"""
    migrated = _bootstrap_if_needed(now)
    if migrated:
        # 首次迁移整份原子写入：避免中途崩溃留下半份事件日志后被误判为“已迁移”。
        _write_jsonl_atomic(events_path(), migrated)
        audit("ltm_migrate", events=len(migrated))
    events, bad = _read_jsonl(events_path())
    if bad:
        audit("ltm_corrupt", file="events", lines=bad)
    if not events:
        # 事件日志被删/清空：不能按空日志折叠后覆盖视图（那是静默丢记忆）。
        # 先从视图恢复当前生效状态；连恢复都做不出来时，退回只读视图。
        events = _recover_events_locked(now)
        if not events:
            existing = _read_json(view_path())
            if existing and _view_entry_count(existing) > 0:
                audit("ltm_events_missing", level=logging.WARNING,
                      view_entries=_view_entry_count(existing))
                return existing
    roadmaps = _all_roadmaps()
    reconcile = _reconcile_events(events, roadmaps, now)
    if reconcile:
        _append_jsonl(events_path(), reconcile)
        audit("ltm_reconcile", events=len(reconcile))
        events.extend(reconcile)
    view = _read_json(view_path())
    last_id = events[-1].get("id") if events else None
    if view is None or view.get("last_event_id") != last_id:
        view = _rebuild_locked(events, roadmaps, now)
    return view


def ensure_view(*, now=None) -> dict:
    """返回最新视图：首次启用自动迁移，事件落后时从日志重建。"""
    if not config.LTM_ENABLED:
        return memory_domain.empty_view()
    now_dt = memory_domain.parse_time(now) or memory_domain.local_now()
    with _LOCK:
        return _ensure_locked(now_dt)


def record_events(events: list[dict], *, now=None) -> dict:
    """写路径：先 append 事件，再同步折叠进视图（毫秒级）。

    事件已落盘后即使折叠失败也不丢：视图 ``last_event_id`` 对不上时，
    下次 ``ensure_view`` 会从日志重放补齐。
    """
    if not config.LTM_ENABLED:
        return memory_domain.empty_view()
    now_dt = memory_domain.parse_time(now) or memory_domain.local_now()
    rows = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        row = dict(event)
        row.setdefault("id", memory_domain.new_event_id(now_dt))
        row.setdefault("at", memory_domain.format_time(now_dt))
        rows.append(row)
    with _LOCK:
        existing, _ = _read_jsonl(events_path())
        if not existing:
            _recover_events_locked(now_dt)  # 日志丢失时先恢复，避免新事件覆盖旧视图
        _append_jsonl(events_path(), rows)
        if rows:
            audit("ltm_event_append", count=len(rows),
                  types=sorted({str(r.get("type") or "") for r in rows}))
        return _ensure_locked(now_dt)


def load_archive() -> tuple[list[dict], int]:
    """读归档冷存储；返回 (rows, 坏行数)。损坏行不拖垮整个文件。"""
    return _read_jsonl(archive_path())


def load_events() -> tuple[list[dict], int]:
    """读事件日志；返回 (rows, 坏行数)。按需查询/时间线用，不参与热路径折叠。"""
    return _read_jsonl(events_path())


def forgotten_ids() -> set[str]:
    """所有被 ``memory_forgotten`` 命中的条目 id（归档展示标注逻辑删除用）。"""
    rows, _ = _read_jsonl(events_path())
    return {str(row.get("target_id")) for row in rows
            if row.get("type") == "memory_forgotten" and row.get("target_id")}


def event_texts(event_ids) -> dict[str, str]:
    """按事件 id 取原始文本（CLI 展示 supersede 旧值用，不参与折叠）。"""
    wanted = {str(event_id) for event_id in (event_ids or []) if event_id}
    if not wanted:
        return {}
    rows, _ = _read_jsonl(events_path())
    out: dict[str, str] = {}
    for row in rows:
        event_id = str(row.get("id") or "")
        if event_id in wanted:
            out[event_id] = str(row.get("text") or row.get("point") or "")
    return out


def _find_view_entry(view: dict, target_id: str) -> dict | None:
    """按确定性 id 在视图里找一条 active 记忆（能力点不在 forget 范围）。"""
    for kind, bucket in (("preference", "preferences"), ("fact", "facts")):
        for entry in view.get(bucket) or []:
            if entry.get("id") == target_id:
                return {"kind": kind, "entry": entry}
    goals = view.get("goals") or {}
    for entry in goals.get("global") or []:
        if entry.get("id") == target_id:
            return {"kind": "goal", "entry": entry}
    for rows in (goals.get("by_tech") or {}).values():
        for entry in rows:
            if entry.get("id") == target_id:
                return {"kind": "goal", "entry": entry}
    for entry in view.get("open_items") or []:
        if entry.get("id") == target_id:
            return {"kind": "open_item", "entry": entry}
    return None


def forget(target_id: str, *, now=None) -> dict:
    """按 id 逻辑删除一条记忆（偏好 / 事实 / 目标 / 未决事项）。

    追加一条 ``memory_forgotten`` 事件，视图不再生效；原始事件仍在日志里
    （逻辑删除，不是物理脱敏）。能力点不允许走这里——能力否认由
    ``update_roadmap(done=false)`` 产生 ``capability_recorded(op=revoke)``。
    """
    target = str(target_id or "").strip()
    if not target:
        return {"status": "error", "error": "需要记忆 id（用 memory show 查看）"}
    if not config.LTM_ENABLED:
        return {"status": "error", "error": "长期记忆已关闭（LTM_ENABLED=false）"}
    if target.startswith("cap:"):
        return {"status": "error",
                "error": "v1 不支持删除能力点；请在对话中用 update_roadmap(done=false) 取消里程碑"}
    try:
        view = ensure_view(now=now)
    except Exception as exc:  # noqa: BLE001
        audit("ltm_error", where="forget_read",
              error=f"{type(exc).__name__}: {exc}"[:200])
        return {"status": "error", "error": "长期记忆读取失败"}
    found = _find_view_entry(view, target)
    from_archive = False
    if found is None:
        # 归档条目也可以删：追加 forget 后，重放时它在投影阶段就被移除，
        # 不会再回到热视图；归档文件本身是冷存储，不物理改写。
        rows, _ = load_archive()
        archived = next((row for row in rows if row.get("id") == target), None)
        if archived is None:
            return {"status": "error", "error": f"未找到记忆条目：{target}"}
        found = {"kind": archived.get("kind"), "entry": archived}
        from_archive = True
    now_dt = memory_domain.parse_time(now) or memory_domain.local_now()
    event = memory_domain.make_event(
        "memory_forgotten", at=now_dt, session="cli", source="user",
        target_id=target)
    try:
        record_events([event], now=now_dt)
    except Exception as exc:  # noqa: BLE001
        audit("ltm_error", where="forget_write",
              error=f"{type(exc).__name__}: {exc}"[:200])
        return {"status": "error", "error": "长期记忆写入失败"}
    audit("ltm_forget", target=target, kind=found["kind"], from_archive=from_archive)
    return {"status": "ok", "target_id": target, "kind": found["kind"],
            "entry": found["entry"], "from_archive": from_archive}


def rebuild(*, now=None) -> dict:
    """显式重建视图（CLI ``memory rebuild`` / 测试用）。"""
    if not config.LTM_ENABLED:
        return memory_domain.empty_view()
    now_dt = memory_domain.parse_time(now) or memory_domain.local_now()
    with _LOCK:
        events, bad = _read_jsonl(events_path())
        if bad:
            audit("ltm_corrupt", file="events", lines=bad)
        roadmaps = _all_roadmaps()
        reconcile = _reconcile_events(events, roadmaps, now_dt)
        if reconcile:
            _append_jsonl(events_path(), reconcile)
            events.extend(reconcile)
        view = _rebuild_locked(events, roadmaps, now_dt)
        audit("ltm_rebuild", events=len(events))
        return view


# ============================================================
# 读取层入口
# ============================================================


def dump_prompt_injection(*, mode: str, tech: str, session: str | None,
                          prompt: str, ltm_text: str,
                          blocks: dict | None = None,
                          section_lengths: dict | None = None,
                          ids: list[str] | None = None,
                          turn: int | None = None,
                          dropped: list[dict] | None = None,
                          truncated: bool | None = None) -> None:
    """调试落盘：在**最终提示词拼装后**记录本次实际追加的长期记忆文本。

    默认关闭（LTM_DEBUG_DUMP）。与审计日志分开：审计只记 id/长度，这里才有
    用户记忆原文；文件在 logs/ 下，已 gitignore。调试失败绝不影响对话。

    为什么不在渲染层记录：渲染只说明「产出了什么」，不说明「最终提示词里
    是否真的追加了」；提示词边界记录才能看见跨段重复、实际长度和归属回合。
    """
    if not config.LTM_DEBUG_DUMP:
        return
    try:
        ctx = current_context()
        text = ltm_text or ""
        prompt = prompt or ""
        blocks = blocks or {}
        row = {
            "schema": 3,
            "ts": memory_domain.format_time(memory_domain.local_now()),
            "mode": mode,
            "tech": tech,
            "session": session or ctx.get("thread_id"),
            "thread_id": ctx.get("thread_id"),
            "step": ctx.get("step"),
            "turn": turn if turn is not None else ctx.get("turn"),
            "origin": ctx.get("origin"),
            "run_id": ctx.get("run_id"),
            "has_ltm": bool(text),
            "ltm_chars": len(text),
            "ltm_text": text,
            "ltm_appended": bool(text) and text in prompt,
            "prompt_chars": len(prompt),
            "blocks": blocks,
            "block_lengths": {key: len(value or "") for key, value in blocks.items()},
            "section_lengths": section_lengths or {},
            "ids": list(ids or []),
            "dropped": dropped or [],
            "truncated": bool(truncated),
        }
        config.AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (config.AUDIT_LOG_DIR / "ltm_inject.jsonl").open(
                "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001, S110 —— 调试输出失败不影响对话
        pass
