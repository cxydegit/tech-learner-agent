"""审计事件：结构化日志的唯一写出口。

一次事件一行 JSON：先过 stderr（本地开发看着方便），同时落 `logs/audit.jsonl`
（按大小轮转）。**只记元数据——提示词、正文、回复一律不进日志**：学的是用户自己的东西。

三条硬约束：

1. **公共字段只在一处注入**（`ts` / `level` / `thread_id` / `step`）。十几个事件散在七八个
   文件里，靠每个调用点手写公共字段必然漂移，漏一个整条时间线就对不齐。
2. **图内事件 `thread_id` 必非空**：取不到记 `"local"`（与 `graph._coach_thread_id` 的既有约定
   一致），**不记 null**——null 会把"归属机制坏了"和"这里本来就没有会话"混成同一种现象，
   是最坏的可观测性。
3. **落盘失败不能把进程带崩，也不能在导入期产生副作用**：文件惰性打开（第一次真写日志时
   才建目录、开文件），失败就静默降级为仅 stderr。

"""

import hashlib
import json
import logging
import os
import platform
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from ..config import config

LOGGER_NAME = "tech_learner.audit"

# 非图上下文的会话标识。与 graph._coach_thread_id() 的 `or "local"` 保持同一个约定。
NO_THREAD = "local"

# 非图线程的显式会话标识（见 audit_context）。默认 None 才轮到 langgraph 的 config 兜底。
_CONTEXT_THREAD_ID: ContextVar[str | None] = ContextVar("audit_thread_id", default=None)

LOGGER = logging.getLogger(LOGGER_NAME)
LOGGER.setLevel(logging.INFO)
# 不向 root 传播：httpx / openai / langchain 的日志若混进来，"一行一个完整 JSON"当场破产。
LOGGER.propagate = False

_handler_lock = threading.Lock()
_file_handler: "RotatingFileHandler | None" = None


