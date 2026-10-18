"""混合检索纯函数：BM25 打分 + RRF 名次融合（零 I/O、零框架依赖）。

设计动机：补纯 dense 漏「精确词匹配」的经典弱点——搜
RedisJSON / FT.SEARCH 这类专有名词时，embedding 模糊匹配失效。BM25 对词法命中
是秒杀级（token 原样躺在文档里），RRF 无权重融合两条名次链：精确词命中由 BM25
榜单兜住，语义相近由 dense 榜单兜住。

全部标准库自实现（不引 rank_bm25）：当前知识库规模小，内存打分即可。
分词规则：英文按词、中文按**相邻两字（bigram）**，但**点号不拆词**：
FT.SEARCH / RedisJSON 这类点号分隔的缩写整体算一个 token——拆成 ft/search 会让
罕见缩写被高频词稀释掉 idf，正是想修的精确词弱点的来源之一。

中文为什么从单字改成 bigram：单字切分把「哪/些/么/里」这类虚词单字也当成高 IDF
词项，中文查询的 BM25 候选榜会被只共享单字的无关块灌满（实测 30 条中文 /ask 查询
平均 46.9% 的块得分 > 0）。bigram 要求相邻两字同时出现，保住复合词（向量/数据/据库、
上下/下文）的稀有度。实测 72 条场景集：/ask 交付召回 90.5%→96.8%，cross_note
81%→100%（dense 与取件口径不变）。`tokenize_char` 保留旧单字口径，供
config.QA_BM25_TOKENIZER=char 灰度 / 回滚与对照。
"""

from __future__ import annotations

import math
import re
from collections import Counter

# 英文单词（允许点号在词内，保住 FT.SEARCH 类缩写整体性）/ 中文单字（char 口径）
_WORD_RE = re.compile(r"[a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)*|[一-鿿]")
# 英文单词 / 连续中文 run（bigram 口径：一次取整个 run，再切相邻两字）
_WORD_OR_CJK_RE = re.compile(r"[a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)*|[一-鿿]+")
_CJK_RE = re.compile(r"[一-鿿]+")
_STOPWORDS = frozenset({
    # 中文
    "的", "了", "和", "与", "在", "用", "是", "对", "个", "有", "这", "那",
    # 英文
    "the", "a", "an", "and", "of", "in", "to", "for", "on", "with", "by",
})


def tokenize_char(text: str) -> list[str]:
    """旧口径：英文按词、中文按单字，小写、去停用词。

    "FT.SEARCH" -> ["ft.search"]（点号不拆）；"Redis 缓存穿透" -> ["redis", "缓", "存", "穿", "透"]。
    """
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS]


def tokenize_bigram(text: str) -> list[str]:
    """生产口径：英文按词、中文按相邻两字（bigram），单字中文 run 保留单字。

    "向量数据库" -> ["向量", "量数", "数据", "据库"]；"Redis 缓存" -> ["redis", "缓存"]。
    "FT.SEARCH" 仍整体成 token（点号不拆）。停用词只过滤英文整词和单字中文；
    跨词边界的 bigram（如「量数」）本身罕见，不会像单字那样大面积命中，故原样保留。
    """
    out: list[str] = []
    for match in _WORD_OR_CJK_RE.finditer((text or "").lower()):
        token = match.group(0)
        if not _CJK_RE.fullmatch(token) or len(token) == 1:
            if token not in _STOPWORDS:
                out.append(token)
        else:
            out.extend(token[i:i + 2] for i in range(len(token) - 1))
    return out


def tokenize(text: str) -> list[str]:
    """默认分词口径（生产使用 bigram）；单字口径用 ``tokenize_char``。"""
    return tokenize_bigram(text)


TOKENIZERS = {"char": tokenize_char, "bigram": tokenize_bigram}


def get_tokenizer(name: str):
    """按配置名取分词函数；未知值回退生产默认 bigram（避免配置写错静默退回旧噪声口径）。"""
    return TOKENIZERS.get((name or "").strip().lower(), tokenize_bigram)


class BM25Scorer:
    """Okapi BM25 打分器。

    build 时统计词频 / 文档频率 / 平均长度；``score(query)`` 返回与 build 时
    docs 等长的分数列表（文档间可比，与输入顺序对齐）。

    k1 控制词频饱和（越大同一文档内重复词项越加分），b 控制长度归一化强度
    （0 关闭长度惩罚）。标准默认 k1=1.2, b=0.75。
    ``tokenizer`` 决定词项口径（默认 bigram，config.QA_BM25_TOKENIZER=char 可回滚）。
    """

    def __init__(self, docs: list[str], *, k1: float = 1.2, b: float = 0.75,
                 tokenizer=tokenize) -> None:
        self._tokenize = tokenizer
        self._docs = [tokenizer(d) for d in docs]
        self._doc_len = [len(t) for t in self._docs]
        self._n = len(self._docs)
        self._avgdl = sum(self._doc_len) / self._n if self._n else 0.0
        self._df: Counter[str] = Counter()
        for toks in self._docs:
            for term in set(toks):
                self._df[term] += 1
        self._k1 = k1
        self._b = b

    def score(self, query: str) -> list[float]:
        """返回与 docs 等长的 BM25 分数列表；无匹配词项则全 0。"""
        if not self._n:
            return []
        terms = [t for t in set(self._tokenize(query)) if self._df[t] > 0]
        if not terms:
            return [0.0] * self._n
        scores = [0.0] * self._n
        for term in terms:
            df = self._df[term]
            idf = math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))
            for i, toks in enumerate(self._docs):
                f = toks.count(term)
                if f == 0:
                    continue
                denom = f + self._k1 * (1.0 - self._b + self._b * self._doc_len[i] / self._avgdl)
                scores[i] += idf * (f * (self._k1 + 1.0)) / denom
        return scores


