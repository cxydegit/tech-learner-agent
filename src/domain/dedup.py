"""笔记去重与文件命名的纯规则（零 I/O、零框架依赖）。

自 storage.py 迁出：sanitize_filename, _topics_overlap, _with_header。
仅依赖 re + datetime。
"""

import re
from datetime import datetime


def sanitize_filename(name: str) -> str:
    """将技术名/主题名转换为安全的文件名。"""
    return re.sub(r"[^\w\-]", "-", name.strip()).strip("-").lower()


def _topics_overlap(a: str, b: str) -> bool:
    """判断两个知识点标题是否高度相似（用于去重）。

    满足以下任一条件即视为重叠：
    - 完全相等
    - 一方是另一方的子串
    - 去掉停用词后，共享的关键词占比 >= 40%
    """
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True

    def _words(s: str) -> set[str]:
        # 中文按字词切分，英文按空格切分；去停用词
        stop = {"的", "了", "和", "与", "在", "用", "是", "对", "个", "the", "a", "an",
                "and", "of", "in", "to", "for", "on", "with", "by"}
        w = set(re.findall(r"[a-zA-Z0-9]+", s)) | set(re.findall(r"[一-鿿]", s))
        return {x for x in w if x not in stop}

    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    intersect = wa & wb
    overlap = max(len(intersect) / len(wa), len(intersect) / len(wb))
    return overlap >= 0.4


def _with_header(topic: str, tags: list[str] | None, content: str) -> str:
    """给笔记正文加上标题和标签头部。"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    tag_str = " ".join(f"#{t}" for t in (tags or []))
    return f"# {topic}\n\n> 日期：{date_str}\n> 标签：{tag_str}\n\n{content.lstrip()}"
