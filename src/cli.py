"""CLI 命令行入口"""

import shlex
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .config import config
from .domain.card_input import parse_card_input
from .pipelines.collect import collect_pipeline
from .pipelines.note import note_pipeline, parse_merge_decision, persist_points
from .pipelines.read import read_pipeline

# Windows 原生控制台默认 GBK 编码，无法输出 emoji/部分中文符号。
# 强制 stdout/stderr 使用 UTF-8，避免 Rich 渲染 emoji 时报 UnicodeEncodeError。
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

console = Console()


@click.group()
@click.version_option(version="0.1.0", prog_name="tech-learner")
def cli():
    """技术学习陪练 Agent —— 资料收集 + 文档解读 + 知识沉淀"""
    # 启动前检查
    missing = config.validate()
    if missing:
        console.print(f"[red]❌ 缺少必要的环境变量: {', '.join(missing)}[/red]")
        console.print("[dim]请复制 .env.example 为 .env 并填入你的 API Keys[/dim]")
        sys.exit(1)

    config.ensure_dirs()


@cli.command()
@click.argument("tech_name", required=False)
@click.argument("focus", nargs=-1, required=False)
def collect(tech_name: str, focus: tuple = ()):
    """收集指定技术的学习资料。

    TECH_NAME: 技术名称，如 "Spring Boot 3"、"FastAPI"（含空格需加引号）
    FOCUS: 可选，关注点（自由文本，多词自动拼接），如 collect FastAPI 异步编程
    """
    parsed = parse_card_input("collect", [tech_name, *focus])
    if parsed.get("error"):
        console.print(f"[yellow]{parsed['error']}[/yellow]")
        return
    tech = parsed["tech"]
    focus_text = parsed.get("focus")
    title = f"📚 开始收集「{tech}」的学习资料"
    if focus_text:
        title += f"（关注：{focus_text}）"
    title += "..."
    console.print(Panel(title, style="bold blue"))

    result = collect_pipeline(tech, focus_text, progress=lambda m: console.print(m))
    console.print(f"✅ 共收集到 [bold]{len(result['urls'])}[/bold] 条去重资源")
    console.print(f"├  保存报告: [bold]{result['materials_path']}[/bold]")
    console.print(Panel(Markdown(result["report"][:3000]), title="✅ 资料收集完成", style="green"))


@cli.command()
@click.argument("url", required=False)
def read(url: str | None = None):
    """解读指定的技术文档。

    URL: 文档页面链接
    """
    parsed = parse_card_input("read", [url] if url else [])
    if parsed.get("error"):
        console.print(f"[yellow]{parsed['error']}[/yellow]")
        return
    url = parsed["args"][0]
    console.print(Panel("📖 开始解读文档...", style="bold blue"))
    console.print(f"[dim]{url}[/dim]")

    # RAG 历史召回：该 URL 已有解读则提示复用（失败静默，不影响抓取）
    if _try_reuse_cached_report(url):
        return

    result = read_pipeline(url, progress=lambda m: console.print(m))
    if result.get("error"):
        console.print(f"[red]❌ {result['error']}[/red]")
        return
    console.print(f"├  保存报告: [bold]{result['report_path']}[/bold]")
    if result.get("index_ok") is False:
        console.print(f"[yellow]⚠️ 报告已保存，但 RAG 索引未更新"
                      f"（{result.get('index_error')}），read 缓存命中暂时不可用，"
                      f"下次 note/index 对账自动补齐[/yellow]")
    console.print(Panel(Markdown(result["report"]), title="✅ 文档解读完成", style="green"))


