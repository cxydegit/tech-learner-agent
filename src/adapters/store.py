"""文件存储 + 笔记沉淀基础设施（含读写工具）。

自 storage.py 迁出（去重/命名纯规则在 domain/dedup.py）+ tools.py 迁出
save_file_tool / read_file_tool / list_files_tool。

⚠️ vector 导入必须保持函数内 lazy（不变量 I1：`import src.cli` 不得拉起 chromadb）。
"""

import re
import time
from datetime import datetime
from pathlib import Path

from ..config import config
from ..domain.dedup import _parse_tags, _title_fast_match, _with_header, sanitize_filename


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


def find_note_match(tech: str, topic: str, existing: list[dict],
                    *, content: str | None = None, tags: list[str] | None = None
                    ) -> tuple[dict | None, float | None, str | None]:
    """语义去重：召回候选 → 标题 fast-path → LLM 判定，返回第一个判定 same 的候选。

    设计（RAG_OPTIMIZATION P0 压力测试后重构）：候选召回与合并决策解耦——
    - 候选召回：语义检索 top2（限定同一技术领域）；相似度低于
      RAG_DEDUP_JUDGE_SIM_MIN 的不送判定（实测同义改写对源笔记 ≥0.50，0.4 以下
      几乎不可能是同一篇，省 LLM 调用）；
    - 标题 fast-path：domain.dedup._title_fast_match（去停用词后词元完全相等）→
      直接确认，不花 LLM 调用；
    - LLM 判定：其余候选交给 adapters/llm.judge_same_knowledge_point（替代旧的
      标签/内容 overlap 确认层——合成压力测试证明确定性信号无法识别真正措辞不同
      的同义改写，见 RAG_OPTIMIZATION）；
    - 判定失败 / LLM 不可用 → 不合并（宁可可修的重复，也不静默错并）。

    返回**第一个**判定 same 的候选（top1 优先；fast-path 优先于 LLM 判定），
    判定理由随候选返回，供 merge_candidates 展示（用户确认时看到为什么建议合并）。

    Args:
        tech: 技术名称（原始大小写，如 "FastAPI"）
        topic: 新知识点标题
        existing: 该技术领域已有的笔记列表（见 get_existing_notes，含 tags/content）
        content: 可选，新知识点正文（LLM 判定上下文）
        tags: 可选，新知识点标签（LLM 判定上下文）

    Returns:
        (命中的已有笔记 dict, 相似度 float | None, 判定理由 str | None)；
        未命中返回 (None, None, None)
    """
    try:
        from .vector import semantic_search_knowledge
        hits = semantic_search_knowledge(topic, top_k=2, tech=sanitize_filename(tech))
    except Exception:  # noqa: BLE001 —— RAG 不可用时 hits 为空，走标题 fast-path 全量扫描兜底
        hits = []
    sim_floor = config.RAG_DEDUP_JUDGE_SIM_MIN
    for h in hits:
        sim = h.get("similarity", 0)
        if sim < sim_floor:
            continue
        # RAG 索引路径相对 BASE_DIR（knowledge/rag/xxx.md），
        # 而 existing 路径相对 KNOWLEDGE_DIR（rag/xxx.md），归一化后比较
        hit_path = h.get("path") or ""
        if hit_path.startswith("knowledge/"):
            hit_path = hit_path[len("knowledge/"):]
        existing_note = next((n for n in existing if n["path"] == hit_path), None)
        if not existing_note:
            continue
        # ① 标题 fast-path：标题基本同一句 → 直接确认，不花 LLM 调用
        if _title_fast_match(topic, existing_note["topic"]):
            return existing_note, sim, "标题等同"
        # ② LLM 判定（异常静默降级为不合并，安全侧）
        try:
            from .llm import judge_same_knowledge_point
            verdict, reason = judge_same_knowledge_point(topic, tags, content, existing_note)
            if verdict == "same":
                return existing_note, sim, reason or "LLM 判定为同一知识点"
        except Exception:  # noqa: BLE001
            pass
    # RAG 不可用时的兜底：标题 fast-path 全量扫描（零 embedding）
    for n in existing:
        if _title_fast_match(topic, n["topic"]):
            return n, None, "标题等同（RAG 不可用回退）"
    return None, None, None


