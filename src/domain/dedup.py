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


# ============================================================
# 去重「确认层」纯函数（RAG_OPTIMIZATION P0）
# 语义相似度只负责找候选；是否同一知识点由标题 / 具体标签 / 内容判别性概念验明正身，
# 避免「高相似但无关」的误合并（如 Redis 主题对多篇笔记都 0.6+ 相似）。
# ============================================================

def _parse_tags(content: str) -> list[str]:
    """从笔记头部解析标签（形如 `> 标签：#a #b`）。纯正则，零 I/O。"""
    for line in (content or "").splitlines()[:5]:
        m = re.search(r">\s*标签[：:]\s*(.*)", line)
        if m:
            return [x.lstrip("#") for x in m.group(1).strip().split() if x.lstrip("#")]
    return []


def _tech_tag_aliases(tech: str) -> set[str]:
    """tech 名可能出现的标签形态（sanitize 后 / 原样 / 去连字符），用于从标签判定里剔除。"""
    s = sanitize_filename(tech)
    return {s, s.replace("-", " "), tech.strip().lower()}


# 元标签停用表：跨笔记出现的流程/任务类标签，不标识具体知识点，参与重叠会误确认。
# （实证：windows 笔记 #部署 让「Docker 部署 Redis 集群」误并入，见 P0 去重评估。）
_TAG_STOP = {"踩坑", "避坑", "最佳实践", "实战", "经验", "心得", "笔记", "学习", "教程",
             "指南", "分享", "总结", "入门", "进阶", "高级", "部署", "安装", "使用",
             "配置", "场景", "对比", "原理"}


def _tag_overlap(new_tags: list[str], existing_tags: list[str], tech: str) -> bool:
    """具体标签重叠：剔除 tech 名与元标签后共享 ≥1 个标签即确认。

    高相似但无关的典型形态是新旧笔记只共享技术域标签（如 #Redis）；正确合并则共享
    具体标签（如 #数据结构 #选型）。tech 名与元标签（踩坑/部署/最佳实践…）不提供
    知识点判别力，不参与判定。
    """
    tech_aliases = _tech_tag_aliases(tech)
    new = {t.strip().lower() for t in (new_tags or [])
           if t.strip().lower() not in tech_aliases and t.strip().lower() not in _TAG_STOP}
    existing = {t.strip().lower() for t in (existing_tags or [])
                if t.strip().lower() not in tech_aliases and t.strip().lower() not in _TAG_STOP}
    return bool(new & existing)


# 内容判别性概念抽取：拉丁标识符 + 中文双字词，去掉功能词。
# 拉丁停用词含技术域常见词（redis/rag/json/api）——它们跨笔记出现，不提供判别力。
_CJK_STOP = {"一个", "这个", "那个", "可以", "能够", "进行", "使用", "通过", "因为", "所以",
             "如果", "以及", "但是", "然后", "不是", "就是", "对于", "还有", "需要", "我们",
             "他们", "时候", "可能", "非常", "具有", "其中", "以下", "例如", "比如", "还是",
             "没有", "已经", "这样", "那样", "同时", "以及", "必须"}
_LATIN_STOP = {"the", "and", "of", "to", "for", "with", "on", "in", "a", "an", "is", "are",
               "be", "it", "this", "that", "from", "into", "as", "by", "or", "at",
               "https", "http", "com", "org", "www", "redis", "rag", "json", "api", "url"}


def _discriminative_concepts(text: str) -> set[str]:
    """抽取文本的判别性概念：拉丁标识符（RedisJSON/FT.SEARCH/RDB）+ 中文双字词（去停用词）。

    专供 containment 型内容重叠（`_content_concept_overlap`）：衡量「新笔记的概念有多少
    出现在旧笔记正文里」，而非两篇文本的对称相似度——后者正是「高相似但无关」的源头。
    """
    low = (text or "").lower()
    latin = {t for t in re.findall(r"[a-z][a-z0-9_.\-]*", low)
             if len(t) >= 2 and t not in _LATIN_STOP}
    cjk = re.findall(r"[一-鿿]", low)
    bigrams = {a + b for a, b in zip(cjk, cjk[1:]) if a + b not in _CJK_STOP}
    return latin | bigrams


def _content_concept_overlap(new_content: str, existing_content: str, threshold: float = 0.3) -> bool:
    """新笔记的判别性概念有多少出现在旧笔记正文里（containment，非对称）。

    Args:
        new_content: 新知识点正文（判别性概念来源）
        existing_content: 已有笔记正文（前 2000 字即可；判别性概念多在文首）
        threshold: 概念命中占比下限

    Returns:
        命中占比 >= threshold 视为内容确认同一知识点。
    """
    concepts = _discriminative_concepts(new_content)
    if not concepts:
        return False
    body = (existing_content or "").lower()
    hit = sum(1 for c in concepts if c in body)
    return hit / len(concepts) >= threshold


def _same_knowledge_point(topic: str, tags: list[str] | None, content: str | None,
                          existing: dict, tech: str, *, content_threshold: float = 0.3) -> str:
    """多信号合并确认：标题 / 具体标签 / 内容判别性概念任一确认 → "same"，否则 "no"。

    设计（RAG_OPTIMIZATION P0 去重优化）：把「候选召回」和「合并决策」解耦。
    语义相似度只负责找候选（不调高阈值、不牺牲召回）；是否「同一知识点」由本函数
    验明正身，避免高相似但无关的误合并。

    - 信号优先级：标题 overlap > 具体标签 overlap > 内容判别性概念 overlap。
      前两者任一确认即合并；都不确认时才查内容（内容信号主要救「措辞不同但同一件事」）。
    - 都不确认 → "no"（不合并，作为新笔记入库）——宁可出现可修的重复笔记，也不静默错并。

    Args:
        topic: 新知识点标题
        tags: 新知识点标签（可为 None；None 时标签信号不生效）
        content: 新知识点正文（可为 None；None 时内容信号不生效）
        existing: 已有笔记 dict（需含 topic / tags / content 字段）
        tech: 技术名称（用于剔除 tech 名标签）
        content_threshold: 内容信号的概念命中占比下限（默认 0.3）

    Returns:
        "same" / "no"。
    """
    existing_topic = existing.get("topic") or ""
    if _topics_overlap(topic, existing_topic):
        return "same"
    if _tag_overlap(tags or [], existing.get("tags") or [], tech):
        return "same"
    if content and existing.get("content"):
        if _content_concept_overlap(content, existing["content"], threshold=content_threshold):
            return "same"
    return "no"


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
