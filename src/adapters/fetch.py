"""抓取基础设施：Firecrawl 网页转 Markdown。

自 tools.py 迁出：fetch_tool；Stage 4 benchmark 后新增 fetch_many（并发抓取，按输入顺序归并）。
"""

import time
from concurrent.futures import ThreadPoolExecutor

from firecrawl import FirecrawlApp

from ..config import config


def fetch_tool(url: str, max_chars: int = 0, timeout: float | None = None) -> dict:
    """使用 Firecrawl 抓取网页内容为 Markdown。

    Args:
        url: 目标网页 URL
        max_chars: 截取的最大字符数；0 表示使用配置的 MAX_FETCH_CHARS
        timeout: 单次抓取超时上限（秒）；None 表示不额外约束（保持历史行为）。
            并发抓取 fetch_many 会显式传入 config.FETCH_TIMEOUT_SECONDS。

    Returns:
        dict: {"url": str, "markdown": str, "title": str}
    """
    limit = max_chars or config.MAX_FETCH_CHARS
    kwargs = {"api_key": config.FIRECRAWL_API_KEY}
    if timeout is not None:
        kwargs["timeout"] = timeout
    app = FirecrawlApp(**kwargs)
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


def fetch_many(urls: list[str], max_workers: int | None = None,
               timeout: float | None = None) -> list[dict]:
    """并发抓取多个 URL，按输入顺序返回结果；单个失败/超时记空结果，不拖垮整批。

    Firecrawl 延迟是网络瓶颈，串行抓取是 collect 耗时的主因（Stage 4 benchmark 实测
    5 页串行占 collect 总耗时约 80%）。这里用线程池并行，结果按输入顺序归并，保持
    输出确定性；墙钟时间用共享 deadline 封顶，避免「N 个全超时」叠加成 N 倍耗时。

    注：超时后底层 Firecrawl 请求线程在后台继续跑（SDK 自带 timeout 兜底），本函数
    不再等待；collect 是短时批量操作，可接受。GitHub star 查询属管道内部质量预筛，不计入。

    Args:
        urls: 目标 URL 列表
        max_workers: 并发线程数上限；None 用 config.FETCH_MAX_WORKERS
        timeout: 整体墙钟超时上限（秒）；None 用 config.FETCH_TIMEOUT_SECONDS

    Returns:
        与 urls 等长、按原顺序的 dict 列表；失败的条目为
        {"url": str, "markdown": "", "title": "", "error": str}。
    """
    if not urls:
        return []
    workers = min(max_workers or config.FETCH_MAX_WORKERS, len(urls))
    deadline = time.monotonic() + (timeout if timeout is not None else config.FETCH_TIMEOUT_SECONDS)

    def _one(u: str) -> dict:
        try:
            return fetch_tool(u, timeout=config.FETCH_TIMEOUT_SECONDS)
        except Exception as e:  # fetch_tool 通常不抛；兜底防意外
            return {"url": u, "markdown": "", "title": "", "error": str(e)}

    def _empty(u: str, reason: str) -> dict:
        return {"url": u, "markdown": "", "title": "", "error": reason}

    results: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(_one, u) for u in urls]
        for idx, fut in enumerate(futures):
            # 已完成的直接取结果（不能因前面慢请求耗尽 deadline 而误杀已完成项）
            if fut.done():
                try:
                    results.append(fut.result())
                except Exception:
                    results.append(_empty(urls[idx], "error"))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(_empty(urls[idx], "timeout"))
                continue
            try:
                results.append(fut.result(timeout=remaining))
            except Exception:
                results.append(_empty(urls[idx], "timeout"))
    finally:
        pool.shutdown(wait=False)  # 不等超时线程，墙钟被封顶
    return results
