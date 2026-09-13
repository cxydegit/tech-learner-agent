"""LLM 基础设施：一次性非循环生成 + 系统时间标签注入（确定性兜底）。

自 agent.py 迁出：generate_text（原 _generate_text）。另含 collect/read
管道共用的 current_time_label / replace_time_line（修复时间编造的产物）。
"""

import json
import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from ..config import config
from ..domain.extraction import parse_json_object


def current_time_label() -> str:
    """当前系统时间标签（YYYY-MM-DD HH:MM），注入 collect/read 管道防止 LLM 编造历史日期。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def replace_time_line(report: str, label: str, now: str) -> str:
    """确定性兜底：把报告中的时间行（如 `> 生成时间：xxx`）替换为系统时间。

    即使 LLM 忽略"使用我提供的时间"指令，也能保证 报告内时间 === 当前系统日期。
    """
    return re.sub(rf"(?m)^>\s*{label}\s*[:：].*$", f"> {label}：{now}", report)


# ============================================================
# LLM 调用埋点
# 一次尝试一行 JSON，只记元数据（耗时 / finish_reason / token 数 / 走哪条路），
# **不记 prompt 与回复内容**：学的是用户自己的东西，内容不进日志。
# 默认就开着、不设开关——没有它，「慢」与「截断」只能靠翻 checkpoint 数据库和看文件
# 断口反推，而那正是 2026-09-09 collect 静默 22 分钟那次的定位成本所在。
# ============================================================

_LOGGER = logging.getLogger("tech_learner.llm")
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False  # 应用若另配了 root logger，不重复打印
if not _LOGGER.handlers:
    # 导入时就挂好：惰性挂载的「检查-再挂载」不是原子的，而后台沉淀线程与主线程可能同时
    # 首次调用，各挂一个 handler 会让每行日志重复输出。
    _HANDLER = logging.StreamHandler()
    _HANDLER.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOGGER.addHandler(_HANDLER)


def _log_call(call_site: str, attempt: int, elapsed: float, *, status: str,
              kind: str = "ok", http: int | None = None,
              finish_reason: str | None = None, usage=None,
              fallback: bool = False, error: str = "") -> None:
    """记一次 LLM 请求（含失败与降级）。

    attempt=1..N 是工具调用通道的第几次尝试；attempt=0 且 fallback=True 表示降级那次请求。
    finish_reason 是「截断」的直接实锤（length=撞 max_tokens），elapsed_s 是「慢」的直接实锤。
    """
    _LOGGER.info(json.dumps({
        "event": "llm_call",
        "site": call_site or "unknown",
        "attempt": attempt,
        "elapsed_s": round(elapsed, 1),
        "status": status,
        "kind": kind,
        "http": http,
        "finish_reason": finish_reason,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "fallback": fallback,
        "error": error[:200],
    }, ensure_ascii=False))


@contextmanager
def _heartbeat(progress, label: str, interval: float):
    """长调用期间每隔 interval 秒发一条「仍在进行…（已 Ns）」，退出时停掉旁路线程。

    为什么需要：报告生成占整条管道耗时的 99%，而它期间没有任何中间信号——用户看到
    「🧠 LLM 生成...」之后就是长时间静止（真实事故里静止了 22 分钟）。心跳把这段静默
    变成可见的等待。interval<=0 或没有回调时完全不启动线程。
    """
    if not progress or interval <= 0:
        yield
        return
    stop = threading.Event()
    started = time.monotonic()

    def _beat() -> None:
        while not stop.wait(interval):
            try:
                progress(f"⏳ {label} 仍在进行...（已 {time.monotonic() - started:.0f}s）")
            except Exception:  # noqa: BLE001 —— 进度回调异常绝不影响主流程
                return

    thread = threading.Thread(target=_beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()  # 无论成功/失败/异常都要停，否则会在调用结束后继续发消息


def generate_text(system_prompt: str, user_content: str, *, max_tokens: int | None = None,
                  call_site: str = "", truncation_notice: str = "",
                  progress=None, progress_label: str = "生成报告") -> str:
    """执行一次（非循环的）LLM 生成，返回响应文本。

    适用于"URL → 抓取 → 生成 → 保存"这类确定性管道任务，
    不需要 Agent 自主选择工具，因而跳过 ReAct 循环以降低开销和失败率。

    只打一次、不重试：这是长任务（整篇报告），重发一遍等于再烧一份 token 再等一轮；
    超时上限用 config.LLM_REPORT_TIMEOUT（远大于对话通道），失败由调用方决定如何降级。

    Args:
        system_prompt: 系统提示词
        user_content: 用户内容（已抓取的文档等）
        max_tokens: 可选，覆盖 config.LLM_MAX_TOKENS（整篇报告用 config.REPORT_MAX_TOKENS）
        call_site: 埋点用调用点标识（如 collect.report），用于从日志定位是谁在调
        truncation_notice: 可选，被生成上限截断（`finish_reason == "length"`）时追加到正文
            末尾的标注。**只给报告类调用点传**——判定类调用点（dedup / note / verify）输出
            是 JSON，追加标注会破坏解析，它们的解析失败路径已各自处理。
        progress: 可选进度回调；长调用期间会由心跳线程周期发"仍在进行"
        progress_label: 心跳文案里的动作名（如「LLM 生成学习资料」）

    Returns:
        LLM 生成的文本（若传入 truncation_notice 且被截断，末尾带该标注）
    """
    kwargs: dict = {
        "model": config.LLM_MODEL,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "temperature": 0.5,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    start = time.monotonic()
    try:
        with _heartbeat(progress, progress_label, config.LLM_HEARTBEAT_SECONDS):
            response = _get_client().chat.completions.create(
                **kwargs, timeout=config.LLM_REPORT_TIMEOUT)
    except Exception as e:  # noqa: BLE001 —— 记完就抛，由调用方决定降级（不在这里吞）
        _log_call(call_site, 1, time.monotonic() - start, status="error",
                  kind=_classify(e), http=getattr(e, "status_code", None),
                  error=f"{type(e).__name__}: {e}")
        raise
    finish = response.choices[0].finish_reason
    _log_call(call_site, 1, time.monotonic() - start, status="ok",
              finish_reason=finish, usage=getattr(response, "usage", None))
    text = response.choices[0].message.content
    if finish == "length":
        # 截断是数据质量事件，用 WARNING 让它从常规 INFO 里跳出来
        _LOGGER.warning(json.dumps({
            "event": "llm_truncated", "site": call_site or "unknown",
            "completion_tokens": getattr(getattr(response, "usage", None), "completion_tokens", None),
            "annotated": bool(truncation_notice),
        }, ensure_ascii=False))
        if truncation_notice:
            text = f"{text}{truncation_notice}"
    return text


# ============================================================
# 工具调用通道（coach agent 循环用）
# 与 generate_text 的区别：向模型暴露 tools 定义，模型可返回 tool_calls。
# RISKS 教训的回应：大内容走文件、工具出入参短、失败回退——由 graph 层护栏兜底。
# ============================================================


class ToolCallError(Exception):
    """工具调用通道持久失败（重试 + 回退后仍失败），由 graph 层降级处理。

    fatal=True 表示确定性错误（鉴权失效 / 模型名不存在等）：重试与降级都不会有
    不同结果。调用方据此换提示语——此时让用户"稍后再试"是骗他，等多久都不会好。
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


