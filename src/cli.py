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
from .pipelines.collect import collect_pipeline, dig_pipeline
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
@click.argument("tech_name")
@click.argument("level", required=False, type=click.Choice(["入门", "进阶"], case_sensitive=False))
def collect(tech_name: str, level: str = None):
    """收集指定技术的学习资料（全面学习，按级别）。

    TECH_NAME: 技术名称，如 "Spring Boot 3"、"FastAPI"（含空格需加引号）
    LEVEL: 可选，入门 或 进阶，默认入门
    """
    level = level or "入门"
    console.print(Panel(f"📚 开始收集「{tech_name}」的学习资料（{level}级）...", style="bold blue"))

    result = collect_pipeline(tech_name, level, progress=lambda m: console.print(m))
    console.print(f"✅ 共收集到 [bold]{len(result['urls'])}[/bold] 条去重资源")
    console.print(f"├  保存报告: [bold]{result['materials_path']}[/bold]")
    console.print(Panel(Markdown(result["report"][:3000]), title="✅ 资料收集完成", style="green"))


@cli.command()
@click.argument("tech_name")
@click.argument("direction", nargs=-1, required=True)
def dig(tech_name: str, direction: tuple):
    """深挖指定技术的具体方向。

    TECH_NAME: 技术名称，如 "Spring Boot"（含空格需加引号）
    DIRECTION: 具体方向，如 "注解原理"、"底层框架"（多词自动拼接）
    """
    direction_text = " ".join(direction)
    console.print(Panel(f"🔍 开始深挖「{tech_name}」的「{direction_text}」...", style="bold blue"))

    result = dig_pipeline(tech_name, direction_text, progress=lambda m: console.print(m))
    console.print(f"✅ 共收集到 [bold]{len(result['urls'])}[/bold] 条去重资源")
    console.print(f"├  保存报告: [bold]{result['materials_path']}[/bold]")
    console.print(Panel(Markdown(result["report"][:3000]), title="✅ 资料深挖完成", style="green"))


@cli.command()
@click.argument("url")
def read(url: str):
    """解读指定的技术文档。

    URL: 文档页面链接
    """
    console.print(Panel(f"📖 开始解读文档...", style="bold blue"))
    console.print(f"[dim]{url}[/dim]")

    # RAG 历史召回：该 URL 已有解读则提示复用（失败静默，不影响抓取）
    if _try_reuse_cached_report(url):
        return

    result = read_pipeline(url, progress=lambda m: console.print(m))
    if result.get("error"):
        console.print(f"[red]❌ {result['error']}[/red]")
        return
    console.print(f"├  保存报告: [bold]{result['report_path']}[/bold]")
    console.print(Panel(Markdown(result["report"]), title="✅ 文档解读完成", style="green"))


@cli.command()
@click.argument("tech")
@click.option("--file", "-f", "file_path", help="从本地文件读取学习内容")
@click.option("--text", "-t", "content", help="直接提供学习内容文本")
def note(tech: str, file_path: str = None, content: str = None):
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
    for r in persisted["results"]:
        label = "🆕 新增" if r["action"] == "new" else "🔗 合并"
        console.print(f"  {label} [bold]{r['topic']}[/bold] → knowledge/{r['path']}")

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
    if result["errors"]:
        console.print("[yellow]部分文件索引失败：[/yellow]")
        for e in result["errors"]:
            console.print(f"  [dim]{e}[/dim]")


# ============================================================
# note 交互辅助
# ============================================================

def _find_materials_path(tech: str) -> str | None:
    """查找该技术的 materials 报告路径（collect 生成 `materials/<tech>-materials.md`）。"""
    safe = tech.lower().replace(" ", "-")
    p = config.MATERIALS_DIR / f"{safe}-materials.md"
    return str(p) if p.exists() else None


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
  collect <技术名> [入门|进阶]   收集学习资料（不指定级别时交互式询问）
  dig <技术名> <具体方向>        深挖指定技术的具体方向
  read <url>                      解读技术文档
  note                            将本次会话内容沉淀为知识笔记
  /ask <问题>                    联想检索笔记库并综合回答（不依赖技术主题）
  /status                         查看会话状态
  /done                           沉淀并结束会话
  /quit                           退出（状态已持久化）
  /help                           查看帮助

提示：技术名含空格时用引号包裹，如 collect "Spring Boot"、dig "Claude Code" 底层框架
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
        stream = graph.stream_events(payload, gconfig, version="v3")
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
            console.print(f"\n[bold cyan]🧭 {intr.value}[/bold cyan]")
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
    console.print(f"[bold]技术主题:[/bold] {values.get('tech') or '（未设置）'}  "
                  f"[bold]级别:[/bold] {values.get('level') or '（未设置）'}")
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
        # 与 /learn 里 `collect <技术名>`（不指定级别）一致：ask_level 交互 → collect
        _drive(graph, gconfig, {"command": "ask_level", "tech": tech})


@cli.command()
@click.argument("session_id", required=False)
def learn(session_id: str = None):
    """进入交互式学习会话（/learn REPL，LangGraph 图驱动）。

    SESSION_ID: 可选，恢复指定会话（对应 checkpointer 的 thread_id）；不传则新建
    """
    from .graph import open_graph

    # 会话 ID = LangGraph thread_id（SqliteSaver 的持久化游标）
    if session_id:
        thread_id = session_id
        console.print(f"[green]已恢复会话:[/green] {session_id}")
    else:
        thread_id = f"learn-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
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
                    question = question.strip()
                    if not question:
                        console.print("[yellow]用法: /ask <问题>（联想检索笔记库并综合回答）[/yellow]")
                        continue
                    final = _drive(graph, gconfig, {"command": "qa", "args": [question]},
                                   render="markdown")
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
                if len(parts) < 2:
                    console.print("[yellow]用法: collect <技术名> [入门|进阶][/yellow]")
                    continue
                level = parts[2] if len(parts) > 2 and parts[2] in ("入门", "进阶") else None
                if level:
                    _drive(graph, gconfig, {"command": "collect", "tech": parts[1], "level": level})
                else:
                    # 未指定级别 → 走 ask_level 交互点（interrupt，模块 2 水平探测最小种子）
                    _drive(graph, gconfig, {"command": "ask_level", "tech": parts[1]})
            elif op == "dig":
                if len(parts) < 3:
                    console.print("[yellow]用法: dig <技术名> <具体方向>[/yellow]")
                    continue
                _drive(graph, gconfig, {"command": "dig", "tech": parts[1], "args": parts[2:]})
            elif op == "read":
                if len(parts) < 2:
                    console.print("[yellow]用法: read <url>[/yellow]")
                    continue
                _handle_read_graph(graph, gconfig, parts[1])
            elif op == "note":
                _drive(graph, gconfig, {"command": "note"})
            else:
                console.print(f"[yellow]未知命令: {op}（输入 /help 查看）[/yellow]")


if __name__ == "__main__":
    cli()