@cli.command()
@click.argument("tech")
@click.option("--file", "-f", "file_path", help="从本地文件读取学习内容")
@click.option("--text", "-t", "content", help="直接提供学习内容文本")
def note(tech: str, file_path: str | None = None, content: str | None = None):
    """将学习内容整理为结构化笔记（差量提取，重复内容不沉淀）。

    TECH: 技术名称
    """
    if file_path:
        full_path = Path(file_path)
        if not full_path.exists():
            console.print(f"[red]文件不存在: {file_path}[/red]")
            sys.exit(1)
        conversation_log = full_path.read_text(encoding="utf-8")
    elif content:
        conversation_log = content
    else:
        # 从 stdin 读取
        console.print("[dim]请输入学习内容（Ctrl+D 或 Ctrl+Z 结束）:[/dim]")
        conversation_log = sys.stdin.read()

    if not conversation_log.strip():
        console.print("[red]未提供学习内容[/red]")
        sys.exit(1)

    console.print(Panel(f"📝 开始整理「{tech}」的学习笔记...", style="bold blue"))

    # materials_path：该技术的 materials 报告存在则带上（无新内容时用于推荐未覆盖方向）
    materials_path = _find_materials_path(tech)

    result = note_pipeline(tech, conversation_log, materials_path=materials_path,
                           progress=lambda m: console.print(m))

    # 无新内容路径：不沉淀，有 materials 则给方向推荐
    if result["empty_reason"]:
        console.print(f"[yellow]ℹ {result['empty_reason']}，未沉淀。[/yellow]")
        if result.get("suggestion"):
            console.print(Panel(Markdown(result["suggestion"]), title="📌 建议继续学习的方向", style="cyan"))
        return

    # 合并候选：汇总展示 + 一次 input() 统一决定 全合并/逐条/跳过
    merge_indices: set[int] = set(range(len(result["merge_candidates"])))
    if result["merge_candidates"]:
        merge_indices = _prompt_merge_candidates(result["merge_candidates"])

    # 用户确认后入库
    persisted = persist_points(tech, result["new_points"], result["merge_candidates"], merge_indices)

    if not persisted["results"]:
        console.print("[yellow]ℹ 未沉淀任何知识点。[/yellow]")
        return

    console.print(f"✅ 本次沉淀：新增 [bold]{persisted['new_count']}[/bold] 篇，"
                  f"合并更新 [bold]{persisted['merged_count']}[/bold] 篇")
    for c in (persisted.get("conflict_reports") or []):
        console.print(f"  [bold yellow]⚠️ 合并发现矛盾：[/bold yellow]{c['report']}")
    for r in persisted["results"]:
        label = "🆕 新增" if r["action"] == "new" else "🔗 合并"
        console.print(f"  {label} [bold]{r['topic']}[/bold] → knowledge/{r['path']}")
        if r.get("index_ok") is False:
            console.print(f"    [bold yellow]⚠️ RAG 索引更新失败（{r.get('index_error')}）："
                          f"笔记已保存，下次运行 index 会自动补齐[/bold yellow]")

    console.print(Panel(Markdown(f"知识沉淀完成，共 {len(persisted['results'])} 个知识点已写入 `knowledge/`。"
                                  f"详见 knowledge/INDEX.md"), title="✅ 学习成果沉淀完成", style="green"))


@cli.command()
@click.option("--force", "-f", is_flag=True,
              help="忽略变更检测，强制重新切块嵌入（分块器升级 / 切块参数调整后需要）")
def index(force: bool = False):
    """建立 / 增量更新 RAG 语义索引（knowledge/ + materials/ + reports/）。

    使用 DashScope text-embedding-v3 嵌入分块，存入本地 Chroma（.chroma/）。
    已索引且内容未变化的文件会自动跳过，不会重复计费；分块器升级后用 --force
    或直接跑（版本号变更会自动触发全量重切）。
    """
    from .adapters.vector import index_documents

    console.print("🧠 [bold cyan]构建 RAG 语义索引...[/bold cyan]")
    result = index_documents(force=force)
    console.print(f"✅ 索引完成：新增 [bold]{result['indexed']}[/bold] 个文件，"
                  f"跳过 [bold]{result['skipped']}[/bold] 个未变化文件")
    orphans = result.get("orphans", 0)
    if orphans:
        console.print(f"🧹 清理 [bold]{orphans}[/bold] 个磁盘已删除文件的残留分块")
    backfilled = result.get("backfilled", 0)
    if backfilled:
        console.print(f"🩹 对账补齐 [bold]{backfilled}[/bold] 个缺失文件（此前索引失败/未索引）")
    if result["errors"]:
        console.print("[yellow]部分文件索引失败：[/yellow]")
        for e in result["errors"]:
            console.print(f"  [dim]{e}[/dim]")