# 降级（去掉 tools）请求的系统提示词后缀：必须显式告知模型工具没了。
# 不告知的话提示词里仍写着"你可以用工具推进学习：collect / read / update_roadmap…"，
# 模型会输出"好的，我已经为你生成了路线"这类叙述式假成功——用户看不出什么都没落库。
FALLBACK_SYSTEM_SUFFIX = (
    "\n\n【系统状态】本次调用的工具通道不可用：你这次无法调用任何工具。"
    "请直接用纯文本回复用户；严禁声称你已完成任何需要工具才能完成的动作"
    "（生成/修改路线、勾选里程碑、收集资料、解读文档、写入笔记等）。"
    "用户的要求若必须借助工具，就直接说明现在无法执行、建议稍后重试。"
)

# 报告被生成上限截断时追加到正文末尾的标注：残篇若被当作完整资料读进知识库，事后就分不清
# 「原文没有」和「被切掉了」。宁可留一条丑标记，也不让半篇内容伪装成完篇（由报告调用点传入）。
REPORT_TRUNCATION_NOTICE = ("\n\n---\n\n> ⚠️ **本报告未写完**：生成时达到长度上限被截断，"
                            "以上内容不完整，请勿作为完整资料引用。")

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """进程内单例（复用连接池）+ 显式超时/重试策略。

    max_retries=0：关掉 SDK 内置重试，避免与外层重试叠乘（曾出现 3 外层 × 3 内置
    = 9 次请求）。重试与退避统一在 chat_with_tools 里做，次数和总耗时都可封顶。
    timeout：SDK 默认 600s 会把交互式 coach 循环挂死，必须显式收紧。
    """
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
            timeout=config.LLM_REQUEST_TIMEOUT,
            max_retries=0,
        )
    return _client


