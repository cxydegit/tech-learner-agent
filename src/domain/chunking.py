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
# 当前版本行为：超大原子块硬上限 + 逻辑二次切分（表格按行重复表头 / 代码按空行分组 + 截断保底）。
CHUNKER_VERSION = 3

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
# Markdown 感知分块
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
    围栏另起一块，各自完整；连续标题各自成块；整篇只有一张大表 → 单个原子块，
    绝不撕碎（除非超过 RAG_CHUNK_HARD_CAP，此时由 chunk_markdown 按行二次切分）。
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


_TABLE_SEP_RE = re.compile(r"^[\s|:+-]+$")


def _split_oversize_table(blines: list[str], hard_cap: int) -> list[str]:
    """超大表格按行二次切分，每块重复表头（Markdown 表头复制）。

    表头 + 分隔行固定重复到每一块，数据行按字符预算分组；
    单行本身仍超过 hard_cap 时截断该行保底（Option B），保证不产超限块。
    """
    header = blines[0]
    if len(header) > hard_cap:  # 病态：表头本身超限
        header = header[:hard_cap]
    sep = None
    start = 1
    if len(blines) > 1 and "-" in blines[1] and _TABLE_SEP_RE.match(blines[1].strip()):
        sep = blines[1]
        start = 2
    head = "\n".join(x for x in (header, sep) if x)
    rows = blines[start:]

    groups: list[list[str]] = []
    cur = [head]
    cur_len = len(head)
    for r in rows:
        add = len(r) + 1  # 换行符
        if cur_len + add > hard_cap:
            if len(cur) > 1:  # 只收有数据行的组，避免表头孤块
                groups.append(cur)
            cur = [head, r]
            cur_len = len(head) + add
            if cur_len > hard_cap:
                budget = max(hard_cap - len(head) - 1, 10)  # 单行截断保底
                cur = [head, r[:budget]]
                cur_len = len(head) + len(cur[1]) + 1
            continue
        cur.append(r)
        cur_len += add
    if len(cur) > 1:
        groups.append(cur)
    return ["\n".join(g) for g in groups]


def _split_oversize_fence(blines: list[str], hard_cap: int) -> list[str]:
    """超大代码围栏按空行（逻辑段落）二次切分，每块闭合为完整围栏。

    单段落本身仍超过 hard_cap 时截断该段落保底（Option B）。
    """
    opening = blines[0]
    closing = blines[-1] if blines[-1].lstrip().startswith(("```", "~~~")) else opening
    inner = blines[1:-1] if len(blines) >= 2 else []

    # 按空行分组 → 逻辑段落
    paras: list[str] = []
    cur: list[str] = []
    for ln in inner:
        if ln.strip():
            cur.append(ln)
        elif cur:
            paras.append("\n".join(cur))
            cur = []
    if cur:
        paras.append("\n".join(cur))
    if not paras:
        return ["\n".join(blines)]  # 空围栏原样返回

    inner_budget = max(hard_cap - len(opening) - len(closing) - 2, 10)

    def _wrap(ps: list[str]) -> str:
        return "\n".join([opening, *ps, closing])

    groups: list[list[str]] = []
    cur_group: list[str] = []
    cur_len = 0
    for p in paras:
        if cur_group and cur_len + len(p) + 1 > inner_budget:
            groups.append(cur_group)
            cur_group = []
            cur_len = 0
        if len(p) > inner_budget:
            if cur_group:
                groups.append(cur_group)
                cur_group = []
                cur_len = 0
            groups.append([p[:inner_budget]])  # 单段落截断保底
            continue
        cur_group.append(p)
        cur_len += len(p) + 1
    if cur_group:
        groups.append(cur_group)
    return [_wrap(g) for g in groups]


def _split_oversize_atomic(kind: str, blines: list[str], hard_cap: int) -> list[str]:
    """超大原子块（超过 RAG_CHUNK_HARD_CAP）的逻辑二次切分。

    表格按行重复表头；代码按空行分组、闭合为完整围栏；单行/单段落仍超限时截断保底。
    """
    if kind == "table":
        return _split_oversize_table(blines, hard_cap)
    return _split_oversize_fence(blines, hard_cap)


def chunk_markdown(text: str, chunk_size: int = 0, overlap: int = 0, hard_cap: int = 0) -> list[Chunk]:
    """Markdown 感知分块：表格 / 代码围栏原子化，标题作为章节上下文前缀。

    相比 chunk_text 的两个核心改进：
    1. **原子块永不进 buf** → 长表格 / 长代码块整体独立成块，绝不按字符硬切，
       overlap 也永不始于原子块内部（不变量）；超过 RAG_CHUNK_HARD_CAP 的病态超大块
       例外：按逻辑结构二次切分（表格按行重复表头 / 代码按空行分组），避免超出 embedding 上限；
    2. 每块携带标题路径前缀（如 "# RAG 学习资料清单 › ## 三、可运行的示例项目"），
       检索时知道块属于哪一节。

    Args:
        text: 文档全文
        chunk_size: 单块最大字符数；0 用 config.RAG_CHUNK_SIZE
        overlap: 相邻普通块重叠字符数；0 用 config.RAG_CHUNK_OVERLAP
        hard_cap: 原子块硬上限（字符）；0 用 config.RAG_CHUNK_HARD_CAP。
            超过它的表格/代码块不再整块保留，按逻辑结构二次切分（见 _split_oversize_atomic）。

    Returns:
        list[Chunk]: Chunk(text, section)。text = 章节前缀 + 正文（用于嵌入），
        section = 标题路径字符串（存 Chroma 元数据，便于调试）
    """
    chunk_size = chunk_size or config.RAG_CHUNK_SIZE
    overlap = overlap or config.RAG_CHUNK_OVERLAP
    hard_cap = hard_cap or config.RAG_CHUNK_HARD_CAP

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
            # 原子块：先收走普通块，再整体独立成块（绝不合并 / 切分，即使超 chunk_size）。
            # 但超过 hard_cap 的病态超大块，按逻辑结构二次切分（表格按行重复表头 / 代码按空行分组），
            # 防超出 embedding 输入上限导致整文件索引失败。
            if buf:
                chunks.append(Chunk(_prefix(section, buf), section))
                buf = ""
            body = "\n".join(blines)
            if len(body) > hard_cap:
                for sub in _split_oversize_atomic(kind, blines, hard_cap):
                    chunks.append(Chunk(f"{prefix}{sub}", section))
            else:
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
    return sha1(f"{CHUNKER_VERSION}\n{content}".encode()).hexdigest()
