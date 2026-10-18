"""笔记题头解析（纯领域逻辑，零 I/O、零框架依赖）。

笔记格式由 ``domain.dedup._with_header`` 生成：

    # 标题

    > 日期：2026-08-09
    > 标签：#Redis #缓存

    ## 正文小节
    ...

题头行没有知识内容。建索引前把它们剥离并转成 Chroma 元数据，避免「标题+日期+标签」
单独成为一个零内容分块：实测 knowledge 209 块里有 52 块是这种块，它们文本短、标题词
密集，相似度天然偏高，会白占召回名额。
"""

from __future__ import annotations

import re

_HEADER_RE = re.compile(r"^>\s*(日期|标签)\s*[：:]\s*(.*)$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def _parse_tags(text: str) -> list[str]:
    """按空白切分标签并去掉 # 前缀（与 domain.dedup._parse_tags 同口径）。"""
    return [x.lstrip("#") for x in (text or "").strip().split() if x.lstrip("#")]


def parse_note_header(text: str) -> tuple[str | None, list[str], str]:
    """解析并剥离笔记开头的 ``> 日期`` / ``> 标签`` 行。

    只处理文档开头第一个连续引用块；块内其他行（如 ``> 原文链接``）原样保留。
    没有匹配行时返回 ``(None, [], 原文)``——非笔记类文档（materials/reports）不受影响。

    Returns:
        (日期字符串, 标签列表, 去掉题头行后的正文)。日期缺省为 None，标签缺省为 []。
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    date: str | None = None
    tags: list[str] = []

    i, n = 0, len(lines)
    # 跳过开头的空行与标题行，定位到第一个引用块
    while i < n and (not lines[i].strip() or _HEADING_RE.match(lines[i])):
        out.append(lines[i])
        i += 1

    # 第一个引用块：逐行判断，只剥离日期/标签行；其余引用行原样保留
    if i < n and lines[i].lstrip().startswith(">"):
        while i < n and lines[i].lstrip().startswith(">"):
            m = _HEADER_RE.match(lines[i].strip())
            if m:
                key, val = m.group(1), m.group(2).strip()
                if key == "日期" and date is None:
                    date = val
                elif key == "标签":
                    tags.extend(_parse_tags(val))
            else:
                out.append(lines[i])
            i += 1

    out.extend(lines[i:])
    return date, tags, "\n".join(out)