def recall_existing_notes(tech: str, query: str, top_k: int = 3) -> list[dict]:
    """语义召回该技术领域下与学习内容最相关的已有笔记 top-k（差量提取的对比上下文）。

    复用 semantic_search_knowledge 限定 knowledge 源 + tech 目录（lazy import，守 I1）；
    RAG 未索引 / 不可用时回退到最近 top_k 篇，保证提取提示词至少有一点对比对象。

    Args:
        tech: 技术名称（原始大小写，如 "FastAPI"）
        query: 语义检索查询文本（本轮学习内容截断）
        top_k: 返回条数

    Returns:
        [{"path", "topic", "date", "content", "similarity"}, ...]
        无笔记时返回 []；content 来自 get_existing_notes（前 2000 字，供提取提示词自行截断）。
    """
    all_notes = get_existing_notes(tech)
    if not all_notes:
        return []
    try:
        from .vector import semantic_search_knowledge
        hits = semantic_search_knowledge(query, top_k=top_k, tech=sanitize_filename(tech))
    except Exception:  # noqa: BLE001 —— RAG 不可用时回退
        hits = []

    by_path = {n["path"]: n for n in all_notes}
    recalled: list[dict] = []
    for h in hits:
        path = h.get("path") or ""
        if path.startswith("knowledge/"):
            path = path[len("knowledge/"):]
        n = by_path.get(path)
        if n and n not in recalled:
            recalled.append({**n, "similarity": h.get("similarity", 0)})
    if not recalled:
        # 回退：最近 top_k 篇（RAG 未索引 / 空索引）
        recalled = [{**n, "similarity": None} for n in all_notes[-top_k:]]
    return recalled


def read_knowledge_note(rel_path: str) -> str:
    """读取知识库笔记全文（合并候选的 old_content 用，get_existing_notes 只截了前 2000 字）。"""
    p = config.KNOWLEDGE_DIR / rel_path
    return p.read_text(encoding="utf-8") if p.exists() else ""


# 索引瞬时失败的重试间隔（秒）：限流 / 网络抖动类失败立即重试一次即可兜住
_INDEX_RETRY_DELAY_SECONDS = 1.5


def _update_rag_index(filepath: Path) -> dict:
    """笔记写库后增量更新 RAG 索引：失败不阻断沉淀，但状态必须返回给调用方呈现。

    P3.1（8-19 事故复盘）：此前失败被 `except: pass` 静默吞掉——embedding API
    一次抖动让 4 篇新笔记「保存成功但检索不到」，无日志无重试、对账只删不补，
    缺口留存 6 天才被评估撞见。现在：瞬时失败立即重试一次（失败的调用不产生
    embedding 计费）；仍失败则返回失败状态，由接口层（cli/graph/web）渲染警告。

    Returns:
        {"index_ok": True} 或 {"index_ok": False, "index_error": 原因}
    """
    last_err: str | None = None
    for attempt in (1, 2):
        try:
            from .vector import index_paths
            result = index_paths([filepath])
            # index_paths 对单文件失败不抛异常而是记进 errors 列表——必须显式检查
            if result.get("errors"):
                last_err = "; ".join(result["errors"])
            else:
                return {"index_ok": True}
        except Exception as e:  # noqa: BLE001 —— 索引失败不应阻断沉淀
            last_err = f"{type(e).__name__}: {e}"
        if attempt == 1:
            time.sleep(_INDEX_RETRY_DELAY_SECONDS)
    return {"index_ok": False, "index_error": last_err}


