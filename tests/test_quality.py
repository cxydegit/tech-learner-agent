"""domain/quality 预筛器纯函数单测（零网络，mock github 星数）。

覆盖：内容农场剔除、低分剔除、官方域名高分、星数四档加分、github 走星数不走域名加分、
无 fetch_stars 时优雅降级、excluded 带原因、子域名匹配。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_quality.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.quality import _star_bonus, screen_results

QUALITY = dict(
    official_domains={"python.org"},
    platform_domains={"github.com", "stackoverflow.com"},
    content_farms={"example-farm.com"},
    min_score=0,
    star_tiers=((10000, 30), (1000, 20), (100, 10), (0, 5)),
    domain_bonus_official=20,
    domain_bonus_platform=10,
    url_bonus_official_docs=10,
    url_penalty_blog=-5,
    url_penalty_source=-5,
)


def _results(*urls):
    return [{"url": u, "title": u, "content": ""} for u in urls]


# ---------- 剔除 ----------

def test_content_farm_excluded():
    kept, excluded = screen_results(
        _results("https://example-farm.com/a", "https://python.org/tutorial"), **QUALITY)
    assert [e["url"] for e in excluded] == ["https://example-farm.com/a"]
    assert excluded[0]["reason"] == "内容农场"
    assert [k["url"] for k in kept] == ["https://python.org/tutorial"]


def test_content_farm_subdomain():
    """内容农场子域名同样命中剔除。"""
    _, excluded = screen_results(_results("https://blog.example-farm.com/post"), **QUALITY)
    assert excluded and excluded[0]["reason"] == "内容农场"


def test_low_score_excluded():
    """github.io 个人博客无星数（无 token）→ 仅 -5 降权 → 低于阈值剔除。"""
    kept, excluded = screen_results(_results("https://user.github.io/blog/post"), **QUALITY)
    assert not kept
    assert excluded and excluded[0]["reason"].startswith("低分")


def test_no_fetch_stars_degrades():
    """无 fetch_stars 时 github 链接不走星数加分，仍可通过（不因无 token 剔除）。"""
    kept, excluded = screen_results(_results("https://github.com/a/b"), **QUALITY)
    assert kept and not excluded
    assert kept[0]["score"] == 0


# ---------- 通过 + 排序 ----------

def test_domain_scores_ranking():
    """官方 python.org(+20) > stackoverflow(+10) > github(0，无星数)。"""
    kept, _ = screen_results(
        _results("https://github.com/a/b", "https://python.org/tutorial",
                 "https://stackoverflow.com/q/1"),
        fetch_stars=lambda u: None, **QUALITY)
    assert [k["url"] for k in kept] == [
        "https://python.org/tutorial", "https://stackoverflow.com/q/1", "https://github.com/a/b"]
    assert kept[0]["score"] == 20


def test_official_subdomain_matches():
    """官方文档子域名命中白名单基础域：docs.python.org = 官方(+20) + 文档路径(+10)。"""
    kept, _ = screen_results(_results("https://docs.python.org/3/tutorial/"), **QUALITY)
    assert kept[0]["score"] == 30


def test_star_bonus_applied():
    """有 token 时 github 高星 → 星数加分显著，压过一般域名。"""
    stars = {"https://github.com/a/b": 15000}
    kept, _ = screen_results(
        _results("https://github.com/a/b", "https://python.org/tutorial"),
        fetch_stars=lambda u: stars.get(u), **QUALITY)
    # github 15000 星 → +30 > python.org +20
    assert kept[0]["url"] == "https://github.com/a/b"
    assert kept[0]["score"] == 30


def test_star_tiers():
    assert _star_bonus(15000, QUALITY["star_tiers"]) == 30
    assert _star_bonus(2000, QUALITY["star_tiers"]) == 20
    assert _star_bonus(150, QUALITY["star_tiers"]) == 10
    assert _star_bonus(30, QUALITY["star_tiers"]) == 5


def test_excluded_reason_present():
    _, excluded = screen_results(
        _results("https://example-farm.com/x", "https://user.github.io/blog"), **QUALITY)
    reasons = {e["url"]: e["reason"] for e in excluded}
    assert reasons["https://example-farm.com/x"] == "内容农场"
    assert "低分" in reasons["https://user.github.io/blog"]
