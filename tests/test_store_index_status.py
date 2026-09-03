"""索引失败状态化 + 对账补缺失的单测（零网络）。

背景：`_update_rag_index` 曾用 `except: pass` 静默吞掉索引失败，
4 篇笔记「保存成功但检索不到」，缺口留存。本组测试锁住三个行为：
1. 索引失败时笔记仍写盘成功，且返回值带 index_ok=False + 原因（对调用方可见）；
2. 瞬时失败立即重试一次（重试成功则 index_ok=True）；
3. 对账补缺失：磁盘有而索引无的文件被补齐，孤儿分块被删除，/ask 路径限量。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_store_index_status.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.adapters.store as store_mod
import src.adapters.vector as vector_mod
from src.adapters.store import _update_rag_index, persist_note
from src.config import config

# ============ _update_rag_index：状态化 + 重试 ============

def _no_sleep():
    monkeypatch_target = store_mod.time
    return monkeypatch_target


def test_index_success_returns_ok(monkeypatch):
    """索引成功 → index_ok=True，只调一次。"""
    calls = []
    monkeypatch.setattr(vector_mod, "index_paths",
                        lambda paths, force=False: calls.append(len(paths)) or {"indexed": 1, "errors": []})
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    out = _update_rag_index(Path("knowledge/x/2026-01-01-t.md"))
    assert out == {"index_ok": True}
    assert calls == [1]


def test_index_transient_failure_retries_once(monkeypatch):
    """第一次失败（抛异常）→ 立即重试一次 → 第二次成功 → index_ok=True。"""
    calls = {"n": 0}

    def flaky(paths, force=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return {"indexed": 1, "errors": []}

    slept = []
    monkeypatch.setattr(vector_mod, "index_paths", flaky)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: slept.append(s))
    out = _update_rag_index(Path("knowledge/x/t.md"))
    assert out == {"index_ok": True}
    assert calls["n"] == 2
    assert slept == [store_mod._INDEX_RETRY_DELAY_SECONDS]


def test_index_persistent_failure_reports(monkeypatch):
    """两次都失败 → index_ok=False + 原因（不再静默）。"""
    monkeypatch.setattr(vector_mod, "index_paths",
                        lambda paths, force=False: (_ for _ in ()).throw(RuntimeError("api down")))
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    out = _update_rag_index(Path("knowledge/x/t.md"))
    assert out["index_ok"] is False
    assert "api down" in out["index_error"]


def test_index_errors_list_reported(monkeypatch):
    """index_paths 对单文件失败不抛异常而是记进 errors 列表——必须被检查到。"""
    monkeypatch.setattr(vector_mod, "index_paths",
                        lambda paths, force=False: {"indexed": 0, "errors": ["x.md: embedding boom"]})
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    out = _update_rag_index(Path("knowledge/x/t.md"))
    assert out["index_ok"] is False
    assert "embedding boom" in out["index_error"]


# ============ persist_note：失败不阻断写盘，状态并入返回 ============

def test_persist_note_new_succeeds_even_when_index_fails(monkeypatch, tmp_path):
    """索引失败：笔记文件照常写盘 + 返回值带 index_ok=False（8-19 事故的回归守卫）。"""
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(vector_mod, "index_paths",
                        lambda paths, force=False: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    r = persist_note("pytest-tech", "索引失败守卫", "正文内容", ["测试"])
    assert r["action"] == "new"
    assert r["index_ok"] is False
    assert "boom" in r["index_error"]
    assert (tmp_path / r["path"]).is_file()          # 笔记本身已写盘
    assert (tmp_path / "INDEX.md").is_file()          # INDEX.md 照常更新


def test_persist_note_merge_carries_index_status(monkeypatch, tmp_path):
    """合并路径同样带 index 状态。"""
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(vector_mod, "index_paths",
                        lambda paths, force=False: {"indexed": 1, "errors": []})
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    # 先造一篇已有笔记
    first = persist_note("pytest-tech", "旧笔记", "旧正文", [])
    assert first["index_ok"] is True
    merged = persist_note("pytest-tech", "旧笔记", "新正文", ["补充"], replace_path=first["path"])
    assert merged["action"] == "merged"
    assert merged["index_ok"] is True


# ============ _reconcile_index：删孤儿 + 补缺失 ============

class _FakeCollection:
    def __init__(self, ids, metas):
        self._ids, self._metas = ids, metas
        self.deleted = None

    def get(self, include=None, where=None):
        return {"ids": list(self._ids), "metadatas": list(self._metas)}

    def delete(self, ids=None, where=None):
        self.deleted = ids


def test_reconcile_deletes_orphans_and_backfills_missing(monkeypatch):
    """孤儿分块被删；磁盘有而索引无的文件被补齐。"""
    monkeypatch.setattr(config, "RAG_RECONCILE", True)
    fake = _FakeCollection(ids=["gone.md::0"], metas=[{"path": "knowledge/gone.md"}])
    monkeypatch.setattr(vector_mod, "get_collection", lambda: fake)
    monkeypatch.setattr(vector_mod, "_discover_files",
                        lambda: [config.BASE_DIR / "knowledge/a.md", config.BASE_DIR / "materials/b.md"])
    backfilled_files = []
    monkeypatch.setattr(vector_mod, "_index_single_file",
                        lambda col, p, force: backfilled_files.append(p) or ("indexed", None))

    out = vector_mod._reconcile_index(backfill=True)
    assert out == {"orphans": 1, "backfilled": 2}
    assert fake.deleted == ["gone.md::0"]
    assert [p.name for p in backfilled_files] == ["a.md", "b.md"]   # sorted 稳定


def test_reconcile_backfill_respects_cap(monkeypatch):
    """/ask 路径的 max_backfill 限量生效。"""
    monkeypatch.setattr(config, "RAG_RECONCILE", True)
    fake = _FakeCollection(ids=[], metas=[])
    monkeypatch.setattr(vector_mod, "get_collection", lambda: fake)
    monkeypatch.setattr(vector_mod, "_discover_files",
                        lambda: [config.BASE_DIR / f"knowledge/{i}.md" for i in range(5)])
    seen = []
    monkeypatch.setattr(vector_mod, "_index_single_file",
                        lambda col, p, force: seen.append(p) or ("indexed", None))
    out = vector_mod._reconcile_index(backfill=True, max_backfill=2)
    assert out == {"orphans": 0, "backfilled": 2}
    assert len(seen) == 2


def test_reconcile_disabled_returns_zero(monkeypatch):
    """RAG_RECONCILE 关闭时不做任何事。"""
    monkeypatch.setattr(config, "RAG_RECONCILE", False)
    out = vector_mod._reconcile_index(backfill=True)
    assert out == {"orphans": 0, "backfilled": 0}
