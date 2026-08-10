"""抓取基础设施：Firecrawl 网页转 Markdown。

自 tools.py 迁出：fetch_tool。
"""

from firecrawl import FirecrawlApp

from ..config import config


def fetch_tool(url: str, max_chars: int = 0) -> dict:
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