def _route_threads() -> list[dict]:
    """读取所有定制路线会话线程（state.command == "route"）的最新状态，供 --list 找回。"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    if not config.GRAPH_DB_PATH.exists():
        return []
    rows = []
    seen: set[str] = set()
    with SqliteSaver.from_conn_string(str(config.GRAPH_DB_PATH)) as saver:
        for tup in saver.list(None):  # checkpoint_id 降序 → 每线程第一个即最新
            tid = (tup.config.get("configurable") or {}).get("thread_id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            ckpt = tup.checkpoint
            values = ckpt.get("channel_values") if isinstance(ckpt, dict) else {}
            if values.get("command") != "route":
                continue
            conv = values.get("conversation") or []
            rows.append({
                "thread_id": tid,
                "tech": values.get("tech") or "",
                "title": values.get("title") or "",
                "updated_at": (conv[-1].get("ts") if conv else "") or "",
            })
    return rows


def _thread_values(thread_id: str) -> dict:
    """读取某会话线程的最新 checkpoint 状态（--resume 时自动取 tech 等字段）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    if not config.GRAPH_DB_PATH.exists():
        return {}
    try:
        with SqliteSaver.from_conn_string(str(config.GRAPH_DB_PATH)) as saver:
            tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
            if tup is None:
                return {}
            ckpt = tup.checkpoint
            return ckpt.get("channel_values") if isinstance(ckpt, dict) else {}
    except Exception:  # noqa: BLE001 —— 线程读取失败按空处理
        return {}


def _list_route_threads() -> None:
    """打印所有 route 会话线程（找回入口）。"""
    rows = _route_threads()
    if not rows:
        console.print("[dim]暂无定制路线会话（先运行 route <技术名>）[/dim]")
        return
    console.print("[bold]已有的定制路线会话：[/bold]")
    for r in sorted(rows, key=lambda x: x["updated_at"], reverse=True):
        console.print(f"  [bold]{r['thread_id']}[/bold]  {r['tech'] or r['title'] or ''}"
                      f"[dim]  {r['updated_at'] or ''}[/dim]")
    console.print("[dim]恢复：route <技术名> --resume <thread_id>[/dim]")


@cli.command()
@click.argument("tech_name", required=False)
@click.option("--list", "list_threads", is_flag=True, help="列出所有定制路线会话（找回用）")
@click.option("--resume", "resume_thread", default=None, help="恢复指定会话线程（thread_id 见 --list）")
def route(tech_name: str | None = None, list_threads: bool = False, resume_thread: str | None = None):
    """定制学习路线：问卷 → 学习路线 → 执行陪练（coach agent 循环）。

    TECH_NAME: 要学习的技术名（含空格需加引号）；不传则交互式询问。

    --list:   列出所有定制路线会话。
    --resume: 恢复指定会话线程（退出后继续上次陪练；thread_id 见 --list）。
    """
    from .adapters import learner as learner_mod
    from .graph import open_graph

    if list_threads:
        _list_route_threads()
        return

    # --resume 且未给技术名：从该线程状态读取 tech，避免重复输入
    if resume_thread and not tech_name:
        tech_name = _thread_values(resume_thread).get("tech") or ""

    if not tech_name:
        try:
            tech_name = input("要学习哪个技术？> ").strip()
        except (EOFError, KeyboardInterrupt):
            tech_name = ""
    parsed = parse_card_input("route", [tech_name] if tech_name else [])
    if parsed.get("error"):
        console.print(f"[yellow]{parsed['error']}[/yellow]")
        return

    thread_id = resume_thread or None
    if thread_id is None:
        # 检测已有路线 → 询问继续 / 重新规划（roadmap.json 记录了生成它的会话线程）
        existing = learner_mod.load_roadmap(parsed["tech"])
        if existing and existing.get("session_thread_id"):
            thread_id = existing["session_thread_id"]
            console.print(Panel(
                f"目标：{existing.get('goal') or ''}\n"
                f"当前阶段：{existing.get('current_stage') or ''}",
                title=f"🧭 已有「{parsed['tech']}」学习路线", style="cyan"))
            ans = input("[c] 继续上次陪练  [n] 重新规划  [回车] 退出 > ").strip().lower()
            if ans in ("n", "new", "重"):
                thread_id = None  # 重新规划 → 新会话
            elif ans not in ("c", "continue", "继续", "y", "yes"):
                return  # 回车/其他 → 退出

    if thread_id is None:
        thread_id = f"route-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
        console.print(Panel(f"🧭 开始定制「{parsed['tech']}」的学习路线（问卷 → 路线 → 陪练）", style="bold blue"))
    else:
        console.print(Panel(f"🧭 继续「{parsed['tech']}」的定制路线（会话 {thread_id}）", style="bold blue"))

    with open_graph() as graph:
        gconfig = {"configurable": {"thread_id": thread_id}}
        _drive(graph, gconfig, parsed, render="markdown")


