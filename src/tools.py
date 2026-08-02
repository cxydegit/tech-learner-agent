"""工具函数封装：搜索、网页抓取、文件操作"""

import json
from pathlib import Path
from typing import Any

from tavily import TavilyClient
from firecrawl import FirecrawlApp

from .config import config


# ============================================================
# 搜索工具
# ============================================================

def search_tool(query: str, max_results: int = 10) -> dict[str, Any]:
    """使用 Tavily 搜索互联网资料。

    Args:
        query: 搜索关键词
        max_results: 最大返回结果数

    Returns:
        dict: {"results": [{title, url, content, score}, ...], "total": int}
    """
    client = TavilyClient(api_key=config.TAVILY_API_KEY)
    response = client.search(
        query=query,
        search_depth="advanced",
        max_results=max_results,
        include_raw_content=False,
    )
    results = []
    for r in response.get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "score": r.get("score", 0.0),
        })
    return {
        "results": results,
        "total": len(results),
    }


# ============================================================
# 网页抓取工具
# ============================================================

def fetch_tool(url: str, max_chars: int = 0) -> dict[str, Any]:
    """使用 Firecrawl 抓取网页内容为 Markdown。

    Args:
        url: 目标网页 URL
        max_chars: 截取的最大字符数；0 表示使用配置的 MAX_FETCH_CHARS

    Returns:
        dict: {"url": str, "markdown": str, "title": str}
    """
    limit = max_chars or config.MAX_FETCH_CHARS
    app = FirecrawlApp(api_key=config.FIRECRAWL_API_KEY)
    response = app.scrape_url(url)

    # handle both response formats
    if hasattr(response, "markdown"):
        markdown = response.markdown or ""
    elif isinstance(response, dict):
        data = response.get("data", response)
        markdown = data.get("markdown", "") if isinstance(data, dict) else ""
    else:
        markdown = ""

    # extract title from metadata if available
    title = ""
    if isinstance(response, dict):
        metadata = response.get("metadata", response.get("data", {}))
        if isinstance(metadata, dict):
            title = metadata.get("title", "")
    elif hasattr(response, "metadata"):
        meta = response.metadata
        title = getattr(meta, "title", "") if meta else ""

    return {
        "url": url,
        "markdown": markdown[:limit],
        "title": title,
        "truncated": len(markdown) > limit,
    }


# ============================================================
# 文件操作工具
# ============================================================

def save_file_tool(path: str, content: str) -> dict[str, Any]:
    """保存内容到本地文件。

    Args:
        path: 相对于项目根目录的文件路径
        content: 文件内容

    Returns:
        dict: {"path": str, "size": int, "success": bool}
    """
    full_path = config.BASE_DIR / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {
        "path": str(full_path),
        "size": len(content),
        "success": True,
    }


def read_file_tool(path: str) -> dict[str, Any]:
    """读取本地文件内容。

    Args:
        path: 相对于项目根目录的文件路径

    Returns:
        dict: {"path": str, "content": str, "success": bool, "error": str}
    """
    full_path = config.BASE_DIR / path
    if not full_path.exists():
        return {"path": str(full_path), "content": "", "success": False, "error": "文件不存在"}
    content = full_path.read_text(encoding="utf-8")
    return {"path": str(full_path), "content": content, "success": True, "error": ""}


def list_files_tool(directory: str = ".") -> dict[str, Any]:
    """列出目录下的文件结构。

    Args:
        directory: 相对于项目根目录的目录路径

    Returns:
        dict: {"files": [{path, size, is_dir}, ...], "total": int}
    """
    full_dir = config.BASE_DIR / directory
    if not full_dir.exists():
        return {"files": [], "total": 0, "error": f"目录不存在: {directory}"}

    files = []
    for p in sorted(full_dir.rglob("*")):
        rel_path = str(p.relative_to(config.BASE_DIR))
        files.append({
            "path": rel_path,
            "size": p.stat().st_size if p.is_file() else 0,
            "is_dir": p.is_dir(),
        })
    return {"files": files, "total": len(files)}


# ============================================================
# 工具注册表（供 Agent 使用）
# ============================================================

TOOL_REGISTRY = {
    "search": {
        "function": search_tool,
        "description": "搜索互联网资料。参数: query (str) — 搜索关键词, max_results (int, 可选) — 最大结果数",
    },
    "fetch": {
        "function": fetch_tool,
        "description": "抓取网页内容为 Markdown。参数: url (str) — 目标网页 URL",
    },
    "save_file": {
        "function": save_file_tool,
        "description": "保存内容到本地文件。参数: path (str) — 相对路径, content (str) — 文件内容",
    },
    "read_file": {
        "function": read_file_tool,
        "description": "读取本地文件。参数: path (str) — 相对路径",
    },
    "list_files": {
        "function": list_files_tool,
        "description": "列出目录结构。参数: directory (str, 可选) — 相对目录路径",
    },
}


def get_tool_descriptions() -> str:
    """生成工具描述文本，注入到系统提示词中。"""
    lines = []
    for name, info in TOOL_REGISTRY.items():
        lines.append(f"- **{name}**: {info['description']}")
    return "\n".join(lines)


def execute_tool(name: str, params: dict[str, Any]) -> str:
    """执行指定工具并返回 JSON 字符串结果。

    Args:
        name: 工具名称
        params: 工具参数

    Returns:
        JSON 字符串格式的结果
    """
    if name not in TOOL_REGISTRY:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

    try:
        func = TOOL_REGISTRY[name]["function"]
        result = func(**params)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)