def build_bm25(docs: list[str], *, k1: float = 1.2, b: float = 0.75,
               tokenizer=tokenize) -> BM25Scorer:
    """构建 BM25 打分器（docs 顺序即分数对齐顺序；tokenizer 默认 bigram）。"""
    return BM25Scorer(docs, k1=k1, b=b, tokenizer=tokenizer)


def rrf_fuse(dense: list[dict], sparse: list[dict], k: int = 60) -> list[dict]:
    """名次倒数相加融合（Reciprocal Rank Fusion），返回按融合分降序、按 id 去重的命中列表。

    对两份按相关度降序的命中列表（各条含 ``id``），score += 1/(k + rank)（rank 从 1 起）。
    同时出现在两条列表里的条目因分数累计而排前——这是融合的价值：BM25 榜兜精确词、
    dense 榜兜语义，两者都中的自然最相关。

    Args:
        dense: 语义检索命中（按相似度降序）
        sparse: 词法 BM25 命中（按分数降序）
        k: 融合常数，默认 60（与 config.QA_RRF_K 一致）

    Returns:
        融合后列表；每条以 dense 的 dict 为基础（稀疏独有条目则用 sparse 的 dict），
        并追加：
        - ``rrf_score``: 原始融合分
        - ``similarity``: 归一化融合分（最大值归 1，用于按相关度排序 / 展示）
        - ``dense_similarity``: dense 原始余弦（若该 id 未进 dense 则为 None，留给阈值过滤）
        - ``bm25_score``: BM25 归一化分（若未进 sparse 则为 None）
    """
    merged: dict[str, dict] = {}
    for rank, item in enumerate(dense, 1):
        doc_id = item.get("id")
        if not doc_id:
            continue
        entry = merged.get(doc_id) or {
            key: item[key] for key in ("id", "path", "source", "tech", "topic", "url", "document")
            if key in item
        }
        entry["dense_similarity"] = item.get("similarity")
        entry["bm25_score"] = entry.get("bm25_score")
        entry["rrf_score"] = entry.get("rrf_score", 0.0) + 1.0 / (k + rank)
        merged[doc_id] = entry
    for rank, item in enumerate(sparse, 1):
        doc_id = item.get("id")
        if not doc_id:
            continue
        if doc_id in merged:
            entry = merged[doc_id]
        else:
            entry = {
                key: item[key] for key in ("id", "path", "source", "tech", "topic", "url", "document")
                if key in item
            }
            entry["dense_similarity"] = None
            merged[doc_id] = entry
        entry["bm25_score"] = item.get("similarity")
        entry["rrf_score"] = entry.get("rrf_score", 0.0) + 1.0 / (k + rank)

    ordered = sorted(merged.values(), key=lambda d: d["rrf_score"], reverse=True)
    max_score = ordered[0]["rrf_score"] if ordered else 0.0
    for d in ordered:
        d["similarity"] = d["rrf_score"] / max_score if max_score > 0 else 0.0
    return ordered


def lexical_rerank(fused: list[dict], query: str, *, w: float,
                   tokenizer=tokenize) -> list[dict]:
    """词法一致性软重排：RRF 融合后按「查询词在块中的覆盖率」加分，打破并列。

    动机：纯 dense 对裸缩写 / 专有名词查询会检索到与查询**零词法重合**的块——
    如 "RoPE" 把 rag-架构模式 排语义榜第 1（sim 0.541）但它通篇没有 RoPE；
    RRF 无权重，导致这个噪声块与真正的关键词命中（transformer 笔记）打平。
    覆盖率加分让「确实含查询词」的块胜出，纠正 dense 噪声。覆盖率只加分不减分，
    常规语义查询（正确块同源词少）不会被惩罚，只是微调。

    Args:
        fused: rrf_fuse 的结果（每条含 document 块文本）
        query: 原始查询
        w: 覆盖率权重；w=0 等价纯 RRF 排序（可关回）
        tokenizer: 词项口径，需与 BM25 侧一致（默认 bigram；char 用于回滚/对照）

    Returns:
        按 rerank_score 降序的新列表（不改原列表），每条附加 lexical_coverage / rerank_score。
    """
    q_toks = set(tokenizer(query))
    if not q_toks:
        return fused
    # 覆盖率按词在候选集上的 mini-idf 加权：罕见词（redisjson/rope）权重大，
    # 常见词（几乎每块都有）权重趋近 0。否则覆盖率会被烂大街词灌满，
    # 把真含关键术语的块反而压低（RedisJSON 案例教训）。
    n = len(fused) or 1
    df: Counter = Counter()
    for h in fused:
        df.update(set(tokenizer(h.get("document") or "")))
    idf = lambda t: math.log(n / df[t]) if df[t] else math.log(n)
    denom = sum(idf(t) for t in q_toks) or 1.0

    out: list[dict] = []
    for h in fused:
        doc_toks = set(tokenizer(h.get("document") or ""))
        cov = sum(idf(t) for t in q_toks & doc_toks) / denom
        h = dict(h)
        h["lexical_coverage"] = cov
        h["rerank_score"] = h.get("rrf_score", 0.0) * (1.0 + w * cov)
        out.append(h)
    out.sort(key=lambda h: h["rerank_score"], reverse=True)
    return out