class _LazyRotatingFileHandler(RotatingFileHandler):
    """惰性建目录 + 打不开就静默降级。

    为什么不直接在构造期建目录：`import` 本模块不该产生任何副作用——只读环境、跑测试、
    甚至 `--help` 都不该凭空多出一个 `logs/`。第一次真写日志时再落盘，语义才对得上。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._unavailable = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._unavailable:
            return
        try:
            os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
            super().emit(record)
        except Exception as exc:  # noqa: BLE001 —— 磁盘满 / 无权限 / 路径被占：降级，不抛
            # 只关掉自己：stderr handler 还在，日志不会丢；重复告警没有意义，所以标记一次就够。
            # 绕开 logging 直接写 stderr —— 从这里再发一条日志会递归回本 handler。
            self._unavailable = True
            sys.stderr.write(f"[audit] 审计日志落盘不可用，本进程降级为仅 stderr：{exc}\n")


def _stderr_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    return handler


def configure_audit_log() -> None:
    """按当前 config 重建 handler 集合（进程启动时调用；测试改过配置后也要调用）。

    幂等、线程安全。文件 handler 用 `%(message)s`：**文件里每行必须是完整 JSON**，
    `ts` / `level` 在 JSON 体内，不再依赖行首前缀；stderr 保留人读的 `asctime level` 前缀。
    两边要求不同，所以不能共用一个 formatter。
    """
    global _file_handler
    with _handler_lock:
        for handler in list(LOGGER.handlers):
            LOGGER.removeHandler(handler)
        LOGGER.addHandler(_stderr_handler())
        _file_handler = None
        try:
            handler = _LazyRotatingFileHandler(
                str(Path(config.AUDIT_LOG_DIR) / "audit.jsonl"),
                maxBytes=config.AUDIT_LOG_MAX_BYTES,
                backupCount=config.AUDIT_LOG_BACKUPS,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
        except Exception as exc:  # noqa: BLE001 —— 路径非法等：只留 stderr，不拦启动
            audit("audit_log_unavailable", level=logging.WARNING, reason=f"{type(exc).__name__}: {exc}")
            return
        LOGGER.addHandler(handler)
        _file_handler = handler


def _graph_context() -> tuple[str | None, int | None]:
    """当前 langgraph run 的 (thread_id, step)。

    实测：**节点体内永远读得到**，无论节点跑在哪个线程——LangGraph 会显式设置自己的
    config contextvar，与"节点里另起线程"是两件事。但**节点里自己起的普通子线程读不到**
    （`threading.Thread` 不复制 contextvars），那种调用点必须用 `audit_context()` 显式给。
    """
    try:
        from langgraph.config import get_config

        cfg = get_config()
    except Exception:  # noqa: BLE001 —— 非图上下文（CLI 直调管道 / 脚本）取不到
        return None, None
    thread_id = (cfg.get("configurable") or {}).get("thread_id")
    step = (cfg.get("metadata") or {}).get("langgraph_step")
    return thread_id, step


@contextmanager
def audit_context(thread_id: str | None):
    """在**非图线程**里显式声明本次事件的归属会话。

    调用点：`graph._start_sweep_thread` 起的后台沉淀线程——它手里本来就有 thread_id，
    显式声明比依赖线程继承可靠（普通线程根本不继承 contextvars），否则这条链上的
    LLM 调用与副作用事件会全部退化成无归属。
    """
    token = _CONTEXT_THREAD_ID.set(thread_id or None)
    try:
        yield
    finally:
        _CONTEXT_THREAD_ID.reset(token)


def _current_thread_id() -> str:
    tid = _CONTEXT_THREAD_ID.get()
    if tid:
        return tid
    return _graph_context()[0] or NO_THREAD


def audit(event: str, /, *, level: int = logging.INFO, **fields) -> None:
    """写一条审计事件（**唯一写出口**）。

    Args:
        event: 事件名（蛇形，如 `llm_call` / `tool_call`）
        level: 日志级别，默认 INFO（数据质量类事件用 WARNING 让它从常规里跳出来）
        **fields: 事件字段。**按白名单显式传，不要 `json.dumps(args)` 整包落盘**——
            新工具可能带长正文，整包 dump 会在某次加字段时静默把学习内容写进日志。

    公共字段由本函数注入且**优先级高于同名字段**（调用方传了 `ts=` 也不会覆盖），
    保证"每条事件都带得上会话标识"这条不变式不被局部代码破坏。
    """
    payload = {
        **fields,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": logging.getLevelName(level),
        "thread_id": _current_thread_id(),
        "step": _graph_context()[1],
        "event": event,
    }
    LOGGER.log(level, json.dumps(payload, ensure_ascii=False, default=str))


# ============================================================
# 进程启动快照
# 给之后每一行日志提供"当时的模型与阈值是什么"这个背景——"它变笨了"的第一嫌疑人永远是
# 模型名或某个阈值被改过。只放标量配置，**不放任何 key**。
# ============================================================

_BOOT_SETTINGS = (
    "LLM_MODEL", "REPORT_MAX_TOKENS", "LLM_REQUEST_TIMEOUT", "LLM_REPORT_TIMEOUT",
    "LLM_MAX_ATTEMPTS", "LLM_RETRY_BUDGET_SECONDS", "LLM_HEARTBEAT_SECONDS",
    "ROUTE_MAX_TOOL_CALLS_PER_TURN", "ROUTE_MAX_HEAVY_TOOLS_PER_TURN",
    "ROUTE_MEMORY_SWEEP_TURNS", "ROUTE_MEMORY_SWEEP_CHARS", "ROUTE_MEMORY_SWEEP_ASYNC",
    "ROUTE_KB_INJECT_SIM", "ROUTE_MILESTONE_VERIFY", "ROUTE_FALLBACK_TO_TEXT",
    "COACH_HISTORY_KEEP", "COACH_COMPRESS_AT",
    "QA_USE_HYBRID", "QA_TOP_K", "RAG_TOP_K",
    "RAG_RECONCILE", "RAG_RECONCILE_MIN_DISK_RATIO",
    "EMBEDDING_MODEL", "MAX_FETCH_PAGES", "FETCH_MAX_WORKERS", "FETCH_TIMEOUT_SECONDS",
)


def _safe_host(url: str) -> str:
    """只取 host[:port]。**绝不记完整 URL**——有些网关把 key 放在 query 里。"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return f"{host}:{parsed.port}" if parsed.port else host
    except Exception:  # noqa: BLE001 —— 非法 URL / 非法端口：宁可空缺也不原样记
        return ""


def _project_version() -> str:
    try:
        from importlib.metadata import version

        return version("tech-learner-agent")
    except Exception:  # noqa: BLE001 —— 未安装（直接从源码跑）时不该让启动失败
        return "unknown"


def boot(entry: str) -> None:
    """进程启动时记一条配置快照。entry：`cli` / `web`。"""
    settings = {key: getattr(config, key, None) for key in _BOOT_SETTINGS}
    canonical = json.dumps(settings, sort_keys=True, ensure_ascii=False, default=str)
    audit(
        "boot",
        entry=entry,
        model=config.LLM_MODEL,
        base_url_host=_safe_host(config.OPENAI_BASE_URL),
        cfg_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
        python=platform.python_version(),
        version=_project_version(),
        config=settings,
    )


# 导入时就挂好 handler（与 llm 埋点同一取舍：惰性挂载的"检查-再挂载"不是原子的，
# 而后台沉淀线程与主线程可能同时首次调用，各挂一个会让每行日志重复输出）。
configure_audit_log()
