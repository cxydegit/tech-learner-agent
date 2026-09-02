"""搜索结果质量预筛（纯规则，零 I/O、零框架依赖）。

确定性预筛丢明显垃圾（内容农场 / 低分），高质量结果保留供后续
抓取与 LLM 合成排序。GitHub 星数通过注入的 ``fetch_stars`` 回调完成，函数本身
不做网络——domain 层可独立单测。

评分：``score = domain_bonus + star_bonus + url_bonus``
剔除：命中内容农场 → 剔除；综合分 < min_score → 剔除；其余通过，按分数降序。
"""

from urllib.parse import urlparse


def screen_results(results: list[dict], *, fetch_stars=None, official_domains,
                   platform_domains, content_farms, min_score, star_tiers,
                   domain_bonus_official, domain_bonus_platform, url_bonus_official_docs,
                   url_penalty_blog, url_penalty_source):
    """对搜索结果做机械预筛。

    Args:
        results: 搜索结果，[{url, title, content}, ...]
        fetch_stars: 可选回调 (url) -> int|None——GitHub 星数；无 token 或非 github 链接
            应返回 None（调用方保证无 token 时不触发网络）。None 表示不做星数查询。
        official_domains / platform_domains / content_farms: 域名集合（来自 config）
        min_score: 剔除阈值（综合分 < 该值剔除）
        star_tiers: [(最小星数, 加分)] 降序判定
        domain_bonus_official / domain_bonus_platform: 官方/平台域名加分
        url_bonus_official_docs: 官方文档路径加分（docs./learn./reference.）
        url_penalty_blog: 个人博客降权（*.github.io、/blog/、/posts/，负值）
        url_penalty_source: 源码直链降权（/blob/、raw.，负值）

    Returns:
        (kept, excluded)
        kept: 通过的结果，附加 ``score`` 字段，按分数降序
        excluded: [{url, reason}]，按剔除原因（供报告"已排除 N 条"透明汇报）
    """
    kept: list[dict] = []
    excluded: list[dict] = []
    for r in results:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        host = _domain_of(url)

        # 内容农场 → 直接剔除（不评分子）
        if _in_domains(host, content_farms):
            excluded.append({"url": url, "reason": "内容农场"})
            continue

        score = 0
        # 域名加分：github.com 不走域名加分、走星数加分
        if _in_domains(host, official_domains):
            score += domain_bonus_official
        elif _in_domains(host, platform_domains) and not _match_domain(host, "github.com"):
            score += domain_bonus_platform
        # URL 特征加减分
        score += _url_bonus(url, url_bonus_official_docs, url_penalty_blog, url_penalty_source)
        # GitHub 星数加分（仅 github 链接、有 fetch_stars 时）
        if _match_domain(host, "github.com") and fetch_stars is not None:
            stars = fetch_stars(url)
            if stars is not None:
                score += _star_bonus(stars, star_tiers)

        if score < min_score:
            excluded.append({"url": url, "reason": f"低分（{score}）"})
            continue
        kept.append({**r, "score": score})

    kept.sort(key=lambda r: r["score"], reverse=True)
    return kept, excluded


def _star_bonus(stars: int, star_tiers) -> int:
    """按星数四档取加分（star_tiers 降序，命中第一档即返回）。"""
    for min_stars, bonus in star_tiers:
        if stars >= min_stars:
            return bonus
    return 0


def _domain_of(url: str) -> str:
    """提取 URL 主机名（小写、去 www. 前缀）。"""
    host = (urlparse(url).netloc or "").lower()
    return host.removeprefix("www.")


def _match_domain(host: str, base: str) -> bool:
    """主机名是否命中基础域名（含子域名）：docs.python.org 命中 python.org。"""
    return host == base or host.endswith("." + base)


def _in_domains(host: str, domains) -> bool:
    """主机名是否命中域名集合中的任意一个（含子域名）。"""
    return any(_match_domain(host, d) for d in domains)


def _url_bonus(url: str, official_docs: int, blog_penalty: int, source_penalty: int) -> int:
    """URL 特征的加减分：官方文档路径加分、个人博客/源码直链降权。"""
    bonus = 0
    host = _domain_of(url)
    path = (urlparse(url).path or "").lower()
    if (host.startswith(("docs.", "learn.", "reference."))
            or path.startswith(("/docs/", "/learn/", "/reference/"))):
        bonus += official_docs
    if host.endswith(".github.io") or "/blog/" in path or "/posts/" in path:
        bonus += blog_penalty
    if "/blob/" in path or host.startswith("raw."):
        bonus += source_penalty
    return bonus
