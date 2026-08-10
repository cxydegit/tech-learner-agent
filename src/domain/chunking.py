"""Markdown 感知分块（纯领域逻辑，零 I/O、零框架依赖）。

自 rag.py 迁出：Chunk, CHUNKER_VERSION, chunk_text, _FENCE_RE/_HEADING_RE,
_scan_blocks, chunk_markdown, _content_digest。

⚠️ 改动分块逻辑时必须递增 CHUNKER_VERSION：版本提升 → 既有 content_hash
全部失配 → 首次 index 自动全量重切（无需手动清库）。
"""

from __future__ import annotations

import re
from collections import namedtuple
from hashlib import sha1

from ..config import config

# 分块器版本号：改分块逻辑时递增，作为 content_hash 前缀。
# 版本提升 → 既有 hash 全部失配 → 首次 index 自动全量重切（无需手动清库）。
CHUNKER_VERSION = 2

# chunk_markdown 的返回值：text 用于嵌入，section 是标题路径字符串（存入 Chroma 元数据便于调试）
Chunk = namedtuple("Chunk", ["text", "section"])


def chunk_text(text: str, chunk_size: int = 0, overlap: int = 0) -> list[str]:
    """把 Markdown 文本切成带重叠的分块。

    以段落（空行分隔）为单位聚合成块，每块不超过 chunk_size 字符；
    块与块之间保留 overlap 字符的重叠，避免语义断裂。

    Args:
        text: 文档全文
        chunk_size: 单块最大字符数；0 用 config.RAG_CHUNK_SIZE
        overlap: 相邻块重叠字符数；0 用 config.RAG_CHUNK_OVERLAP

    Returns:
        list[str]: 分块列表（保留原文本顺序）
    """
    chunk_size = chunk_size or config.RAG_CHUNK_SIZE
    overlap = overlap or config.RAG_CHUNK_OVERLAP

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        # 单个段落超长：按字符硬切（保留 overlap）
        if len(p) > chunk_size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(p), chunk_size - overlap):
                chunks.append(p[i : i + chunk_size])
            continue
        # 当前块放不下该段落：先收走，下一块从尾部 overlap 续接
        if buf and len(buf) + len(p) + 1 > chunk_size:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        buf = (buf + "\n\n" + p).strip() if buf else p
    if buf:
        chunks.append(buf)
    return chunks


# ============================================================
# Markdown 感知分块（chunker v2）
# ============================================================

_FENCE_RE = re.compile(r"^(```+|~~~+)")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")


