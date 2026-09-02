"""笔记去重与文件命名的纯规则（零 I/O、零框架依赖）。

自 storage.py 迁出：sanitize_filename, _topics_overlap, _with_header。
仅依赖 re + datetime。
"""

import itertools
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
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    tag_str = " ".join(f"#{t}" for t in (tags or []))
    return f"# {topic}\n\n> 日期：{date_str}\n> 标签：{tag_str}\n\n{content.lstrip()}"


# ============================================================
# 去重「标题 fast-path」纯函数
# 旧的标签 / 内容 overlap 确认层（_same_knowledge_point 及其信号）在 LLM 合成
# 压力测试（scripts/eval_dedup_synth.json）中被证明无法识别真正措辞不同的同义改写
# （对源笔记方向确认率仅 9%），且标签信号会撞到错误候选造成错合并，已整体删除。
# 新方案：候选召回 → 标题 fast-path（省一次 LLM 调用）→ LLM 判定（judge 见 adapters/llm.py）。
# ============================================================

def _parse_tags(content: str) -> list[str]:
    """从笔记头部解析标签（形如 `> 标签：#a #b`）。纯正则，零 I/O。"""
    for line in (content or "").splitlines()[:5]:
        m = re.search(r">\s*标签[：:]\s*(.*)", line)
        if m:
            return [x.lstrip("#") for x in m.group(1).strip().split() if x.lstrip("#")]
    return []


# 标题 fast-path 停用词：数词 / 量词 / 虚词 / meta 后缀词。它们不标识知识点，
# 保留会放大「共享几个字就判定相同」的误判（如「五种核心角色」vs「五大核心角色」）。
_TITLE_STOP = {
    # 单字：数词 / 量词 / 虚词 / 方位词
    "一", "两", "三", "四", "五", "六", "七", "八", "九", "十", "百", "千", "万",
    "种", "个", "类", "项", "大", "小", "多", "少", "的", "了", "与", "和", "在",
    "用", "是", "对", "之", "中", "下", "上", "内", "等", "及", "或", "其", "这", "那",
    # 双字：meta 后缀 / 对比类（不标识具体知识点，如「持久化机制」vs「持久化原理」）
    "机制", "原理", "详解", "入门", "实践", "概述", "介绍", "指南", "总结", "笔记",
    "学习", "使用", "配置", "方法", "方式", "深入", "浅谈", "对比", "区别", "差异",
    "基础", "核心", "进阶", "高级", "实战", "经验", "心得", "踩坑", "避坑", "最佳",
    "以及", "对于", "关于", "如何", "怎么", "为什么", "vs",
}


# 单字停用集 = 停用词表里的单字 + 双字停用词的组成字（「机制」→机、制也停），
# 用于剔除跨词边界的噪音双字词（「持久化机制」的 bigram「化机」）——否则
# 「Redis 持久化机制」vs「Redis 持久化原理」会因残留「化机」/「化原」判定不等价。
_TITLE_STOP_SINGLE = ({w for w in _TITLE_STOP if len(w) == 1}
                      | {ch for w in _TITLE_STOP if len(w) == 2 for ch in w})


def _title_tokens(title: str) -> set[str]:
    """标题判别性词元：拉丁标识符 + 中文双字词，去掉停用词（数词/量词/meta 后缀）。

    中文按双字词切分（旧 `_topics_overlap` 按单字切，一个"五"字就能让两个标题
    「共享 40% 词元」——「NoSQL内存数据库五种核心角色」误撞「redis 的五大核心角色」的
    错合并根源）。
    """
    low = (title or "").lower()
    latin = {t for t in re.findall(r"[a-z][a-z0-9_.\-]*", low)
             if len(t) >= 2 and t not in _TITLE_STOP}
    cjk = re.findall(r"[一-鿿]", low)
    bigrams = {
        a + b for a, b in itertools.pairwise(cjk)
        if a + b not in _TITLE_STOP and a not in _TITLE_STOP_SINGLE and b not in _TITLE_STOP_SINGLE
    }
    return latin | bigrams


def _title_fast_match(new_topic: str, existing_topic: str) -> bool:
    """标题 fast-path：去停用词后标题词元**完全相等** → 判定为同一知识点。

    语义是「标题基本是同一句」（「Redis 持久化机制」vs「Redis 持久化原理」），
    **不是**「标题高度相似」——「Redis 缓存」vs「Redis 缓存雪崩」（子主题）刻意
    不命中，交给 LLM 判定。fast-path 只省一次 LLM 判定调用，不决定合并
    （合并动作仍由用户确认，见 pipelines/note.py merge_candidates）。

    Args:
        new_topic: 新知识点标题
        existing_topic: 已有笔记标题

    Returns:
        词元集合完全相等 → True；任一标题无词元 → False（空标题不匹配）
    """
    ta, tb = _title_tokens(new_topic), _title_tokens(existing_topic)
    return bool(ta) and bool(tb) and ta == tb


def strip_note_header(content: str) -> str:
    """去掉笔记文件头部（# 标题 + > 日期/> 标签 元信息），只留正文。

    兼容 _with_header 生成的格式：`# 主题\\n\\n> 日期：...\\n> 标签：...\\n\\n正文`。
    差量合并（merge_notes）喂给 LLM 前用它剥头，避免 LLM 把头部当正文重复输出。
    """
    lines = content.splitlines()
    i = 0
    # 只认 `# 主题` 这种单 # 文件头（_with_header 产出格式）；`## 二级标题` 是正文，不剥
    if i < len(lines) and lines[i].startswith("# "):
        i += 1
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith(">")):
        i += 1
    return "\n".join(lines[i:]).lstrip()
