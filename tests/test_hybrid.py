"""domain/hybrid 纯函数单测（零网络）：BM25 打分排序正确 + RRF 融合正确。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_hybrid.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.hybrid import build_bm25, rrf_fuse, tokenize


# ---------- tokenize ----------

def test_tokenize_english_and_chinese_char():
    toks = tokenize("Redis 缓存穿透")
    assert "redis" in toks
    assert {"缓", "存", "穿", "透"} <= set(toks)


def test_tokenize_splits_punct_and_drops_stopwords():
    toks = tokenize("The FT.SEARCH 与 Redis")
    assert "the" not in toks
    assert "ft" in toks and "search" in toks
    assert "与" not in toks
    assert "redis" in toks


# ---------- BM25 ----------

def test_bm25_exact_token_ranks_above():
    """专有名词精确命中：只有含 RedisJSON 的文档该得高分，其余为 0（dense 的弱点 BM25 无）。"""
    docs = [
        "Redis 基础数据类型选型",
        "Redis Stack 集成了 RediSearch RedisJSON RedisTimeSeries 等模块",
        "向量数据库 VectorStore 的构建",
    ]
    scores = build_bm25(docs).score("RedisJSON")
    assert scores[1] > scores[0] and scores[1] > scores[2]
    assert scores[0] == 0.0 and scores[2] == 0.0


def test_bm25_ordering_by_relevance():
    """多文档里含查询词频更高的文档排更前。"""
    docs = [
        "缓存 缓存 缓存",   # 词频 3
        "缓存",              # 词频 1
        "完全不相关的内容",
    ]
    scores = build_bm25(docs).score("缓存")
    assert scores[0] > scores[1] > scores[2]


def test_bm25_no_match_all_zero():
    scorer = build_bm25(["abc 缓存", "def"])
    assert scorer.score("zzqq") == [0.0, 0.0]  # 与文档无任何共享词项
    assert scorer.score("") == [0.0, 0.0]


def test_bm25_empty_docs():
    assert build_bm25([]).score("anything") == []


# ---------- RRF ----------

def _hit(idx: int, path: str) -> dict:
    return {"id": f"{path}::{idx}", "path": path, "source": "knowledge", "tech": "redis",
            "topic": "t", "url": "", "similarity": 1.0, "document": "d"}


def test_rrf_ranked_in_both_wins():
    """两路都排第 1 的条目融合分 > 只在一路排第 1 的。"""
    dense = [_hit(0, "a/role"), _hit(1, "a/redis-stack")]
    sparse = [_hit(0, "a/role")]  # role 在 dense 第 1、sparse 第 1
    fused = rrf_fuse(dense, sparse, k=60)
    assert fused[0]["id"] == "a/role::0"
    assert fused[0]["path"] == "a/role"


def test_rrf_sparse_only_outranks_dense_tail():
    """只出现在 sparse 第 1 的条目（词法强命中）应排在 dense 第 2 的语义弱命中前面。"""
    dense = [_hit(0, "a/noise"), _hit(1, "a/noise2")]
    sparse = [_hit(0, "a/redis-stack")]
    fused = rrf_fuse(dense, sparse, k=60)
    ids = [h["id"] for h in fused]
    assert len(fused) == 3
    assert ids.index("a/redis-stack::0") < ids.index("a/noise2::1")


def test_rrf_normalized_top_one():
    dense = [_hit(0, "a/x"), _hit(1, "a/y")]
    sparse = [_hit(0, "a/x")]
    fused = rrf_fuse(dense, sparse, k=60)
    assert fused[0]["similarity"] == 1.0
    assert all(0.0 <= h["similarity"] <= 1.0 for h in fused)


def test_rrf_dedup_and_preserves_dense_sim():
    """同 id 去重；dense 独有条目保留 dense_similarity，sparse 独有条目为 None。"""
    dense = [_hit(0, "a/x"), _hit(0, "a/x")]  # 同 id 重复出现
    fused = rrf_fuse(dense, [_hit(0, "a/y")])
    assert len(fused) == 2  # a/x 去重、a/y 另计
    x = next(h for h in fused if h["id"] == "a/x::0")
    y = next(h for h in fused if h["id"] == "a/y::0")
    assert x["dense_similarity"] == 1.0
    assert y["dense_similarity"] is None
    assert y["bm25_score"] == 1.0
