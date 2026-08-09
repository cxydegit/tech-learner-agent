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
from .agent import run_collect, run_read, run_note, run_dig

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
    run_collect(tech_name, level or "入门")


@cli.command()
@click.argument("tech_name")
@click.argument("direction", nargs=-1, required=True)
def dig(tech_name: str, direction: tuple):
    """深挖指定技术的具体方向。

    TECH_NAME: 技术名称，如 "Spring Boot"（含空格需加引号）
    DIRECTION: 具体方向，如 "注解原理"、"底层框架"（多词自动拼接）
    """
    direction_text = " ".join(direction)
    run_dig(tech_name, direction_text)


@cli.command()
@click.argument("url")
def read(url: str):
    """解读指定的技术文档。

    URL: 文档页面链接
    """
    run_read(url)


@cli.command()
@click.argument("tech")
@click.option("--file", "-f", "file_path", help="从本地文件读取学习内容")
@click.option("--text", "-t", "content", help="直接提供学习内容文本")
def note(tech: str, file_path: str = None, content: str = None):
    """将学习内容整理为结构化笔记。

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

    run_note(tech, conversation_log)


@cli.command()
@click.option("--force", "-f", is_flag=True,
              help="忽略变更检测，强制重新切块嵌入（分块器升级 / 切块参数调整后需要）")
def index(force: bool = False):
    """建立 / 增量更新 RAG 语义索引（knowledge/ + materials/ + reports/）。

    使用 DashScope text-embedding-v3 嵌入分块，存入本地 Chroma（.chroma/）。
    已索引且内容未变化的文件会自动跳过，不会重复计费；分块器升级后用 --force
    或直接跑（版本号变更会自动触发全量重切）。
    """
    from .rag import index_documents

    console.print("🧠 [bold cyan]构建 RAG 语义索引...[/bold cyan]")
    result = index_documents(force=force)
    console.print(f"✅ 索引完成：新增 [bold]{result['indexed']}[/bold] 个文件，"
                  f"跳过 [bold]{result['skipped']}[/bold] 个未变化文件")
    if result["errors"]:
        console.print("[yellow]部分文件索引失败：[/yellow]")
        for e in result["errors"]:
            console.print(f"  [dim]{e}[/dim]")


# ============================================================
# /learn 交互式学习会话
# ============================================================

_HELP_TEXT = """可用命令：
  collect <技术名> [入门|进阶]   收集学习资料（不指定级别时交互式询问）
  dig <技术名> <具体方向>        深挖指定技术的具体方向
  read <url>                      解读技术文档
  note                            将本次会话内容沉淀为知识笔记
  /status                         查看会话状态
  /done                           沉淀并结束会话
  /quit                           退出（状态已持久化）
  /help                           查看帮助

提示：技术名含空格时用引号包裹，如 collect "Spring Boot"、dig "Claude Code" 底层框架
"""


def _drive(graph, config, payload, render: str = "plain") -> dict:
    """驱动一次图执行；遇 interrupt 暂停询问，以 Command(resume) 恢复。

    Args:
        graph: build_graph 编译后的状态图
        config: {"configurable": {"thread_id": ...}}
        payload: 命令输入（dict）或恢复用的 Command
        render: 结束时 last_output 的渲染方式（plain / markdown）

    Returns:
        最终状态 dict（含 last_output）
    """
    from langgraph.types import Command

    while True:
        stream = graph.stream_events(payload, config, version="v3")
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


def _print_graph_status(graph, config) -> None:
    """/status：从 checkpointer 读取当前会话状态。"""
    snap = graph.get_state(config)
    values = snap.values or {}
    console.print(f"[bold]会话线程:[/bold] {config['configurable']['thread_id']}")
    console.print(f"[bold]技术主题:[/bold] {values.get('tech') or '（未设置）'}  "
                  f"[bold]级别:[/bold] {values.get('level') or '（未设置）'}")
    console.print(f"[bold]收集到的链接:[/bold] {len(values.get('urls') or [])}")
    console.print(f"[bold]已解读文档:[/bold] {len(values.get('visited') or [])}")
    console.print(f"[bold]解读/沉淀记录:[/bold] {len(values.get('notes') or [])}")
    if snap.next:
        console.print(f"[bold]下一步:[/bold] {snap.next}")


def _handle_read_graph(graph, gconfig, url) -> None:
    """/learn 里的 read：RAG 缓存命中时询问是否复用，否则走图解读。"""
    from .agent import _confirm_reuse

    try:
        from .rag import check_read_cache
        cached = check_read_cache(url)
    except Exception:  # noqa: BLE001 —— RAG 不可用时静默降级
        cached = None
    if cached:
        console.print(Panel(
            f"📌 检测到该 URL 已有解读报告：\n[bold]{cached['path']}[/bold]"
            f"（相似度 {cached['similarity']:.2f}）",
            title="⏭ RAG 缓存命中",
            style="cyan",
        ))
        if _confirm_reuse():
            report_path = config.BASE_DIR / cached["path"]
            if report_path.exists():
                report = report_path.read_text(encoding="utf-8", errors="replace")
            else:
                report = cached.get("content", "")
            # 直接更新图状态（不跑节点），记录该文档已读
            graph.update_state(gconfig, {
                "visited": [url],
                "notes": [{"url": url, "title": cached["path"], "report": report}],
            })
            console.print(Markdown(report))
            return
    _drive(graph, gconfig, {"command": "read", "args": [url]}, render="markdown")


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
        config = {"configurable": {"thread_id": thread_id}}

        # 恢复旧线程：已有状态则先展示
        if graph.get_state(config).values:
            console.print("[dim]—— 上次会话状态 ——[/dim]")
            _print_graph_status(graph, config)

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
                    _print_graph_status(graph, config)
                elif cmd == "done":
                    _drive(graph, config, {"command": "note"})
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
                    _drive(graph, config, {"command": "collect", "tech": parts[1], "level": level})
                else:
                    # 未指定级别 → 走 ask_level 交互点（interrupt，模块 2 水平探测最小种子）
                    _drive(graph, config, {"command": "ask_level", "tech": parts[1]})
            elif op == "dig":
                if len(parts) < 3:
                    console.print("[yellow]用法: dig <技术名> <具体方向>[/yellow]")
                    continue
                _drive(graph, config, {"command": "dig", "tech": parts[1], "args": parts[2:]})
            elif op == "read":
                if len(parts) < 2:
                    console.print("[yellow]用法: read <url>[/yellow]")
                    continue
                _handle_read_graph(graph, config, parts[1])
            elif op == "note":
                _drive(graph, config, {"command": "note"})
            else:
                console.print(f"[yellow]未知命令: {op}（输入 /help 查看）[/yellow]")


if __name__ == "__main__":
    cli()