def _scan_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """把归一化后的行序列扫描成块（单遍，按序分类）。

    块类型：
    - ("fence",  [行])  代码围栏，从起始围栏到闭合行，内部不解析（**原子**）
    - ("heading",[行])  单个标题行
    - ("table",  [行])  连续以 | 开头的表格行（**原子**）
    - ("normal", [行])  引用块 / 段落 / 列表（连续非空行，非原子，可安全切分）

    边界约定（见 RISKS.md 决策）：表格后紧跟代码围栏 → 表格止于首个非 | 行，
    围栏另起一块，各自完整；连续标题各自成块；整篇只有一张大表 → 单个原子块，绝不撕碎。
    """
    blocks: list[tuple[str, list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        # 代码围栏：直到匹配闭合行（同一围栏字符且长度 >= 起始）
        fm = _FENCE_RE.match(line)
        if fm:
            fence_char = fm.group(1)[0]
            fence_len = len(fm.group(1))
            j = i + 1
            while j < n:
                cm = _FENCE_RE.match(lines[j])
                if cm and cm.group(1)[0] == fence_char and len(cm.group(1)) >= fence_len:
                    break
                j += 1
            blocks.append(("fence", lines[i : j + 1]))
            i = j + 1
            continue

        # 标题
        if _HEADING_RE.match(line):
            blocks.append(("heading", [line]))
            i += 1
            continue

        # 表格：连续 | 开头行（原子）
        if line.lstrip().startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            blocks.append(("table", lines[i:j]))
            i = j
            continue

        # 引用块：连续 > 开头行（非原子，可随正文切分）
        if line.lstrip().startswith(">"):
            j = i
            while j < n and lines[j].lstrip().startswith(">"):
                j += 1
            blocks.append(("normal", lines[i:j]))
            i = j
            continue

        # 其余：连续非空行（段落 / 列表），遇到空行或特殊行结束
        j = i
        while j < n:
            cand = lines[j]
            if not cand.strip():
                break
            if _HEADING_RE.match(cand) or _FENCE_RE.match(cand):
                break
            if cand.lstrip().startswith("|") or cand.lstrip().startswith(">"):
                break
            j += 1
        blocks.append(("normal", lines[i:j]))
        i = j
    return blocks


def chunk_markdown(text: str, chunk_size: int = 0, overlap: int = 0) -> list[Chunk]:
    """Markdown 感知分块：表格 / 代码围栏原子化，标题作为章节上下文前缀。

    相比 chunk_text 的两个核心改进：
    1. **原子块永不进 buf** → 长表格 / 长代码块整体独立成块，绝不按字符硬切，
       overlap 也永不始于原子块内部（不变量）；
    2. 每块携带标题路径前缀（如 "# RAG 学习资料清单 › ## 三、可运行的示例项目"），
       检索时知道块属于哪一节。

    Args:
        text: 文档全文
        chunk_size: 单块最大字符数；0 用 config.RAG_CHUNK_SIZE
        overlap: 相邻普通块重叠字符数；0 用 config.RAG_CHUNK_OVERLAP

    Returns:
        list[Chunk]: Chunk(text, section)。text = 章节前缀 + 正文（用于嵌入），
        section = 标题路径字符串（存 Chroma 元数据，便于调试）
    """
    chunk_size = chunk_size or config.RAG_CHUNK_SIZE
    overlap = overlap or config.RAG_CHUNK_OVERLAP

    # Phase A：归一化换行（\r\n / \r -> \n）
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = _scan_blocks(text.split("\n"))

    # Phase C：标题路径栈（level 相等则同级替换，> 则 pop）
    stack: list[tuple[int, str]] = []

    def _section() -> str:
        return " › ".join(f"{'#' * lvl} {txt}" for lvl, txt in stack)

    def _prefix(section: str, body: str) -> str:
        return f"{section}\n\n{body}" if section else body

    chunks: list[Chunk] = []
    buf = ""  # 累积普通块正文（不含前缀；原子块从不进 buf）

    for kind, blines in blocks:
        if kind == "heading":
            # 标题不跨节续接：先 flush 当前节正文（不带 overlap），再更新栈
            if buf:
                chunks.append(Chunk(_prefix(_section(), buf), _section()))
                buf = ""
            m = _HEADING_RE.match(blines[0])
            lvl, txt = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, txt))
            continue

        section = _section()
        prefix = f"{section}\n\n" if section else ""

        if kind in ("fence", "table"):
            # 原子块：先收走普通块，再整体独立成块（绝不合并 / 切分，即使超 chunk_size）
            if buf:
                chunks.append(Chunk(_prefix(section, buf), section))
                buf = ""
            body = "\n".join(blines)
            chunks.append(Chunk(f"{prefix}{body}", section))
            continue

        # 普通块（引用块 / 段落 / 列表）：前缀占掉 chunk_size 的一部分
        body = "\n".join(blines).strip()
        if not body:
            continue
        avail = max(chunk_size - len(prefix), 1)
        if len(body) > avail:
            # 单个超长段落仍硬切（散文可接受；表格 / 围栏不适用）
            if buf:
                chunks.append(Chunk(_prefix(section, buf), section))
                buf = ""
            step = max(avail - overlap, 1)
            for i in range(0, len(body), step):
                piece = body[i : i + avail]
                chunks.append(Chunk(f"{prefix}{piece}", section))
            continue
        if buf and len(buf) + len(body) + 1 > avail:
            chunks.append(Chunk(_prefix(section, buf), section))
            buf = buf[-overlap:] if overlap else ""
        buf = (buf + "\n\n" + body).strip() if buf else body

    if buf:
        chunks.append(Chunk(_prefix(_section(), buf), _section()))
    return chunks


def _content_digest(content: str) -> str:
    """内容哈希，前缀分块器版本号。

    版本号提升 → 同一文件的新 hash 与旧 hash 必然不同 → 既有分块全部失配，
    首次 index 自动全量重切，无需手动清库。
    """
    return sha1(f"{CHUNKER_VERSION}\n{content}".encode("utf-8")).hexdigest()
