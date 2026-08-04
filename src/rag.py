"""RAG 知识库：Chroma 持久化索引 + 语义检索。

索引三个来源目录（knowledge/ + materials/ + reports/）的 Markdown 文档，
分块嵌入后存入本地 Chroma（.chroma/）。对外提供：

- ``index_documents()`` / ``index_paths()``：全量 / 增量建立索引（含变更检测，避免重复计费）
- ``semantic_search()``：通用语义检索（可用 where 过滤 source / tech）
- ``semantic_search_knowledge()``：笔记语义去重召回（限定 knowledge 源、可选限定技术领域）
- ``check_read_cache()``：read 历史召回（命中已有解读则提示复用）
"""

from __future__ import annotations

import re
from hashlib import sha1
from pathlib import Path
from typing import Any

import chromadb

from .config import config
from .embedding import DashScopeEmbeddingFunction

_COLLECTION_NAME = "knowledge_base"

# 复用同一个 embedding 函数实例，保证 Chroma 序列化与编码一致
_embedding_function = DashScopeEmbeddingFunction()
_client: Any = None
_collection: Any = None


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
# 文档分块
# ============================================================

def chunk_text(text: str, chunk_size: int = 0, overlap: int = 0) -> list[str]:
    """把 Markdown 文本切成带重叠的分块。

    以段落（空行分隔）为单位聚合成块，每块不超过 chunk_size 字符；
    块与块之间保留 overlap 字符的重叠，避免语义断裂。

    Args:
        text: 文档全文
        chunk_size: 单块最大字符数；0 用 config.RAG_CHUNK_SIZE
        overlap: 相邻块重叠字符数；0 用 config.RAG_CHUNK_OVERLAP

    Returns:
        list[str]: 分块列表（保留原文本顺序）
    """
    chunk_size = chunk_size or config.RAG_CHUNK_SIZE
    overlap = overlap or config.RAG_CHUNK_OVERLAP

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        # 单个段落超长：按字符硬切（保留 overlap）
        if len(p) > chunk_size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(p), chunk_size - overlap):
                chunks.append(p[i : i + chunk_size])
            continue
        # 当前块放不下该段落：先收走，下一块从尾部 overlap 续接
        if buf and len(buf) + len(p) + 1 > chunk_size:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        buf = (buf + "\n\n" + p).strip() if buf else p
    if buf:
        chunks.append(buf)
    return chunks


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


def index_paths(paths: list[Path]) -> dict:
    """增量索引指定文件；内容未变化则跳过（避免重复嵌入计费）。

    Args:
        paths: 待索引的 Markdown 文件列表

    Returns:
        {"indexed": int, "skipped": int, "errors": list[str]}
    """
    collection = get_collection()
    indexed = skipped = 0
    errors: list[str] = []

    for path in paths:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            continue
        rel = path.relative_to(config.BASE_DIR).as_posix()
        digest = sha1(content.encode("utf-8")).hexdigest()

        # 变更检测：该文件已有同 hash 的分块则跳过，避免重复计费
        existing = collection.get(where={"path": rel}, include=["metadatas"])
        ids = existing.get("ids", [])
        metas = existing.get("metadatas", []) or []
        if any(m.get("content_hash") == digest for m in metas):
            skipped += 1
            continue

        # 内容变更或新文件：删旧块 → 重新切块嵌入
        if ids:
            collection.delete(ids=ids)
        chunks = chunk_text(content)
        if not chunks:
            skipped += 1
            continue
        meta = _doc_metadata(path, content)
        doc_ids = [f"{rel}::{i}" for i in range(len(chunks))]
        doc_metas = [{**meta, "content_hash": digest, "chunk": i} for i in range(len(chunks))]
        try:
            collection.add(ids=doc_ids, documents=chunks, metadatas=doc_metas)
            indexed += 1
        except Exception as e:  # noqa: BLE001 —— 单文件失败不应中断全量索引
            errors.append(f"{rel}: {e}")

    return {"indexed": indexed, "skipped": skipped, "errors": errors}


def index_documents(paths: list[Path] | None = None) -> dict:
    """建立（重建 / 增量更新）RAG 索引。

    Args:
        paths: 待索引的文件；None 时自动扫描 knowledge/ + materials/ + reports/

    Returns:
        {"indexed": int, "skipped": int, "errors": list[str]}
    """
    files = paths if paths is not None else _discover_files()
    return index_paths(files)


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
    except Exception:  # noqa: BLE001 —— RAG 不可用时静默降级
        pass
    return None


# ============================================================
# 命令行入口：python -m src.rag
# ============================================================

if __name__ == "__main__":
    result = index_documents()
    print(f"索引完成：新增 {result['indexed']} 个文件，跳过 {result['skipped']} 个未变化文件")
    if result["errors"]:
        print("错误：")
        for e in result["errors"]:
            print(f"  - {e}")
