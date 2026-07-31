"""知识库存储管理：笔记读写、索引维护"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import config


def ensure_knowledge_base() -> None:
    """确保知识库目录和索引文件存在。"""
    config.KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    index_path = config.KNOWLEDGE_DIR / "INDEX.md"
    if not index_path.exists():
        index_path.write_text(
            "# 知识库索引\n\n"
            "> 自动生成，请勿手动编辑\n\n"
            "## 技术领域\n\n",
            encoding="utf-8",
        )


def sanitize_filename(name: str) -> str:
    """将技术名/主题名转换为安全的文件名。"""
    return re.sub(r"[^\w\-]", "-", name.strip()).strip("-").lower()


def save_note(tech: str, topic: str, content: str) -> Path:
    """保存一篇知识笔记。

    Args:
        tech: 技术名称（如 "spring-boot"）
        topic: 知识点主题（如 "依赖注入"）
        content: Markdown 格式的笔记内容

    Returns:
        保存的文件路径
    """
    ensure_knowledge_base()

    tech_dir = config.KNOWLEDGE_DIR / sanitize_filename(tech)
    tech_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{sanitize_filename(topic)}.md"
    filepath = tech_dir / filename

    # 如果文件已存在，追加内容
    if filepath.exists():
        existing = filepath.read_text(encoding="utf-8")
        content = existing + "\n\n---\n\n## 补充（更新于 {date_str}）\n\n".format(date_str=date_str) + content

    filepath.write_text(content, encoding="utf-8")
    return filepath


def update_index(tech: str, topic: str, filepath: Path) -> None:
    """更新知识库索引文件。

    Args:
        tech: 技术名称
        topic: 知识点主题
        filepath: 笔记文件路径
    """
    ensure_knowledge_base()
    index_path = config.KNOWLEDGE_DIR / "INDEX.md"

    content = index_path.read_text(encoding="utf-8")
    tech_display = tech.replace("-", " ").title()
    rel_path = filepath.relative_to(config.KNOWLEDGE_DIR)

    # 构建新条目
    new_entry = f"- [{topic}]({rel_path.as_posix()}) — {datetime.now().strftime('%Y-%m-%d')}"

    # 检查技术领域是否已存在
    tech_header = f"### {tech_display}"
    if tech_header in content:
        # 在现有技术领域下添加条目
        lines = content.split("\n")
        new_lines = []
        inserted = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            if line.strip() == tech_header and not inserted:
                # 检查是否已存在相同主题的条目
                if new_entry not in content:
                    new_lines.append(new_entry)
                inserted = True
        content = "\n".join(new_lines)
    else:
        # 添加新技术领域
        content += f"\n{tech_header}\n{new_entry}\n"

    index_path.write_text(content, encoding="utf-8")


def get_existing_notes(tech: str) -> list[dict]:
    """获取指定技术领域的所有已有笔记。

    Args:
        tech: 技术名称

    Returns:
        [{"path": str, "topic": str, "date": str, "content": str}, ...]
    """
    tech_dir = config.KNOWLEDGE_DIR / sanitize_filename(tech)
    if not tech_dir.exists():
        return []

    notes = []
    for f in sorted(tech_dir.glob("*.md")):
        name = f.stem
        # 解析日期和主题：YYYY-MM-DD-topic
        parts = name.split("-", 3)
        if len(parts) >= 4:
            date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"
            topic = parts[3].replace("-", " ")
        else:
            date_str = "unknown"
            topic = name

        notes.append({
            "path": str(f.relative_to(config.KNOWLEDGE_DIR)),
            "topic": topic,
            "date": date_str,
            "content": f.read_text(encoding="utf-8")[:2000],  # 只取前 2000 字符用于去重
        })
    return notes


def get_knowledge_summary() -> str:
    """获取知识库概览，用于 Agent 了解现有知识结构。"""
    ensure_knowledge_base()

    summary = ["## 知识库概览\n"]
    for tech_dir in sorted(config.KNOWLEDGE_DIR.iterdir()):
        if tech_dir.is_dir():
            notes = list(tech_dir.glob("*.md"))
            if notes:
                summary.append(f"- **{tech_dir.name}**: {len(notes)} 篇笔记")
                for n in sorted(notes):
                    summary.append(f"  - {n.stem}")
    return "\n".join(summary) if len(summary) > 1 else "知识库为空"