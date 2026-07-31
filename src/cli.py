"""CLI 命令行入口"""

import sys
from pathlib import Path

import click
from rich.console import Console

from .config import config
from .agent import run_collect, run_read, run_note

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
def collect(tech_name: str):
    """收集指定技术的学习资料。

    TECH_NAME: 要学习的技术名称，如 "Spring Boot 3"、"FastAPI"
    """
    run_collect(tech_name)


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