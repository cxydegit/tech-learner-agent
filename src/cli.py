"""CLI 命令行入口"""

import sys
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


if __name__ == "__main__":
    cli()