# ============================================================
# note 交互辅助
# ============================================================

def _find_materials_path(tech: str) -> str | None:
    """查找该技术最近一份 materials 报告（collect 生成 `materials/<tech>-materials-<时间>.md`，按 mtime 取最新）。"""
    safe = tech.lower().replace(" ", "-")
    matches = sorted(config.MATERIALS_DIR.glob(f"{safe}-materials*.md"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    return str(matches[0]) if matches else None


def _prompt_merge_candidates(candidates: list[dict]) -> set[int]:
    """汇总展示相似候选，一次 input() 让用户统一决定 全合并/逐条/跳过。

    Returns:
        要合并的候选索引集合（0-based）；空集表示全部跳过。
    """
    console.print("[yellow]发现以下新知识点与已有笔记相似：[/yellow]")
    for i, c in enumerate(candidates, 1):
        sim = f"（相似度 {c['similarity']:.2f}）" if c.get("similarity") is not None else ""
        delta = (c.get("content") or "").strip().replace("\n", " ")[:120]
        console.print(f"  [bold][{i}][/bold] {c['topic']} → 已有「{c['old_topic']}」{sim}")
        console.print(f"      [dim]新增点：{delta}[/dim]")
    try:
        ans = input("如何处理？全合并 [a] / 逐条选择编号（如 1,3）/ 跳过 [s]：").strip()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return parse_merge_decision(ans, len(candidates))


# ============================================================
# /learn 交互式学习会话
# ============================================================

_HELP_TEXT = """可用命令：
  collect <技术名> [关注点]      收集学习资料（关注点可选，多词直接拼）
  read <url>                      解读技术文档
  note                            将本次会话内容沉淀为知识笔记
  /ask <问题>                    联想检索笔记库并综合回答（不依赖技术主题）
  /status                         查看会话状态
  /done                           沉淀并结束会话
  /quit                           退出（状态已持久化）
  /help                           查看帮助

提示：技术名含空格时用引号包裹，如 collect "Spring Boot"、collect FastAPI 异步编程
"""


def _confirm_reuse() -> bool:
    """询问用户是否复用已有解读报告（默认不复用，避免误跳过新文档）。"""
    try:
        ans = input("是否复用已有解读，跳过抓取？[y/N] ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _try_reuse_cached_report(url: str, *, graph=None, gconfig=None) -> bool:
    """RAG 历史召回：命中已有解读则询问是否复用；复用成功返回 True。

    三种调用场景合一（原 run_read 内联块 + agent._reuse_cached_report + _handle_read_graph）：
    - standalone `read`：graph/gconfig 均为 None → 仅展示复用报告，无状态写入
    - /learn 内 `read`：graph/gconfig 提供 → 命中后 update_state 记录该文档已读
    """
    try:
        from .adapters.vector import check_read_cache
        cached = check_read_cache(url)
    except Exception:  # noqa: BLE001 —— RAG 不可用时静默降级
        cached = None
    if not cached:
        return False

    console.print(Panel(
        f"📌 检测到该 URL 已有解读报告：\n[bold]{cached['path']}[/bold]"
        f"（相似度 {cached['similarity']:.2f}）",
        title="⏭ RAG 缓存命中",
        style="cyan",
    ))
    if not _confirm_reuse():
        return False

    # 优先读取完整报告文件；文件丢失时退回缓存的片段
    report_path = config.BASE_DIR / cached["path"]
    if report_path.exists():
        report = report_path.read_text(encoding="utf-8", errors="replace")
    else:
        report = cached.get("content", "")

    console.print(Panel(
        f"[dim]{cached['path']}[/dim]\n相似度 {cached['similarity']:.2f}",
        title="📄 复用已有解读",
        style="cyan",
    ))
    console.print(Markdown(report))

    if graph is not None and gconfig is not None:
        # 直接更新图状态（不跑节点），记录该文档已读
        graph.update_state(gconfig, {
            "visited": [url],
            "notes": [{"url": url, "title": cached["path"], "report": report}],
        })
    return True


def _drive(graph, gconfig, payload, render: str = "plain") -> dict:
    """驱动一次图执行；遇 interrupt 暂停询问，以 Command(resume) 恢复。

    Args:
        graph: build_graph 编译后的状态图
        gconfig: {"configurable": {"thread_id": ...}}
        payload: 命令输入（dict）或恢复用的 Command
        render: 结束时 last_output 的渲染方式（plain / markdown）

    Returns:
        最终状态 dict（含 last_output）
    """
    from langgraph.types import Command

    while True:
        # recursion_limit 是运行 config 的一部分（防 coach 循环失控打转）
        run_config = {**gconfig, "recursion_limit": gconfig.get("recursion_limit", config.ROUTE_RECURSION_LIMIT)}
        stream = graph.stream_events(payload, run_config, version="v3")
        if not stream.interrupted:
            final = stream.output
            last = (final or {}).get("last_output")
            if last:
                if render == "markdown":
                    console.print(Markdown(last))
                else:
                    console.print(last)
            return final
        resumed = False
        for intr in stream.interrupts:
            val = intr.value
            if isinstance(val, dict) and val.get("type") == "coach_question":
                # coach 循环：interrupt 负载是结构化问题（mode / tech / message）
                mode = val.get("mode") or ""
                tech = val.get("tech") or ""
                badge = f"🧭 [{mode}]{(' ' + tech) if tech else ''}"
                console.print(f"\n[bold cyan]{badge}[/bold cyan]")
                console.print(Markdown(val.get("message") or ""))
            else:
                console.print(f"\n[bold cyan]🧭 {val}[/bold cyan]")
            payload = Command(resume=input("> ").strip())
            resumed = True
        if not resumed:
            # 理论不可达：interrupted 却无 interrupt 负载，避免死循环
            return stream.output


def _print_graph_status(graph, gconfig) -> None:
    """/status：从 checkpointer 读取当前会话状态。"""
    snap = graph.get_state(gconfig)
    values = snap.values or {}
    console.print(f"[bold]会话线程:[/bold] {gconfig['configurable']['thread_id']}")
    console.print(f"[bold]技术主题:[/bold] {values.get('tech') or '（未设置）'}")
    console.print(f"[bold]收集到的链接:[/bold] {len(values.get('urls') or [])}")
    console.print(f"[bold]已解读文档:[/bold] {len(values.get('visited') or [])}")
    reports = [n for n in (values.get("notes") or []) if n.get("report")]
    console.print(f"[bold]已解读 report:[/bold] {len(reports)}  "
                  f"[bold]note 已处理:[/bold] {values.get('noted_count') or 0}")
    if snap.next:
        console.print(f"[bold]下一步:[/bold] {snap.next}")


def _handle_read_graph(graph, gconfig, url) -> None:
    """/learn 里的 read：RAG 缓存命中时询问是否复用，否则走图解读。"""
    if _try_reuse_cached_report(url, graph=graph, gconfig=gconfig):
        return
    _drive(graph, gconfig, {"command": "read", "args": [url]}, render="markdown")


def _maybe_guide_collect(graph, gconfig, final) -> None:
    """/ask 无命中引导：笔记库暂无相关内容 → 询问是否先 collect 某技术（回车跳过）。

    复用 _handle_read_graph 的交互模式（CLI 层 input() + 再驱动命令），不需要 interrupt。
    """
    hist = (final or {}).get("qa_history") or []
    if not hist or not hist[-1].get("no_hit"):
        return
    try:
        tech = input("笔记库暂无相关内容，要针对哪个技术先 collect？（回车跳过）> ").strip()
    except (EOFError, KeyboardInterrupt):
        tech = ""
    if tech:
        # 与 /learn 里 `collect <技术名>` 一致：直接驱动 collect
        _drive(graph, gconfig, {"command": "collect", "tech": tech})


@cli.command()
@click.argument("session_id", required=False)
def learn(session_id: str | None = None):
    """进入交互式学习会话（/learn REPL，LangGraph 图驱动）。

    SESSION_ID: 可选，恢复指定会话（对应 checkpointer 的 thread_id）；不传则新建
    """
    from .graph import open_graph

    # 会话 ID = LangGraph thread_id（SqliteSaver 的持久化游标）
    if session_id:
        thread_id = session_id
        console.print(f"[green]已恢复会话:[/green] {session_id}")
    else:
        thread_id = f"learn-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
        console.print(f"[green]已创建新会话:[/green] {thread_id}")

    console.print("[dim]进入学习会话（LangGraph 图驱动）。输入命令开始，/help 查看帮助，/quit 退出。[/dim]")

    with open_graph() as graph:
        gconfig = {"configurable": {"thread_id": thread_id}}

        # 恢复旧线程：已有状态则先展示
        if graph.get_state(gconfig).values:
            console.print("[dim]—— 上次会话状态 ——[/dim]")
            _print_graph_status(graph, gconfig)
        elif session_id:
            # 传入的会话 ID 无任何状态 → 警告，避免静默新建空会话（线程 ID 格式 learn-YYYYMMDD-HHMMSS，
            # 少掉 learn- 前缀会进到全新空线程，note 会报"还没有技术主题"）
            console.print(f"[yellow]⚠ 会话ID '{session_id}' 不存在或为空，已新建空会话。[/yellow]")
            console.print("[dim]若想恢复旧会话，请确认 ID 完整（含 learn- 前缀），如 learn learn-20260810-122828[/dim]")

        while True:
            try:
                line = input(f"{thread_id} > ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]退出会话（状态已由 checkpointer 保存）[/dim]")
                break
            if not line:
                continue

            # 斜杠命令
            if line.startswith("/"):
                verb = line[1:].strip().split()
                cmd = verb[0] if verb else ""
                if cmd in ("quit", "exit"):
                    console.print("[dim]会话已保存（checkpointer），退出。[/dim]")
                    break
                elif cmd == "status":
                    _print_graph_status(graph, gconfig)
                elif cmd == "ask":
                    # 联想检索笔记库并综合回答：不依赖会话技术主题，可直接执行
                    _, _, question = line[1:].strip().partition(" ")
                    parsed = parse_card_input("ask", [question])
                    if parsed.get("error"):
                        console.print("[yellow]用法: /ask <问题>（联想检索笔记库并综合回答）[/yellow]")
                        continue
                    final = _drive(graph, gconfig, parsed, render="markdown")
                    _maybe_guide_collect(graph, gconfig, final)
                elif cmd == "done":
                    _drive(graph, gconfig, {"command": "note"})
                    console.print("[dim]已沉淀并保存，退出。[/dim]")
                    break
                elif cmd == "help":
                    console.print(_HELP_TEXT)
                else:
                    console.print(f"[yellow]未知命令: /{cmd}（输入 /help 查看）[/yellow]")
                continue

            # 普通命令路由（shlex 解析，支持引号包裹带空格的技术名）
            try:
                parts = shlex.split(line)
            except ValueError:
                console.print("[yellow]⚠ 引号未闭合，无法解析[/yellow]")
                continue
            if not parts:
                continue
            op = parts[0].lower()
            if op == "collect":
                parsed = parse_card_input("collect", parts[1:])
                if parsed.get("error"):
                    console.print(f"[yellow]{parsed['error']}[/yellow]")
                    continue
                _drive(graph, gconfig, parsed)
            elif op == "read":
                parsed = parse_card_input("read", parts[1:])
                if parsed.get("error"):
                    console.print(f"[yellow]{parsed['error']}[/yellow]")
                    continue
                _handle_read_graph(graph, gconfig, parsed["args"][0])
            elif op == "note":
                _drive(graph, gconfig, {"command": "note"})
            else:
                console.print(f"[yellow]未知命令: {op}（输入 /help 查看）[/yellow]")


if __name__ == "__main__":
    cli()
