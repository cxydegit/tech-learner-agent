"""GitHub 外部 I/O 适配器：查询仓库 star 数（质量预筛用）。

标准库 ``urllib`` 单次 GET，可选 ``GITHUB_TOKEN``（5000 次/时）。
任何失败都返回 None 优雅降级——不设 token 时调用方（collect_pipeline）直接跳过，
不会发起网络请求。
"""

import json
import re
import urllib.request

_GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([^/]+)/([^/?#]+)")


def fetch_star_count(url: str, token: str | None = None) -> int | None:
    """查询 GitHub 仓库的 star 数。

    Args:
        url: github.com 仓库链接（如 https://github.com/langchain-ai/langgraph）
        token: 可选 GITHUB_TOKEN（提高速率限制）；None 也能匿名查询

    Returns:
        stargazers_count；非 github 链接 / 请求失败 / 仓库不存在 → None（优雅降级）
    """
    m = _GITHUB_RE.match(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)

    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("stargazers_count")
    except Exception:  # noqa: BLE001 —— 网络/JSON/404 一律降级为 None
        return None
