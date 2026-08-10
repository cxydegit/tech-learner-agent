"""文件存储 + 笔记沉淀基础设施（含读写工具）。

自 storage.py 迁出（去重/命名纯规则在 domain/dedup.py）+ tools.py 迁出
save_file_tool / read_file_tool / list_files_tool。

⚠️ vector 导入必须保持函数内 lazy（不变量 I1：`import src.cli` 不得拉起 chromadb）。
"""

from datetime import datetime
from pathlib import Path

from ..config import config
from ..domain.dedup import _topics_overlap, _with_header, sanitize_filename


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


def _find_dedup_match(tech: str, topic: str, existing: list[dict]) -> dict | None:
    """语义去重：先召回同一领域 top1，相似度达标或主题重叠即视为同一知识点。

    设计取舍（替代纯字符串）：
    - 语义召回 top1 是主信号：相似度 >= RAG_DEDUP_THRESHOLD 即合并，
      能抓住"字面不同但语义相同"的改写（纯字符串 overlap 抓不到）。
    - 轻量 overlap 作为辅助确认：相似度未达阈值但主题高度重叠时也可合并。
    - 两者都不中则回退到纯字符串匹配（覆盖 RAG 未索引 / 不可用场景）。

    Args:
        tech: 技术名称（原始大小写，如 "FastAPI"）
        topic: 新知识点标题
        existing: 该技术领域已有的笔记列表（见 get_existing_notes）

    Returns:
        命中的已有笔记 dict（{"path", "topic", ...}），否则 None
    """
    try:
        from .vector import semantic_search_knowledge
        # 限定同一技术领域，避免跨领域误合并
        hits = semantic_search_knowledge(topic, top_k=1, tech=sanitize_filename(tech))
        if hits:
            h = hits[0]
            hit_topic = h.get("topic") or ""
            # RAG 索引路径相对 BASE_DIR（knowledge/rag/xxx.md），
            # 而 existing 路径相对 KNOWLEDGE_DIR（rag/xxx.md），归一化后比较
            hit_path = h.get("path") or ""
            if hit_path.startswith("knowledge/"):
                hit_path = hit_path[len("knowledge/"):]
            strong = h.get("similarity", 0) >= config.RAG_DEDUP_THRESHOLD
            overlap = _topics_overlap(topic, hit_topic or hit_path)
            if (strong or overlap) and hit_path:
                match = next((n for n in existing if n["path"] == hit_path), None)
                if match:
                    return match
    except Exception:  # noqa: BLE001 —— RAG 不可用时回退到字符串匹配
        pass
    return next((n for n in existing if _topics_overlap(n["topic"], topic)), None)


def _update_rag_index(filepath: Path) -> None:
    """笔记写库后增量更新 RAG 索引（失败静默，不影响主流程）。"""
    try:
        from .vector import index_paths
        index_paths([filepath])
    except Exception:  # noqa: BLE001 —— 索引失败不应阻断沉淀
        pass


def persist_note(tech: str, topic: str, content: str, tags: list[str] | None = None) -> dict:
    """持久化一条知识笔记，自动去重/合并。

    若知识库中已有语义相近的主题，则作为"补充"合并到已有笔记文件；
    否则创建新的 dated 笔记文件并更新索引。写入后增量更新 RAG 索引。

    Args:
        tech: 技术名称（如 "spring-boot"）
        topic: 知识点主题（如 "依赖注入"）
        content: Markdown 格式的笔记正文
        tags: 标签列表

    Returns:
        {"action": "new"|"merged", "path": str（相对 knowledge/）, "topic": str}
    """
    ensure_knowledge_base()
    tech_dir = config.KNOWLEDGE_DIR / sanitize_filename(tech)
    tech_dir.mkdir(parents=True, exist_ok=True)

    # 去重：先语义召回 + overlap 确认，回退到纯字符串匹配
    existing = get_existing_notes(tech)
    match = _find_dedup_match(tech, topic, existing)

    if match:
        # 合并：追加"补充"章节到已有文件
        existing_path = config.KNOWLEDGE_DIR / match["path"]
        _append_section(existing_path, content)
        _update_rag_index(existing_path)
        return {"action": "merged", "path": match["path"], "topic": match["topic"]}

    # 新建：dated 文件 + 更新索引
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{sanitize_filename(topic)}.md"
    filepath = tech_dir / filename
    filepath.write_text(_with_header(topic, tags, content), encoding="utf-8")
    update_index(tech, topic, filepath)
    _update_rag_index(filepath)
    return {"action": "new", "path": filepath.relative_to(config.KNOWLEDGE_DIR).as_posix(), "topic": topic}


def _append_section(existing_path: Path, content: str) -> None:
    """在已有笔记文件后追加"补充"章节。"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    existing = existing_path.read_text(encoding="utf-8")
    combined = existing.rstrip() + f"\n\n---\n\n## 补充（更新于 {date_str}）\n\n" + content
    existing_path.write_text(combined, encoding="utf-8")


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
            "path": f.relative_to(config.KNOWLEDGE_DIR).as_posix(),  # 统一 POSIX 分隔符，与 RAG 索引一致
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


# ============================================================
# 文件读写工具（自 tools.py 迁出，供 ReAct 基线 / 管道保存用）
# ============================================================

def save_file_tool(path: str, content: str) -> dict:
    """保存内容到本地文件。

    Args:
        path: 相对于项目根目录的文件路径
        content: 文件内容

    Returns:
        dict: {"path": str, "size": int, "success": bool}
    """
    full_path = config.BASE_DIR / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {
        "path": str(full_path),
        "size": len(content),
        "success": True,
    }


def read_file_tool(path: str) -> dict:
    """读取本地文件内容。

    Args:
        path: 相对于项目根目录的文件路径

    Returns:
        dict: {"path": str, "content": str, "success": bool, "error": str}
    """
    full_path = config.BASE_DIR / path
    if not full_path.exists():
        return {"path": str(full_path), "content": "", "success": False, "error": "文件不存在"}
    content = full_path.read_text(encoding="utf-8")
    return {"path": str(full_path), "content": content, "success": True, "error": ""}


def list_files_tool(directory: str = ".") -> dict:
    """列出目录下的文件结构。

    Args:
        directory: 相对于项目根目录的目录路径

    Returns:
        dict: {"files": [{path, size, is_dir}, ...], "total": int}
    """
    full_dir = config.BASE_DIR / directory
    if not full_dir.exists():
        return {"files": [], "total": 0, "error": f"目录不存在: {directory}"}

    files = []
    for p in sorted(full_dir.rglob("*")):
        rel_path = str(p.relative_to(config.BASE_DIR))
        files.append({
            "path": rel_path,
            "size": p.stat().st_size if p.is_file() else 0,
            "is_dir": p.is_dir(),
        })
    return {"files": files, "total": len(files)}
