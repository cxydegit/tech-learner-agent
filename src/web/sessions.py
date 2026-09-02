"""会话列表 / 详情 / 删除：基于 SqliteSaver.list() + 每线程最新 checkpoint。

会话列表项 `{thread_id, title, tech, created_at, updated_at, preview, qa数, note数}`：
- `saver.list(None)` 可遍历全部线程，按 checkpoint_id 降序 → 每线程第一条即最新 checkpoint；
- `created_at` / `updated_at` 首选 conversation 的 ts（新会话有），老会话回退 checkpoint_id / thread_id 时间；
- title 默认 `tech + 时间`。

本模块顶层不 import langgraph（SqliteSaver 函数内 lazy）。
"""

import re
from datetime import datetime

from ..config import config

# 线程 ID 形如 learn-YYYYMMDD-HHMMSS；老会话无 conversation 字段时用它兜底创建时间
_THREAD_ID_TS_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")


def _open_saver():
    """打开 SqliteSaver 连接（lazy import 保持顶层无重依赖；with 语句负责关闭）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    return SqliteSaver.from_conn_string(str(config.GRAPH_DB_PATH))


def _get_state_values(tup) -> dict:
    """从 CheckpointTuple 提取状态 channel_values（dict 或序列化 dict 都兼容）。"""
    ckpt = tup.checkpoint
    if isinstance(ckpt, dict):
        return ckpt.get("channel_values") or {}
    return {}


def _pending_interrupt(tup) -> dict | None:
    """从 CheckpointTuple.pending_writes 读任意 interrupt 负载。

    langgraph 的 interrupt() 以 __interrupt__ channel 写 pending write（value=[Interrupt(...)]）。
    返回值：{"kind": "coach_question", "value": dict} 或 {"kind": "merge_candidates", "value": str}；
    无 pending interrupt 返回 None。
    """
    for _tid, channel, value in (tup.pending_writes or []):
        if channel == "__interrupt__" and value:
            try:
                v = value[0].value if hasattr(value[0], "value") else str(value[0])
            except Exception:  # noqa: BLE001 —— 兜底取值，失败用 str 表示
                v = str(value[0])
            if isinstance(v, dict) and v.get("type") == "coach_question":
                return {"kind": "coach_question", "value": v}
            return {"kind": "merge_candidates", "value": v}
    return None


def _pending_note_interrupt(tup) -> dict | None:
    """向后兼容：仅当 pending interrupt 是 note 合并确认时返回 {candidates_text}。"""
    p = _pending_interrupt(tup)
    if p and p["kind"] == "merge_candidates":
        return {"candidates_text": p["value"]}
    return None


def _parse_thread_ts(thread_id: str) -> str | None:
    """从线程 ID（learn-YYYYMMDD-HHMMSS）解析创建时间，转 ISO 便于前端展示/排序。"""
    m = _THREAD_ID_TS_RE.search(thread_id or "")
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s).isoformat(timespec="seconds")  # noqa: DTZ001 —— thread_id 即本地时间标识，naive 语义
    except ValueError:
        return None


def _checkpoint_ts(tup) -> str | None:
    """从 checkpoint_id（langgraph 自定义 uuid6）解析时间戳，转本地 ISO。

    langgraph 的 uuid6 布局（checkpoint/base/id.py）：前 48 bit = (timestamp >> 12)、
    12 bit = (timestamp & 0x0FFF)，timestamp 为 100ns 自 1582-10-15。拼接后换算 Unix 秒。
    """
    cid = (tup.config.get("configurable") or {}).get("checkpoint_id")
    if not cid:
        return None
    try:
        import uuid
        u = uuid.UUID(cid)
        if u.version == 6:
            ts_100ns = ((u.int >> 80) << 12) | ((u.int >> 64) & 0x0FFF)
            epoch_s = ts_100ns / 1e7 - 12219292800
            return datetime.fromtimestamp(epoch_s).isoformat(timespec="seconds")  # noqa: DTZ006 —— 转本地时间展示，naive 即可
    except Exception:  # noqa: BLE001, S110 —— 解析失败返回 None，调用方回退
        pass
    return None


def _preview(conversation: list[dict], limit: int = 60) -> str:
    """会话预览：最后一条 AI 回复首行截断（无 assistant 时回退最后一条消息）。"""
    for msg in reversed(conversation or []):
        if msg.get("role") == "assistant":
            return ((msg.get("content") or "").strip().replace("\n", " "))[:limit]
    if conversation:
        return ((conversation[-1].get("content") or "").replace("\n", " "))[:limit]
    return ""


def _summarize(thread_id: str, values: dict, tup) -> dict:
    """把每线程最新 checkpoint 压成会话列表项。"""
    tech = (values.get("tech") or "").strip()
    conversation = values.get("conversation") or []
    qa_history = values.get("qa_history") or []
    notes = values.get("notes") or []
    read_count = len([n for n in notes if n.get("report")])
    created_at = (conversation[0].get("ts") if conversation
                  else (_parse_thread_ts(thread_id) or _checkpoint_ts(tup)))
    updated_at = (conversation[-1].get("ts") if conversation else _checkpoint_ts(tup))
    return {
        "thread_id": thread_id,
        # 标题 = 持久化 title（首次 collect 固化）→ tech → 新会话；不带时间（时间在列表元信息展示）
        "title": (values.get("title") or "").strip() or tech or "新会话",
        "tech": tech,
        "created_at": created_at,
        "updated_at": updated_at,
        "preview": _preview(conversation),
        "qa_count": len(qa_history),
        "note_count": read_count,
    }


def create_session() -> str:
    """新建会话：生成 thread_id 并初始化空 checkpoint。

    POST /api/sessions 只返回 id 会让 GET 详情 404（无线程）——这里用
    graph.update_state 写入一个空快照，让详情/列表立即可读。
    """
    from ..graph import build_graph
    thread_id = f"learn-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
    config.GRAPH_DB_DIR.mkdir(parents=True, exist_ok=True)
    with _open_saver() as saver:
        saver.setup()
        graph = build_graph(saver)
        graph.update_state({"configurable": {"thread_id": thread_id}}, {})
    return thread_id


def list_sessions() -> list[dict]:
    """列出全部历史会话（按 updated_at 降序）。"""
    if not config.GRAPH_DB_PATH.exists():
        return []
    sessions: dict[str, dict] = {}
    with _open_saver() as saver:
        for tup in saver.list(None):
            thread_id = (tup.config.get("configurable") or {}).get("thread_id")
            if not thread_id or thread_id in sessions:
                continue  # list 按 checkpoint_id 降序，第一个即该线程最新
            sessions[thread_id] = _summarize(thread_id, _get_state_values(tup), tup)
    return sorted(sessions.values(), key=lambda s: s["updated_at"] or "", reverse=True)


def get_session(thread_id: str) -> dict | None:
    """会话详情：conversation 消息流 + 状态摘要；线程不存在返回 None。

    若会话正处于 note 合并确认（interrupt 暂停），附加 pending_note={candidates_text}，
    前端据此恢复决策面板——SSE 断开 / 切走会话再切回都不丢。
    """
    if not config.GRAPH_DB_PATH.exists():
        return None
    with _open_saver() as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return None
    values = _get_state_values(tup)
    result = {
        "thread_id": thread_id,
        "conversation": values.get("conversation") or [],
        "state": {
            "tech": values.get("tech") or "",
            "focus": values.get("focus") or "",
            "urls": values.get("urls") or [],
            "visited": values.get("visited") or [],
            "notes": values.get("notes") or [],
            "noted_count": values.get("noted_count") or 0,
            "materials_path": values.get("materials_path") or "",
            "qa_history": values.get("qa_history") or [],
        },
    }
    pending = _pending_interrupt(tup)
    if pending:
        if pending["kind"] == "coach_question":
            result["pending_coach"] = pending["value"]  # 前端据此恢复 coach 问答面板
        else:
            result["pending_note"] = {"candidates_text": pending["value"]}
    return result


def delete_session(thread_id: str) -> bool:
    """删除会话线程；不存在返回 False。"""
    with _open_saver() as saver:
        if saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None:
            return False
        saver.delete_thread(thread_id)
        return True
