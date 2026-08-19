"""图执行后台任务：run / resume + SSE 进度事件队列。

WEB_PLAN.md §4-⑤：图执行为同步阻塞 → 丢进后台线程；collect/read 长任务的进度经
管道 `progress=` 回调注入（CLI 传 None，Web 传流式回调）推给 SSE；note 的 interrupt()
（合并确认）两段式：run 跑到 interrupt → 推 `interrupt` 事件 → 前端展示 → resume 提交决策。

线程模型：每个 thread_id 一个 Job（queue + worker 线程），同一时刻只允许一个活跃 worker
（单飞，避免并发写同一 checkpoint 冲突）。事件经 job.queue 投递，SSE 端消费。

I1：本模块顶层不 import langgraph（SqliteSaver / build_graph / Command 全函数内 lazy）。
"""

import queue
import threading
from typing import Any

from ..config import config

# 事件类型：progress(进度) / interrupt(合并确认，等 resume) / final(成功) / error / done(收尾)
_EVENT_QUEUE_TIMEOUT = 15.0


class Job:
    """一个 thread_id 的图执行任务（queue + 活跃 worker 线程）。

    command 记录本次任务类型（collect/read/qa/note），供切回会话时按命令类型恢复 SSE；
    resume（Command 对象）统一视为 note 合并确认的继续。
    """

    __slots__ = ("queue", "thread", "lock", "command")

    def __init__(self) -> None:
        self.queue: queue.Queue[dict] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.command: str | None = None

    @property
    def active(self) -> bool:
        t = self.thread
        return t is not None and t.is_alive()

    def drain(self) -> None:
        """清空遗留事件（新 run 前调用，避免旧事件被新 SSE 连接重放）。"""
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _job(thread_id: str) -> Job:
    with _jobs_lock:
        return _jobs.setdefault(thread_id, Job())


def get_job(thread_id: str) -> Job:
    """供 SSE 端点读取事件（不存在则惰性创建空 job）。"""
    return _job(thread_id)


def _worker(thread_id: str, payload: Any) -> None:
    """后台线程：打开 SqliteSaver → 编译图 → stream_events 执行 → 事件入队。"""
    from ..graph import build_graph, web_progress
    from langgraph.checkpoint.sqlite import SqliteSaver

    job = _job(thread_id)

    def progress(msg: str) -> None:
        job.queue.put({"type": "progress", "message": msg})

    try:
        config.GRAPH_DB_DIR.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(config.GRAPH_DB_PATH)) as saver:
            saver.setup()
            graph = build_graph(saver)
            cfg = {"configurable": {"thread_id": thread_id}}
            # ⚠️ stream_events(v3) 是异步后台执行：返回时节点可能仍在 ThreadPoolExecutor 线程跑。
            # 必须在 with 内立即访问 .interrupted/.output（会阻塞等待后台完成），
            # 否则 web_progress 的 finally 会在节点执行前注销注册表，进度全部丢失。
            with web_progress(thread_id, progress):
                stream = graph.stream_events(payload, cfg, version="v3")
                interrupted = stream.interrupted
                interrupts = stream.interrupts
                output = stream.output
            if interrupted:
                # note 合并确认：把 interrupt 负载（merge 候选展示文本）推给前端等 resume
                value = interrupts[0].value if interrupts else ""
                job.queue.put({"type": "interrupt", "kind": "merge_candidates", "payload": value})
            else:
                job.queue.put({"type": "final", "output": output})
    except Exception as exc:  # noqa: BLE001 —— 线程内兜底，事件里透传错误给前端
        job.queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.queue.put({"type": "done"})
        job.thread = None


def _start(thread_id: str, payload: Any) -> str | None:
    """启动后台图执行；已有活跃任务返回错误消息，否则返回 None（成功启动）。"""
    job = _job(thread_id)
    with job.lock:
        if job.active:
            return "该会话已有任务在运行，请等待完成"
        job.drain()
        job.command = payload.get("command") if isinstance(payload, dict) else "note"
        job.thread = threading.Thread(target=_worker, args=(thread_id, payload), daemon=True)
        job.thread.start()
    return None


def job_info(thread_id: str) -> dict:
    """查询某会话的任务状态：{active, command}；无任务记录返回 False/None。

    供 get_session 端点使用，前端切回会话时据此重连 SSE（command=="note" 按 note 流处理）。
    """
    with _jobs_lock:
        job = _jobs.get(thread_id)
    if job is None:
        return {"active": False, "command": None}
    return {"active": job.active, "command": job.command}


def start_run(thread_id: str, payload: dict) -> str | None:
    """启动一轮图执行（command 契约 dict）。"""
    return _start(thread_id, payload)


def resume_run(thread_id: str, answer: str) -> str | None:
    """恢复 note interrupt：提交合并决策（all / 编号逗号分隔 / skip）。"""
    from langgraph.types import Command
    return _start(thread_id, Command(resume=answer))