def persist_note(tech: str, topic: str, content: str, tags: list[str] | None = None,
                 *, replace_path: str | None = None) -> dict:
    """持久化一条知识笔记：新建 dated 文件，或覆盖合并进已有文件。

    Step 3 起**不再静默追加**：去重/合并决策上移到 note_pipeline + 交互层
    （pipelines/note.py），此处只做纯 I/O——
    - replace_path 为 None：创建新 dated 文件并更新 INDEX.md；
    - replace_path 给出：把 content（交互层已用 LLM 差量合并好的正文）覆盖写入该文件，
      保留原始日期，标签与既有标签合并去重。

    Args:
        tech: 技术名称（如 "spring-boot"）
        topic: 知识点主题（如 "依赖注入"）
        content: Markdown 笔记正文（合并场景为合并后的完整正文，不含头部）
        tags: 标签列表
        replace_path: 可选，要覆盖合并的已有笔记相对路径（knowledge/ 下）

    Returns:
        {"action": "new"|"merged", "path": str（相对 knowledge/）, "topic": str,
         "index_ok": bool, "index_error": str|None（仅失败时）}
        索引失败不代表写盘失败——笔记已在磁盘，缺口由对账补缺失自愈。
    """
    ensure_knowledge_base()
    tech_dir = config.KNOWLEDGE_DIR / sanitize_filename(tech)
    tech_dir.mkdir(parents=True, exist_ok=True)

    if replace_path:
        filepath = config.KNOWLEDGE_DIR / replace_path
        if filepath.exists():
            old = filepath.read_text(encoding="utf-8")
            old_date, old_tags = _parse_header(old)
            date_str = old_date or datetime.now().strftime("%Y-%m-%d")
            tag_str = " ".join(f"#{t}" for t in _merge_tags(old_tags, tags))
            header = f"# {topic}\n\n> 日期：{date_str}\n> 标签：{tag_str}\n\n"
            filepath.write_text(header + content.lstrip(), encoding="utf-8")
            result = {"action": "merged", "path": replace_path, "topic": topic}
            result.update(_update_rag_index(filepath))
            return result

    # 新建：dated 文件 + 更新索引
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{sanitize_filename(topic)}.md"
    filepath = tech_dir / filename
    filepath.write_text(_with_header(topic, tags, content), encoding="utf-8")
    update_index(tech, topic, filepath)
    result = {"action": "new", "path": filepath.relative_to(config.KNOWLEDGE_DIR).as_posix(), "topic": topic}
    result.update(_update_rag_index(filepath))
    return result


def _parse_header(content: str) -> tuple[str | None, list[str]]:
    """从笔记文件头部解析日期和标签（形如 `> 日期：2026-08-09` / `> 标签：#a #b`）。

    标签解析复用 domain.dedup._parse_tags（同一纯规则，供去重确认层使用）。

    Returns:
        (日期字符串, 标签列表)；缺省项为 (None, [])
    """
    date_str: str | None = None
    for line in content.splitlines()[:5]:
        m = re.search(r">\s*日期[：:]\s*(\S+)", line)
        if m:
            date_str = m.group(1).strip()
    return date_str, _parse_tags(content)


def _merge_tags(old: list[str], new: list[str] | None) -> list[str]:
    """合并新旧标签，去重保序。"""
    out: list[str] = []
    for t in list(old) + list(new or []):
        if t and t not in out:
            out.append(t)
    return out


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

        text = f.read_text(encoding="utf-8")
        notes.append({
            "path": f.relative_to(config.KNOWLEDGE_DIR).as_posix(),  # 统一 POSIX 分隔符，与 RAG 索引一致
            "topic": topic,
            "date": date_str,
            "content": text[:2000],  # 只取前 2000 字符用于去重/内容确认（判别性概念多在文首）
            "tags": _parse_tags(text),  # 供 LLM 去重判定（judge_same_knowledge_point）作上下文
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
