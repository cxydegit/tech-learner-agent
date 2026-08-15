"""混合检索纯函数：BM25 打分 + RRF 名次融合（零 I/O、零框架依赖）。

P1（docs/RAG_OPTIMIZATION.md）：补纯 dense 漏「精确词匹配」的经典弱点——搜
RedisJSON / FT.SEARCH 这类专有名词时，embedding 模糊匹配失效。BM25 对词法命中
是秒杀级（token 原样躺在文档里），RRF 无权重融合两条名次链：精确词命中由 BM25
榜单兜住，语义相近由 dense 榜单兜住。

全部标准库自实现（不引 rank_bm25）：当前知识库规模小，内存打分即可。
分词规则与 domain/dedup.py::_topics_overlap 保持一致（英文按词、中文按单字），
保证与既有去重逻辑对"词"的理解一致。
"""

from __future__ import annotations

import math
import re
from collections import Counter

# 与 domain/dedup.py::_topics_overlap 相同的切词粒度：英文单词 / 中文单字
_WORD_RE = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")
_STOPWORDS = frozenset({
    # 中文
    "的", "了", "和", "与", "在", "用", "是", "对", "个", "有", "这", "那",
    # 英文
    "the", "a", "an", "and", "of", "in", "to", "for", "on", "with", "by",
})


def tokenize(text: str) -> list[str]:
    """分词：英文按词、中文按单字，小写、去停用词。

    "FT.SEARCH" -> ["ft", "search"]；"Redis 缓存穿透" -> ["redis", "缓", "存", "穿", "透"]。
    """
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS]


class BM25Scorer:
    """Okapi BM25 打分器。

    build 时统计词频 / 文档频率 / 平均长度；``score(query)`` 返回与 build 时
    docs 等长的分数列表（文档间可比，与输入顺序对齐）。

    k1 控制词频饱和（越大同一文档内重复词项越加分），b 控制长度归一化强度
    （0 关闭长度惩罚）。标准默认 k1=1.2, b=0.75。
    """

    def __init__(self, docs: list[str], *, k1: float = 1.2, b: float = 0.75) -> None:
        self._docs = [tokenize(d) for d in docs]
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
        terms = [t for t in set(tokenize(query)) if self._df[t] > 0]
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


def build_bm25(docs: list[str], *, k1: float = 1.2, b: float = 0.75) -> BM25Scorer:
    """构建 BM25 打分器（docs 顺序即分数对齐顺序）。"""
    return BM25Scorer(docs, k1=k1, b=b)


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
        - ``dense_similarity``: dense 原始余弦（若该 id 未进 dense 则为 None，留给 P2 阈值）
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
