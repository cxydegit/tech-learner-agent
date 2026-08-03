"""CLI 命令行入口"""

import shlex
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console

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


# ============================================================
# /learn 交互式学习会话
# ============================================================

_HELP_TEXT = """可用命令：
  collect <技术名> [入门|进阶]   收集学习资料（级别默认入门）
  dig <技术名> <具体方向>        深挖指定技术的具体方向
  read <url>                      解读技术文档
  note                            将本次会话内容沉淀为知识笔记
  /status                         查看会话状态
  /done                           沉淀并结束会话
  /quit                           保存并退出
  /help                           查看帮助

提示：技术名含空格时用引号包裹，如 collect "Spring Boot"、dig "Claude Code" 底层框架
"""


def _print_status(session) -> None:
    console.print(f"[bold]会话:[/bold] {session.session_id}")
    console.print(f"[bold]技术主题:[/bold] {session.tech or '（未设置）'}  [bold]级别:[/bold] {session.level}")
    console.print(f"[bold]收集到的链接:[/bold] {len(session.urls)}")
    console.print(f"[bold]已解读文档:[/bold] {len(session.visited)}")
    console.print(f"[bold]资料清单:[/bold] {session.materials_path or '（无）'}")
    console.print(f"[bold]本次沉淀笔记:[/bold] {len(session.notes)} 条")
    if session.history:
        console.print("[bold]交互记录:[/bold]")
        for h in session.history:
            console.print(f"  [{h['time']}] {h['action']}: {h['detail']}")


def _do_note(session, run_note) -> None:
    """把本次会话读到的文档内容汇总，交给 run_note 沉淀。"""
    if not session.tech:
        console.print("[yellow]⚠ 会话还没有技术主题，先 collect <技术名>[/yellow]")
        return
    parts = []
    for n in session.notes:
        if n.get("report"):
            parts.append(f"来源：{n.get('url')}\n{n['report']}")
    content = "\n\n".join(parts)
    if not content.strip():
        console.print("[yellow]⚠ 没有可沉淀的内容，先 read 一些文档[/yellow]")
        return
    run_note(session.tech, content, session=session)


@cli.command()
@click.argument("session_id", required=False)
def learn(session_id: str = None):
    """进入交互式学习会话（/learn REPL）。

    SESSION_ID: 可选，恢复指定会话；不传则新建会话
    """
    from .session import LearnSession

    # 恢复或新建会话
    if session_id:
        try:
            session = LearnSession.load(session_id)
            console.print(f"[green]已恢复会话:[/green] {session_id}")
        except FileNotFoundError:
            console.print(f"[red]会话不存在: {session_id}[/red]")
            return
    else:
        session_id = f"learn-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        session = LearnSession(session_id=session_id)
        console.print(f"[green]已创建新会话:[/green] {session_id}")

    console.print("[dim]进入学习会话。输入命令开始，/help 查看帮助，/quit 退出。[/dim]")

    while True:
        try:
            line = input(f"{session.tech or session.session_id} > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]退出会话[/dim]")
            session.save()
            break
        if not line:
            continue

        # 斜杠命令
        if line.startswith("/"):
            verb = line[1:].strip().split()
            cmd = verb[0] if verb else ""
            if cmd in ("quit", "exit"):
                session.save()
                console.print("[dim]会话已保存，退出。[/dim]")
                break
            elif cmd == "status":
                _print_status(session)
            elif cmd == "done":
                _do_note(session, run_note)
                session.save()
                console.print("[dim]会话已沉淀并保存，退出。[/dim]")
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
            level = parts[2] if len(parts) > 2 and parts[2] in ("入门", "进阶") else "入门"
            run_collect(parts[1], level, session=session)
        elif op == "dig":
            if len(parts) < 3:
                console.print("[yellow]用法: dig <技术名> <具体方向>[/yellow]")
                continue
            run_dig(parts[1], " ".join(parts[2:]), session=session)
        elif op == "read":
            if len(parts) < 2:
                console.print("[yellow]用法: read <url>[/yellow]")
                continue
            run_read(parts[1], session=session)
        elif op == "note":
            _do_note(session, run_note)
        else:
            console.print(f"[yellow]未知命令: {op}（输入 /help 查看）[/yellow]")


if __name__ == "__main__":
    cli()