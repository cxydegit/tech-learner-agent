"""RAG 向量库基础设施：Chroma 持久化索引 + 语义检索。

自 rag.py 迁出（分块部分在 domain/chunking.py）。索引三个来源目录
（knowledge/ + materials/ + reports/）的 Markdown 文档，分块嵌入后存入
本地 Chroma（.chroma/）。对外提供：

- ``index_documents()`` / ``index_paths()``：全量 / 增量建立索引（含变更检测，避免重复计费）
- ``semantic_search()``：通用语义检索（可用 where 过滤 source / tech）
- ``semantic_search_knowledge()``：笔记语义去重召回（限定 knowledge 源、可选限定技术领域）
- ``keyword_search_knowledge()``：词法 BM25 召回（补纯 dense 漏精确词匹配）
- ``hybrid_search_knowledge()``：dense + BM25 → RRF 融合召回（Q&A 默认走这里）
- ``check_read_cache()``：read 历史召回（命中已有解读则提示复用）
- ``reconcile_orphans()``：索引对账（删孤儿 + 补缺失；index_paths 末尾自动跑，
  /ask 节流跑且单次补齐限量）

⚠️ 本模块 import 时会加载 chromadb，必须保持 lazy 导入（见 CLI / store 的调用点），
否则 `import src.cli` 会被拉起 chromadb。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import chromadb

from ..config import config
from ..domain.chunking import _content_digest, chunk_markdown
from ..domain.hybrid import build_bm25, lexical_rerank, rrf_fuse
from .embedding import DashScopeEmbeddingFunction

_COLLECTION_NAME = "knowledge_base"

# 复用同一个 embedding 函数实例，保证 Chroma 序列化与编码一致
_embedding_function = DashScopeEmbeddingFunction()
_client: Any = None
_collection: Any = None
# /ask 惰性对账节流：距上次对账 < RAG_RECONCILE_INTERVAL 秒则跳过（避免每次提问都扫全库）
_last_reconcile_at: float = 0.0


# ============================================================
# Chroma 客户端与集合管理
# ============================================================

def get_client() -> Any:
    """懒加载 Chroma PersistentClient（本地持久化到 .chroma/）。"""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    return _client


def get_collection() -> Any:
    """获取（或创建）知识库集合。

    使用 cosine 度量：distance = 1 - cosine_similarity，越小越相似。
    """
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=_embedding_function,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


# ============================================================
# 索引
# ============================================================

def _discover_files() -> list[Path]:
    """收集需要索引的 Markdown 文件：knowledge/ + materials/ + reports/。"""
    files: list[Path] = []
    for d in (config.KNOWLEDGE_DIR, config.MATERIALS_DIR, config.REPORTS_DIR):
        if d.exists():
            files.extend(p for p in d.rglob("*.md") if p.is_file())
    # 排除自动生成的 INDEX.md（无学习内容）
    files = [p for p in files if p.name != "INDEX.md"]
    return sorted(files)


def _doc_metadata(path: Path, content: str) -> dict[str, str]:
    """按来源提取文档元数据（用于 Chroma where 过滤与命中展示）。"""
    rel = path.relative_to(config.BASE_DIR).as_posix()
    source = rel.split("/", 1)[0]  # knowledge / materials / reports
    meta = {"path": rel, "source": source}

    if source == "knowledge":
        # 路径形如 knowledge/<tech>/<date>-<topic>.md
        parts = rel.split("/")
        meta["tech"] = parts[1] if len(parts) > 1 else ""
        # topic：去掉 YYYY-MM-DD- 日期前缀
        name = path.stem
        m = re.match(r"\d{4}-\d{2}-\d{2}-(.*)", name)
        meta["topic"] = m.group(1).replace("-", " ") if m else name
    elif source == "reports":
        # 报告头部有 "> 原文链接：<url>"，提取用于 read 缓存命中
        m = re.search(r"原文链接[：:]\s*(\S+)", content[:2000])
        meta["url"] = m.group(1).strip() if m else ""

    return meta


def _index_single_file(collection: Any, path: Path, force: bool) -> tuple[str, str | None]:
    """索引单个文件（index_paths 与对账补缺失共用；错误不抛出，由调用方决定呈现方式）。

    Returns:
        ("indexed" | "skipped" | "error", 错误消息或 None)。
        "indexed"=已嵌入新块；"skipped"=不存在/空内容/内容未变（省计费）；"error"=嵌入失败。
    """
    if not path.exists():
        return "skipped", None
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        return "skipped", None
    rel = path.relative_to(config.BASE_DIR).as_posix()
    digest = _content_digest(content)

    # 变更检测：该文件已有同 hash 的分块则跳过，避免重复计费（force 时忽略）
    existing = collection.get(where={"path": rel}, include=["metadatas"])
    ids = existing.get("ids", [])
    metas = existing.get("metadatas", []) or []
    if not force and any(m.get("content_hash") == digest for m in metas):
        return "skipped", None

    # 内容变更或新文件：删旧块 → 重新切块嵌入
    if ids:
        collection.delete(ids=ids)
    chunks = chunk_markdown(content)
    if not chunks:
        return "skipped", None
    meta = _doc_metadata(path, content)
    doc_ids = [f"{rel}::{i}" for i in range(len(chunks))]
    doc_metas = [
        {**meta, "content_hash": digest, "chunk": i, "section": c.section}
        for i, c in enumerate(chunks)
    ]
    try:
        collection.add(ids=doc_ids, documents=[c.text for c in chunks], metadatas=doc_metas)
        return "indexed", None
    except Exception as e:  # noqa: BLE001 —— 单文件失败不应中断全量索引
        return "error", f"{rel}: {e}"


def index_paths(paths: list[Path], force: bool = False) -> dict:
    """增量索引指定文件；内容未变化则跳过（避免重复嵌入计费）。

    Args:
        paths: 待索引的 Markdown 文件列表
        force: 忽略变更检测，强制重新切块嵌入（分块器升级 / 参数调整后需要）

    Returns:
        {"indexed": int, "skipped": int, "orphans": int, "backfilled": int,
         "errors": list[str], "warn": str}
        ``orphans`` 为对账清理的孤儿分块数；``backfilled`` 为对账补齐的
        缺失文件数（磁盘有而索引无的文件——写路径自愈，不限个数）。
        ``warn`` 为孤儿删除被安全闸拦截时的提示（空串 = 正常执行）。
    """
    collection = get_collection()
    indexed = skipped = 0
    errors: list[str] = []

    for path in paths:
        status, err = _index_single_file(collection, path, force)
        if status == "indexed":
            indexed += 1
        elif status == "skipped":
            skipped += 1
        if err:
            errors.append(err)

    # 对账：以「磁盘现状」为准修齐索引——删孤儿 + 补缺失。手动 index /
    # 每次写笔记都会经过这里，缺口随下一次自然写入自愈，无需定期跑 index。
    rec = _reconcile_index(backfill=True)
    return {"indexed": indexed, "skipped": skipped,
            "orphans": rec["orphans"], "backfilled": rec["backfilled"],
            "errors": errors, "warn": rec.get("warn", "")}


def index_documents(paths: list[Path] | None = None, force: bool = False) -> dict:
    """建立（重建 / 增量更新）RAG 索引。

    Args:
        paths: 待索引的文件；None 时自动扫描 knowledge/ + materials/ + reports/
        force: 忽略变更检测，强制重新切块嵌入

    Returns:
        {"indexed": int, "skipped": int, "orphans": int, "errors": list[str]}
    """
    files = paths if paths is not None else _discover_files()
    return index_paths(files, force=force)


# ============================================================
# 索引对账（孤儿分块清理）
# ============================================================

def _reconcile_index(*, backfill: bool, max_backfill: int | None = None) -> dict:
    """对账：以「磁盘现状」为准修齐索引——删孤儿 + 补缺失。

    - 删孤儿：磁盘已不存在文件的残留分块，/ask 会召回并引用已不存在的笔记；
    - 补缺失：磁盘有而索引无任何分块的文件。笔记写盘成功但
      索引静默失败时，缺口从这里自愈——digest 变更检测保证补齐幂等、不重复计费。

    Args:
        backfill: 是否补缺失（只读路径可关或限个数）
        max_backfill: 单次补齐文件数上限（None 不限）。/ask 节流路径传 config 上限，
            避免提问时突发多次 embedding 调用拖延迟；写路径（index_paths）不限。

    Returns:
        {"orphans": 删除的孤儿分块数, "backfilled": 补齐的文件数,
         "warn": 孤儿删除被安全闸拦截时的警告信息（空串 = 正常执行）}

    受 config.RAG_RECONCILE 控制（默认开）；Chroma 异常 / 索引未建时静默降级为 0。

    安全闸（孤儿误删防线）：孤儿删除以「磁盘现状」为准，但磁盘扫描可能因
    目录被切换 / rglob 异常 / 空目录运行而暂时看不到文件——此时若照常删孤儿，
    会把整库分块（尤其 knowledge 笔记）全当孤儿清空。防护：当磁盘可见文件数
    与索引已跟踪文件数之比低于 RAG_RECONCILE_MIN_DISK_RATIO 时，判定为扫描异常，
    跳过删孤儿（只补缺失，缺口由写路径自愈），避免「全库消失」类事故。
    """
    if not config.RAG_RECONCILE:
        return {"orphans": 0, "backfilled": 0, "warn": ""}
    try:
        res = get_collection().get(include=["metadatas"])
    except Exception:  # noqa: BLE001 —— 索引未建 / Chroma 异常时静默降级
        return {"orphans": 0, "backfilled": 0, "warn": ""}
    ids = res.get("ids", [])
    metas = res.get("metadatas", []) or []
    disk_files = _discover_files()
    disk_paths = {p.relative_to(config.BASE_DIR).as_posix() for p in disk_files}
    indexed_paths = {m.get("path") for m in metas}

    # 1) 删孤儿：path 不在磁盘的分块。安全闸：磁盘可见文件数断崖式低于索引
    #    跟踪数（而非接近全量）→ 疑似扫描异常，拒绝删孤儿，避免误清整库。
    orphans = 0
    orphan_ids = [doc_id for doc_id, m in zip(ids, metas) if m.get("path") not in disk_paths]
    warn = ""
    min_ratio = config.RAG_RECONCILE_MIN_DISK_RATIO
    if orphan_ids and disk_paths and min_ratio > 0:
        ratio = len(disk_paths) / max(len(indexed_paths), 1)
        if ratio < min_ratio:
            warn = (f"磁盘可见文件 {len(disk_paths)} 个 << 索引跟踪 {len(indexed_paths)} 个 "
                    f"(ratio {ratio:.2f} < {min_ratio})，疑似扫描异常，已跳过 {len(orphan_ids)} 个"
                    "孤儿分块的删除，仅补缺失（检查是否在空/临时目录下运行过 index）")
            orphan_ids = []
    if orphan_ids:
        try:
            get_collection().delete(ids=orphan_ids)
            orphans = len(orphan_ids)
        except Exception:  # noqa: BLE001 —— 删除失败不致命，下次对账再试
            orphans = 0

    # 2) 补缺失：磁盘有而索引无分块的文件（空文件补不进会随下次对账重试，无副作用）
    backfilled = 0
    if backfill:
        missing = sorted(disk_paths - indexed_paths)
        if max_backfill is not None:
            missing = missing[:max_backfill]
        collection = get_collection()
        for rel in missing:
            status, _err = _index_single_file(collection, config.BASE_DIR / rel, force=False)
            if status == "indexed":
                backfilled += 1
    return {"orphans": orphans, "backfilled": backfilled, "warn": warn}


def reconcile_orphans(force: bool = False) -> dict:
    """带节流的对账入口（/ask 惰性调用）：RAG_RECONCILE_INTERVAL 秒内只跑一次。

    index_paths 末尾的对账不走节流（写入路径自愈、补齐不限量）；此处节流避免每次
    提问都扫全库，且单次补齐文件数受 RAG_RECONCILE_BACKFILL_MAX 限制（提问时
    突发大量 embedding 调用会拖延迟）。force=True 跳过节流强制对账。

    Returns:
        {"orphans": int, "backfilled": int, "warn": str}（节流跳过 / 未启用时 warn 为空）
    """
    global _last_reconcile_at
    now = time.time()
    if not force and now - _last_reconcile_at < config.RAG_RECONCILE_INTERVAL:
        return {"orphans": 0, "backfilled": 0, "warn": ""}
    _last_reconcile_at = now
    rec = _reconcile_index(backfill=True, max_backfill=config.RAG_RECONCILE_BACKFILL_MAX)
    if rec.get("warn"):
        import warnings
        warnings.warn(rec["warn"], stacklevel=2)
    return rec


# ============================================================
# 语义检索
# ============================================================

def _where_and(conditions: dict) -> dict | None:
    """把多个等值条件组装成 Chroma 合法的 where 子句。

    Chroma 1.x 的 where 只接受单操作符字典；多条件必须显式用 $and 包裹。
    """
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions
    return {"$and": [{k: v} for k, v in conditions.items()]}


def _search(query: str, top_k: int, where: dict | None) -> list[dict]:
    """执行一次语义检索，返回规范化的命中列表。"""
    if not query.strip():
        return []
    try:
        result = get_collection().query(
            query_texts=[query],
            n_results=max(top_k, 1),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception:  # noqa: BLE001 —— 索引为空 / Chroma 异常时优雅降级
        return []

    ids = result.get("ids", [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]

    hits: list[dict] = []
    for i, doc_id in enumerate(ids):
        m = metas[i] if i < len(metas) else {}
        hits.append({
            "id": doc_id,
            "path": m.get("path", ""),
            "source": m.get("source", ""),
            "tech": m.get("tech", ""),
            "topic": m.get("topic", ""),
            "url": m.get("url", ""),
            "similarity": 1.0 - dists[i] if i < len(dists) else 0.0,
            "document": docs[i] if i < len(docs) else "",
        })
    return hits


def semantic_search(query: str, top_k: int = 0, where: dict | None = None) -> list[dict]:
    """通用语义检索。

    Args:
        query: 查询文本
        top_k: 返回条数；0 用 config.RAG_TOP_K
        where: Chroma where 过滤条件（如 {"source": "knowledge", "tech": "fastapi"}）

    Returns:
        [{id, path, source, tech, url, similarity, document}, ...]
    """
    return _search(query, top_k or config.RAG_TOP_K, where)


def semantic_search_knowledge(query: str, top_k: int = 1, tech: str | None = None) -> list[dict]:
    """在知识笔记（knowledge 源）中检索，用于 note 语义去重。

    Args:
        query: 知识点标题
        top_k: 返回条数
        tech: 可选，限定技术领域（需传 sanitize 后的目录名）
    """
    where = _where_and({"source": "knowledge", **({"tech": tech} if tech else {})})
    return _search(query, top_k, where)


# ============================================================
# 词法 BM25 检索 + 混合检索
# ============================================================

def keyword_search_knowledge(query: str, top_k: int = 1, tech: str | None = None) -> list[dict]:
    """词法 BM25 检索：补纯 dense 漏「精确词匹配」的弱点（搜 RedisJSON/FT.SEARCH 等专有名词）。

    collection 全量取 knowledge chunk（可选限定 tech）→ 内存 BM25 打分 top-k。
    当前知识库规模小，全量内存打分可行；库变大后再考虑持久化 BM25 索引。

    返回结构与 semantic_search_knowledge 一致；``similarity`` 为归一化 BM25 分（top=1.0）。
    """
    where = _where_and({"source": "knowledge", **({"tech": tech} if tech else {})})
    try:
        res = get_collection().get(where=where, include=["documents", "metadatas"])
    except Exception:  # noqa: BLE001 —— Chroma 异常时优雅降级为空
        return []
    ids = res.get("ids", [])
    metas = res.get("metadatas", []) or []
    docs = res.get("documents", []) or []
    if not ids or not (query or "").strip():
        return []

    scores = build_bm25(docs).score(query)
    # 只保留 BM25 分 > 0 的块（与查询至少共享一个词项）。0 分块是纯填充，
    # 若放进 RRF 会按"填充排名"白拿融合分，把无关笔记抬高（教训：RoPE 查询里
    # rag-架构模式 靠语义榜第 1 + 0 分填充的关键词榜"第 12 名"拿到 0.0303 排第一，
    # 真正的 transformer 笔记只有关键词榜第 1 的 0.0164，掉出前 8）。
    order = [i for i, s in enumerate(scores) if s > 0]
    order.sort(key=lambda i: scores[i], reverse=True)
    order = order[:top_k]
    if not order:
        return []
    max_score = scores[order[0]]
    hits: list[dict] = []
    for i in order:
        m = metas[i] if i < len(metas) else {}
        hits.append({
            "id": ids[i],
            "path": m.get("path", ""),
            "source": m.get("source", ""),
            "tech": m.get("tech", ""),
            "topic": m.get("topic", ""),
            "url": m.get("url", ""),
            "similarity": scores[i] / max_score if max_score > 0 else 0.0,
            "document": docs[i] if i < len(docs) else "",
        })
    return hits


def hybrid_search_knowledge(query: str, top_k: int = 1, tech: str | None = None) -> list[dict]:
    """混合检索：dense + BM25 → RRF 融合。

    每路取 ``top_k*3`` 候选再融合后截断到 top_k（候选充足融合才有意义）；
    dense 空回退 keyword、keyword 全零回退 dense（两路内部已各自吞掉 Chroma 异常）。
    返回条目的 ``similarity`` 为归一化 RRF 分（top=1.0），``dense_similarity`` 保留
    原始余弦给相关度阈值等下游。
    """
    cand = max(top_k * 3, 8)
    dense = semantic_search_knowledge(query, cand, tech)
    sparse = keyword_search_knowledge(query, cand, tech)
    if not dense:
        return sparse[:top_k]
    if not sparse:
        return dense[:top_k]
    fused = rrf_fuse(dense, sparse, config.QA_RRF_K)
    # 词法一致性软重排：仅当查询是「罕见词型」（BM25 正命中集中在 ≤ N 篇笔记）
    # 才启用，纠正 dense 对裸缩写/专有名词的零词法重合噪声（RoPE → rag-架构模式
    # 语义第1但通篇无 RoPE 的案例）。概念查询 BM25 命中散落各篇，不重排避免误伤语义排序
    # （实测：命中≤3 篇时重排只改进/持平；≥4 篇时会造成回退）。
    if config.QA_RERANK_LEXICAL and len({h.get("path") for h in sparse}) <= config.QA_RERANK_MIN_HITS:
        fused = lexical_rerank(fused, query, w=config.QA_RERANK_LEXICAL_W)
    return fused[:top_k]


# ============================================================
# read 历史召回
# ============================================================

def _url_path_key(url: str) -> str:
    """从 URL 中提取有语义的路径片段，作为语义检索查询词。

    例如 https://www.databricks.com/blog/what-is-retrieval-augmented-generation
    -> what-is-retrieval-augmented-generation
    """
    m = re.search(r"//[^/]+/([^?#]+)", url)
    path = m.group(1) if m else ""
    segs = [s for s in path.split("/") if s and not s.startswith(("_", "."))]
    return segs[-1] if segs else url


def check_read_cache(url: str, threshold: float = 0) -> dict | None:
    """read 历史召回：判断该 URL 是否已有解读报告。

    优先用报告的「原文链接」元数据精确匹配；未命中再用 URL 路径片段做语义检索。

    Returns:
        命中则返回 {"path", "similarity", "content"}，否则 None
    """
    threshold = threshold or config.RAG_READ_THRESHOLD
    try:
        # 1) 精确匹配：已有解读报告记录了同样的原文链接
        res = get_collection().get(
            where=_where_and({"source": "reports", "url": url}),
            include=["documents", "metadatas"],
        )
        if res.get("ids"):
            path = (res["metadatas"] or [{}])[0].get("path", "")
            return {"path": path, "similarity": 1.0, "content": (res["documents"] or [""])[0]}
        # 2) 语义检索：用 URL 路径片段作查询词
        hits = _search(_url_path_key(url), 1, {"source": "reports"})
        if hits and hits[0]["similarity"] >= threshold:
            h = hits[0]
            return {"path": h["path"], "similarity": h["similarity"], "content": h["document"]}
    except Exception:  # noqa: BLE001, S110 —— RAG 不可用时静默降级
        pass
    return None


# ============================================================
# 命令行入口：python -m src.adapters.vector
# ============================================================

def main() -> None:
    """建立 / 增量更新 RAG 语义索引（CLI 入口）。"""
    import argparse

    parser = argparse.ArgumentParser(description="建立 / 增量更新 RAG 语义索引")
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="忽略变更检测，强制重新切块嵌入（分块器升级 / 参数调整后需要）",
    )
    args = parser.parse_args()

    result = index_documents(force=args.force)
    print(f"索引完成：新增 {result['indexed']} 个文件，跳过 {result['skipped']} 个未变化文件")
    if result.get("backfilled"):
        print(f"对账补齐 {result['backfilled']} 个缺失文件（此前索引失败 / 未索引）")
    if result.get("orphans"):
        print(f"清理孤儿分块 {result['orphans']} 个")
    if result.get("warn"):
        print(f"⚠️ {result['warn']}")
    if result["errors"]:
        print("错误：")
        for e in result["errors"]:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
