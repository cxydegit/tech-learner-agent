"""hybrid_search_knowledge：候选池深度与返回条数解耦（零网络，两路检索打桩）。

动机：候选池此前由 ``top_k`` 推导（``max(top_k*3, 8)``），于是"放大 top_k 看完整排名"
会连带放大候选池、改变 RRF 融合排序——放大后的结果**不是**原结果的加长版，跨设定的
名次无法比较。``candidates`` 参数把两者拆开：不传时行为与从前完全一致（生产路径零变化），
诊断时可固定生产口径的池子、拿到完整融合排名，回答"这条期望命中到底排第几"。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_vector_hybrid.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fake_hits(prefix: str, n: int) -> list[dict]:
    """构造 n 条命中（两路前缀不同、id 不重叠，便于观察融合后的完整长度）。"""
    return [{"id": f"{prefix}{i}", "path": f"{prefix}/note{i}", "source": "knowledge",
             "tech": "t", "topic": "t", "url": "", "similarity": 1.0 - i * 0.01,
             "document": f"doc{i}"} for i in range(n)]


def _patch(monkeypatch):
    """两路检索换成记录入参的桩；返回 (vector 模块, 入参记录)。"""
    import src.adapters.vector as vector

    seen: dict[str, int] = {}

    def semantic(query, top_k=1, tech=None):
        seen["dense"] = top_k
        return _fake_hits("d", top_k)

    def keyword(query, top_k=1, tech=None):
        seen["sparse"] = top_k
        return _fake_hits("k", top_k)

    monkeypatch.setattr(vector, "semantic_search_knowledge", semantic)
    monkeypatch.setattr(vector, "keyword_search_knowledge", keyword)
    return vector, seen


def test_default_pool_derives_from_top_k(monkeypatch):
    """不传 candidates：每路取 top_k*3 条候选，返回 top_k 条（与从前一致）。"""
    vector, seen = _patch(monkeypatch)
    out = vector.hybrid_search_knowledge("q", top_k=8)
    assert seen == {"dense": 24, "sparse": 24}
    assert len(out) == 8


def test_default_pool_has_minimum_eight(monkeypatch):
    vector, seen = _patch(monkeypatch)
    vector.hybrid_search_knowledge("q", top_k=2)
    assert seen == {"dense": 8, "sparse": 8}


def test_candidates_pins_pool_while_returning_more(monkeypatch):
    """诊断口径：池子钉在生产值 24、返回放大到 48 → 拿到完整融合排名（24+24）。"""
    vector, seen = _patch(monkeypatch)
    out = vector.hybrid_search_knowledge("q", top_k=48, candidates=24)
    assert seen == {"dense": 24, "sparse": 24}
    assert len(out) == 48


def test_pinned_pool_keeps_prefix_order(monkeypatch):
    """池子钉住时，放大返回条数不改变前 8 名——这正是"加长版"成立的前提。"""
    vector, _ = _patch(monkeypatch)
    long_out = vector.hybrid_search_knowledge("q", top_k=48, candidates=24)
    short_out = vector.hybrid_search_knowledge("q", top_k=8, candidates=24)
    assert [h["id"] for h in long_out[:8]] == [h["id"] for h in short_out]


def test_production_recall_extends_the_same_ranking(monkeypatch):
    """生产取件口径：召回 24 条（池子取同值）的前 8 名 == 旧口径（top_k=8，池子由推导得 24）。

    这是 P1-1b 的 before/after 能归因干净的前提：排名一条没动，改的只是取件。
    """
    vector, _ = _patch(monkeypatch)
    new_out = vector.hybrid_search_knowledge("q", top_k=24, candidates=24)
    old_out = vector.hybrid_search_knowledge("q", top_k=8)
    assert len(new_out) == 24
    assert [h["id"] for h in new_out[:8]] == [h["id"] for h in old_out]


def test_pinned_pool_reads_true_rank_of_a_hit(monkeypatch):
    """诊断用途：能读到某条命中在生产池子里的真实名次（此处 dense 第 30 名）。"""
    import src.adapters.vector as vector

    monkeypatch.setattr(vector, "semantic_search_knowledge",
                        lambda query, top_k=1, tech=None: _fake_hits("d", top_k))
    monkeypatch.setattr(vector, "keyword_search_knowledge",
                        lambda query, top_k=1, tech=None: [])

    out = vector.hybrid_search_knowledge("q", top_k=40, candidates=40)
    ids = [h["id"] for h in out]
    assert len(ids) == 40
    assert ids.index("d30") == 30
