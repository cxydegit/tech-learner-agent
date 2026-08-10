"""搜索基础设施：Tavily 网页搜索。

自 tools.py 迁出：search_tool。
"""

from tavily import TavilyClient

from ..config import config


def search_tool(query: str, max_results: int = 10) -> dict:
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