# 异常分档（见 _classify）：原样重试 / 超时立即失败 / 不重试但可降级 / 两类都不做
_RETRY, _TIMEOUT, _DEGRADE, _ABORT = "retry", "timeout", "degrade", "abort"

# 可重试的状态码（超时 / 冲突 / 限流；≥500 另行判断）：其余 4xx 都是"请求本身不合法"
_RETRY_STATUS = frozenset({408, 409, 429})
# 请求体不合法：可能就是 tools 定义不被该模型支持 → 不重试，但值得降级一试。
# 其余 4xx（401/403/404 鉴权与模型名等）归 _ABORT：降级请求只少了 tools，必然同样失败。
_PAYLOAD_STATUS = frozenset({400, 422})


def _classify(e: Exception) -> str:
    """按「重试能不能改变结果」给异常分档。"""
    if isinstance(e, APITimeoutError):
        # 超时既不重试也不降级：实测最坏合法生成 ≈23s，45s 超时是它的 2 倍，更像故障而不是
        # "生成得慢"；重发（或降级再发）等于把整条 prompt 再烧一遍、再让用户等一遍。
        return _TIMEOUT
    if isinstance(e, APIConnectionError):  # 连接失败 / 重置 / DNS：便宜，且常是瞬时的
        return _RETRY
    if isinstance(e, APIStatusError):
        if e.status_code in _RETRY_STATUS or e.status_code >= 500:
            return _RETRY
        return _DEGRADE if e.status_code in _PAYLOAD_STATUS else _ABORT
    # 非 SDK 异常 = 本模块或 SDK 内部的 bug（响应结构异常 / 类型错），重试无意义，
    # 也不能包装成"模型暂时不可用"骗用户重试——原样抛出让它显形。
    return _ABORT


def _is_deterministic(e: Exception) -> bool:
    """请求本身不合法（4xx 里除瞬时状态码外）：重试不会有不同结果。

    降级那次也吃确定性错误 → 说明与 tools 无关，整条通道本轮没救。
    """
    return (isinstance(e, APIStatusError)
            and 400 <= e.status_code < 500
            and e.status_code not in _RETRY_STATUS)


def _fatal_message(e: APIStatusError) -> str:
    """确定性错误的用户可读文案：HTTP 码 + 该码最可能的原因。

    笼统的"暂时不可用/请稍后再试"
    会把排查带偏——原因要具体，用户才知道该看 key、模型名，还是会话历史。
    """
    hint = {
        401: "API key 无效", 403: "无权限", 404: "模型名不存在",
        400: "请求被拒绝（参数或会话历史不合法）", 422: "请求参数不合法",
    }.get(e.status_code, "请求被拒绝")
    return f"HTTP {e.status_code}（{hint}）：{str(e.message)[:200]}"


def _backoff_delay(attempt: int) -> float:
    """指数退避 + 抖动：首次故障往往几百毫秒到数秒自愈，立即重试只是白打三次。"""
    return config.LLM_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.3)


def _fallback_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """降级请求的 messages：系统提示词追加「工具不可用」声明（见 FALLBACK_SYSTEM_SUFFIX）。"""
    return [{"role": "system", "content": system_prompt + FALLBACK_SYSTEM_SUFFIX}, *messages]


def _parse_chat_response(msg) -> dict:
    """把 openai 响应消息转成统一 dict：{content, tool_calls:[{id,name,arguments}]}。

    arguments 是 JSON 字符串，解析失败兜底为 {}（graph 层护栏会拦截异常参数）。
    """
    tool_calls = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        args = tc.function.arguments or ""
        try:
            args_obj = json.loads(args) if args.strip() else {}
        except Exception:  # noqa: BLE001 —— 模型给的 arguments 不合法 JSON，兜底空 dict
            args_obj = {}
        tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args_obj})
    return {"content": msg.content, "tool_calls": tool_calls}


def chat_with_tools(system_prompt: str, messages: list[dict], tools: list[dict],
                    *, max_tokens: int | None = None, call_site: str = "") -> dict:
    """执行一次带原生工具定义的对话补全。

    与 generate_text 的定位不同：generate_text 是"单次生成"，适用于确定性管道；
    chat_with_tools 暴露工具，模型可返回 tool_calls，供 coach 循环反复调用。

    Args:
        system_prompt: 系统提示词（mode 相关，由调用方按模式挑选）
        messages: 历史消息（dict 列表，含 role/content/tool_calls/tool_call_id），
            格式对齐 openai 兼容接口（DashScope 支持原生 tool_calls）
        tools: OpenAI function 定义列表
        max_tokens: 可选，覆盖 config.LLM_MAX_TOKENS
        call_site: 埋点用调用点标识（如 coach.chat），用于从日志定位是谁在调

    Returns:
        {"content": str | None, "tool_calls": [{id, name, arguments(dict)}], "fallback": bool}
        - 模型决定调用工具：content 可为 None、tool_calls 非空
        - 模型直接回复文本：tool_calls 为空
        - fallback=True：工具通道持久失败后去掉 tools 再问一次拿到的纯文本。
          调用方必须当降级处理——该回复**没有执行过任何工具动作**（路线/进度/笔记
          均未变更），要给用户显式提示，否则会被当成一条正常回复。
        - 确定性错误（key/模型名/无权限）：抛 ToolCallError（fatal=True），别建议重试
        - 超时（LLM_REQUEST_TIMEOUT）：立即抛 ToolCallError，不重试也不降级
        - 代码 bug（响应解析等非 SDK 异常）：原样抛出，不包装、不重试
    """
    kwargs: dict = {
        "model": config.LLM_MODEL,
        "temperature": 0.5,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
    }
    client = _get_client()
    deadline = time.monotonic() + config.LLM_RETRY_BUDGET_SECONDS
    last_err: Exception | None = None
    for attempt in range(config.LLM_MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break  # 预算耗尽 → 立刻降级，不让用户继续干等
        start = time.monotonic()
        try:
            response = client.chat.completions.create(
                **kwargs, tools=tools, timeout=min(config.LLM_REQUEST_TIMEOUT, remaining))
            parsed = _parse_chat_response(response.choices[0].message)
            _log_call(call_site, attempt + 1, time.monotonic() - start, status="ok",
                      finish_reason=response.choices[0].finish_reason,
                      usage=getattr(response, "usage", None))
            parsed["fallback"] = False
            return parsed
        except Exception as e:  # noqa: BLE001 —— 按错误性质分流，见 _classify
            kind = _classify(e)
            _log_call(call_site, attempt + 1, time.monotonic() - start, status="error",
                      kind=kind, http=getattr(e, "status_code", None),
                      error=f"{type(e).__name__}: {e}")
            if kind == _TIMEOUT:
                # 超时直接失败：不重试、也不降级（降级仍是同一条流水线上的又一次完整生成）
                raise ToolCallError(
                    f"请求超时（{config.LLM_REQUEST_TIMEOUT:.0f}s）：{e}") from e
            if kind == _ABORT:
                if isinstance(e, APIStatusError):
                    raise ToolCallError(_fatal_message(e), fatal=True) from e
                raise  # 代码 bug：不重试、不包装，直接显形
            last_err = e
            if kind == _DEGRADE:
                break  # 本请求没救（如 tools 不被该模型支持）→ 直接进降级
            delay = _backoff_delay(attempt)
            if time.monotonic() + delay >= deadline:
                break  # 退避完就超预算：不如现在降级
            time.sleep(delay)
    if config.ROUTE_FALLBACK_TO_TEXT:
        start = time.monotonic()
        try:
            fallback_kwargs = {**kwargs, "messages": _fallback_messages(system_prompt, messages)}
            response = client.chat.completions.create(
                **fallback_kwargs, timeout=config.LLM_REQUEST_TIMEOUT)
            parsed = _parse_chat_response(response.choices[0].message)
            _log_call(call_site, 0, time.monotonic() - start, status="ok", fallback=True,
                      finish_reason=response.choices[0].finish_reason,
                      usage=getattr(response, "usage", None))
            parsed["fallback"] = True
            return parsed
        except Exception as e:  # noqa: BLE001 —— 降级也失败，只能上报
            _log_call(call_site, 0, time.monotonic() - start, status="error", fallback=True,
                      kind=_classify(e), http=getattr(e, "status_code", None),
                      error=f"{type(e).__name__}: {e}")
            last_err = e
    if last_err is None:  # 首次请求前预算就耗尽：没有真实错误可报，是配置问题
        raise ToolCallError("重试预算耗尽（请检查 LLM_RETRY_BUDGET_SECONDS）")
    if _is_deterministic(last_err):
        # 降级那份请求也被判 4xx → 原因不在 tools，重试无益 → fatal，提示语不再劝重试
        raise ToolCallError(_fatal_message(last_err), fatal=True) from last_err
    raise ToolCallError(str(last_err)) from last_err


# ============================================================
# 去重 LLM 判定
# 旧确定性确认层（标题/标签/内容 overlap）对真正措辞不同的同义改写确认率仅 9%，
# 新方案把「是否同一知识点」交给 LLM 判定；标题 fast-path（domain/dedup）先挡掉
# 「标题基本同一句」的平凡情况省一次调用。判定输出 same/diff + 理由，理由供
# merge_candidates 展示（用户确认时看到为什么建议合并）。
# ============================================================

DEDUP_JUDGE_SYSTEM_PROMPT = """你是一个知识库去重助手。给你一篇**新提取的知识点**和一篇**已有笔记**，判断它们是否应该**归入同一篇笔记**（新知识点是否可以合并进已有笔记，而不是独立成篇）。

## 判定标准
- "same"：新知识点与已有笔记是**同一主题**——属于同一条知识线（如都是缓存问题、都是持久化问题）。同一主题下的不同子问题（如缓存穿透、缓存雪崩）也判 same：合并进同一篇笔记补充细节即可，不追求拆成更细的独立笔记。
- "diff"：新知识点与已有笔记**讲的完全不是同一件事**——只是看起来相似（同领域、用词相近），但主题根本不同，合并会污染已有笔记。

## 反面示例（不要犯）
- 「Redis 数据结构选型」与「Redis 五大核心角色」：一个讲选型、一个讲角色分工，主题不同 → diff
- 「Redis 缓存问题」与「Redis 持久化」：同属 Redis 但主题不同 → diff
- 同属 Redis 领域、都提到"高性能"这类泛泛内容，不算同一主题

## 正面示例
- 「Redis 持久化原理」与「Redis 持久化机制（RDB 与 AOF）」→ same（新知识点是对已有笔记的展开）
- 「缓存穿透」与「缓存雪崩」→ same（同一主题：缓存问题，合并进同一篇补充）
- 「用少量内存统计海量数据」与「概率数据类型（布隆过滤器 / HyperLogLog）」→ same

## 输出
只输出一个 JSON 对象，不要任何解释或 ```json 代码块标记：
{"verdict": "same" 或 "diff", "reason": "一句话中文理由（20 字内）"}
"""


def judge_same_knowledge_point(topic: str, tags: list[str] | None, content: str | None,
                               existing: dict) -> tuple[str, str]:
    """LLM 判定新知识点与已有笔记是否同一主题（可归入同一篇笔记）。

    粒度对齐知识库习惯：**按主题聚合**——同一主题下的不同子问题（缓存穿透 vs
    缓存雪崩）判 same 合并；只有「看起来相似但讲的完全不是同一件事」才判 diff。
    判定是**建议**不是决定：判定 same 后仍送 merge_candidates 由用户确认；
    判定失败（网络/解析异常）由调用方降级为不合并（安全侧）。

    Args:
        topic: 新知识点标题
        tags: 新知识点标签（判定上下文）
        content: 新知识点正文（判定上下文）
        existing: 已有笔记 dict（需含 topic / tags / content 字段）

    Returns:
        (verdict, reason)：verdict ∈ {"same", "diff"}，reason 为 LLM 给出的一句话理由
        （可能为空字符串）。
    """
    tag_str = " ".join(f"#{t}" for t in (tags or []))
    old_tag_str = " ".join(f"#{t}" for t in (existing.get("tags") or []))
    user_content = (
        f"===== 新提取的知识点 =====\n"
        f"标题：{topic}\n标签：{tag_str}\n"
        f"正文：{(content or '').strip()[:2000]}\n\n"
        f"===== 已有笔记 =====\n"
        f"标题：{existing.get('topic') or ''}\n标签：{old_tag_str}\n"
        f"正文：{(existing.get('content') or '').strip()[:2000]}"
    )
    raw = generate_text(DEDUP_JUDGE_SYSTEM_PROMPT, user_content, call_site="dedup.judge")
    obj = parse_json_object(raw)
    verdict = obj.get("verdict")
    if verdict not in ("same", "diff"):
        verdict = "diff"  # 解析失败 / 模型输出异常 → 安全侧：不合并
    return verdict, str(obj.get("reason") or "").strip()
