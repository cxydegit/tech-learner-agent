"""pipelines/collect 纯函数单测（零网络）：materials 文件名时间版本号 + excluded 汇报统计。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_collect.py -v
"""

import re
import sys
from pathlib import Path

# 保证 tests/ 下能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.pipelines.collect as collect_mod
from src.adapters.llm import REPORT_TRUNCATION_NOTICE
from src.config import config
from src.pipelines.collect import _excluded_summary, materials_filename

# ---------- materials 文件名（带时间版本号避免覆盖） ----------

def test_materials_filename_has_timestamp_version():
    """每次运行带 MMDD-HHMM 时间版本号，区分多次询问。"""
    name = materials_filename("FastAPI")
    assert re.fullmatch(r"materials/fastapi-materials-\d{4}-\d{4}\.md", name)


def test_materials_filename_sanitizes_tech():
    """技术名小写、空格转连字符，版本号后缀仍存在。"""
    name = materials_filename("Spring Boot 3")
    assert name.startswith("materials/spring-boot-3-materials-")
    assert re.search(r"-\d{4}-\d{4}\.md$", name)


# ---------- excluded 汇报统计 ----------

def test_excluded_summary_empty():
    assert _excluded_summary([]) == ""


def test_excluded_summary_counts():
    s = _excluded_summary([{"url": "a", "reason": "内容农场"}, {"url": "b", "reason": "低分（-5）"}])
    assert "共排除 2 条" in s
    assert "内容农场 1 条" in s
    assert "低分（-5） 1 条" in s


# ---------- 报告生成的接线：独立 token 预算 + 截断标注 + 提示词字数约束 ----------

def test_collect_report_uses_budget_notice_and_length_rule(monkeypatch):
    """整篇报告必须带独立 token 预算与截断标注，提示词里必须带字数约束。

    这一层原先没有测试：预算或标注没接上不会报错，只会静默退回对话级 4096 被截断。
    """
    captured: dict = {}

    def fake_generate(system, user, **kw):
        captured.update(kw)
        captured["system"] = system
        return "# 报告\n正文"

    hit = {"title": "T", "url": "https://e.com/x", "content": "摘要"}
    monkeypatch.setattr(collect_mod, "search_tool", lambda q: {"results": [hit]})
    monkeypatch.setattr(collect_mod, "screen_results", lambda *a, **kw: ([hit], []))
    monkeypatch.setattr(collect_mod, "fetch_many",
                        lambda urls: [{"markdown": "# 抓取正文", "title": "T"}])
    monkeypatch.setattr(collect_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": len(content), "success": True})
    monkeypatch.setattr(collect_mod, "generate_text", fake_generate)

    out = collect_mod.collect_pipeline("FastAPI")

    assert out["report"].startswith("# 报告")
    assert captured["max_tokens"] == config.REPORT_MAX_TOKENS
    assert captured["truncation_notice"] == REPORT_TRUNCATION_NOTICE
    assert captured["call_site"] == "collect.report"
    assert "800~2000 汉字" in captured["system"]  # 字数约束真的进了提示词


def test_collect_all_fetch_failed_marks_resource_incomplete(monkeypatch):
    """抓取全失败（fetch_many 绝不抛错、静默记空）→ 提示词里必须显式说明并要求如实标注。"""
    captured = {}

    def fake_generate(system, user, **kw):
        captured["user"] = user
        return "# 报告"

    hit = {"title": "T", "url": "https://e.com/x", "content": "摘要"}
    monkeypatch.setattr(collect_mod, "search_tool", lambda q: {"results": [hit]})
    monkeypatch.setattr(collect_mod, "screen_results", lambda *a, **kw: ([hit], []))
    monkeypatch.setattr(collect_mod, "fetch_many",
                        lambda urls: [{"markdown": "", "error": "timeout"}])
    monkeypatch.setattr(collect_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})
    monkeypatch.setattr(collect_mod, "generate_text", fake_generate)

    out = collect_mod.collect_pipeline("FastAPI")
    assert out["resource_ok"] is False
    assert "抓取阶段全部失败" in captured["user"]


def test_collect_no_results_marks_resource_incomplete(monkeypatch):
    """搜索零结果 → 同样要给信号，否则模型会凭自己的知识编一份资源清单。"""
    captured = {}

    def fake_generate(system, user, **kw):
        captured["user"] = user
        return "# 报告"

    monkeypatch.setattr(collect_mod, "search_tool", lambda q: {"results": []})
    monkeypatch.setattr(collect_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})
    monkeypatch.setattr(collect_mod, "generate_text", fake_generate)

    out = collect_mod.collect_pipeline("FastAPI")
    assert out["resource_ok"] is False
    assert "没有找到资料" in captured["user"]


def test_collect_normal_path_stays_resource_ok(monkeypatch):
    """正常抓到正文时不加任何"资料不完整"提示（避免误报）。"""
    captured = {}

    def fake_generate(system, user, **kw):
        captured["user"] = user
        return "# 报告"

    hit = {"title": "T", "url": "https://e.com/x", "content": "摘要"}
    monkeypatch.setattr(collect_mod, "search_tool", lambda q: {"results": [hit]})
    monkeypatch.setattr(collect_mod, "screen_results", lambda *a, **kw: ([hit], []))
    monkeypatch.setattr(collect_mod, "fetch_many",
                        lambda urls: [{"markdown": "# 正文", "title": "T"}])
    monkeypatch.setattr(collect_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})
    monkeypatch.setattr(collect_mod, "generate_text", fake_generate)

    out = collect_mod.collect_pipeline("FastAPI")
    assert out["resource_ok"] is True
    assert "资料不完整" not in captured["user"]


# ---------- GitHub 星数：并发预取 + 条数封顶 ----------

def test_prefetch_star_counts_caps_lookups(monkeypatch):
    """条数封顶（硬要求）：超过上限的仓库链接不再查——星数只是加分信号，不值得拖慢 collect。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(config, "GITHUB_STAR_MAX_LOOKUPS", 3)
    seen: list[str] = []
    monkeypatch.setattr(collect_mod, "fetch_star_count",
                        lambda u, token=None: (seen.append(u), 100)[1])

    urls = [f"https://github.com/o{i}/r{i}" for i in range(10)]
    cache = collect_mod._prefetch_star_counts([{"url": u} for u in urls])

    assert len(seen) == 3                       # 只查了前 3 条
    assert set(cache) == set(urls[:3])


def test_prefetch_star_counts_prefilters_and_respects_no_token(monkeypatch):
    """明显非 github / 路径不像仓库的链接不占名额；无 token 时一个请求都不发。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    seen: list[str] = []
    monkeypatch.setattr(collect_mod, "fetch_star_count",
                        lambda u, token=None: (seen.append(u), None)[1])

    cache = collect_mod._prefetch_star_counts([
        {"url": "https://example.com/a/b"},     # 非 github
        {"url": "https://github.com/a"},        # 路径只有一个段落，不像仓库
        {"url": "https://github.com/a/b"},
    ])
    assert seen == ["https://github.com/a/b"]
    assert cache == {"https://github.com/a/b": None}

    seen.clear()
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    assert collect_mod._prefetch_star_counts([{"url": "https://github.com/a/b"}]) == {}
    assert seen == []                           # 没 token 完全不发请求（既有约定）


def test_prefetch_star_counts_is_concurrent(monkeypatch):
    """并发而非串行：8 条 × 0.25s、4 线程 → 远快于串行的 2.0s。"""
    import time

    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(config, "GITHUB_STAR_MAX_LOOKUPS", 8)
    monkeypatch.setattr(config, "GITHUB_STAR_WORKERS", 4)

    def slow(u, token=None):
        time.sleep(0.25)
        return 1

    monkeypatch.setattr(collect_mod, "fetch_star_count", slow)
    urls = [{"url": f"https://github.com/o{i}/r{i}"} for i in range(8)]
    t0 = time.monotonic()
    cache = collect_mod._prefetch_star_counts(urls)
    elapsed = time.monotonic() - t0
    assert len(cache) == 8
    assert elapsed < 1.2, f"疑似串行执行：{elapsed:.2f}s"


def test_collect_pipeline_passes_cached_stars_to_screen(monkeypatch):
    """接线：预筛回调读的是预取缓存（星数照常参与加分），未命中的链接不再触发网络。"""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(collect_mod, "fetch_star_count", lambda u, token=None: 8888)
    captured = {}

    def fake_screen(results, **kw):
        captured["fetch_stars"] = kw.get("fetch_stars")
        return ([], [])

    hit = {"title": "T", "url": "https://github.com/o/r", "content": "摘要"}
    monkeypatch.setattr(collect_mod, "search_tool", lambda q: {"results": [hit]})
    monkeypatch.setattr(collect_mod, "screen_results", fake_screen)
    monkeypatch.setattr(collect_mod, "generate_text", lambda s, u, **kw: "# 报告")
    monkeypatch.setattr(collect_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})

    collect_mod.collect_pipeline("FastAPI")

    stars = captured["fetch_stars"]
    assert stars is not None
    assert stars("https://github.com/o/r") == 8888
    assert stars("https://github.com/never/looked-up") is None   # 缓存未命中 → 不发网络


def test_collect_report_wires_heartbeat_progress(monkeypatch):
    """报告生成要挂上心跳回调：那一步占管道耗时 99%，不能全程静默。"""
    captured = {}

    def fake_generate(system, user, **kw):
        captured.update(kw)
        return "# 报告"

    hit = {"title": "T", "url": "https://e.com/x", "content": "摘要"}
    monkeypatch.setattr(collect_mod, "search_tool", lambda q: {"results": [hit]})
    monkeypatch.setattr(collect_mod, "screen_results", lambda *a, **kw: ([hit], []))
    monkeypatch.setattr(collect_mod, "fetch_many",
                        lambda urls: [{"markdown": "# 正文", "title": "T"}])
    monkeypatch.setattr(collect_mod, "save_file_tool",
                        lambda path, content: {"path": path, "size": 0, "success": True})
    monkeypatch.setattr(collect_mod, "generate_text", fake_generate)

    seen: list[str] = []
    collect_mod.collect_pipeline("FastAPI", progress=seen.append)

    assert callable(captured["progress"])
    assert captured["progress"] is seen.append or captured["progress"]("x") is None
    assert captured["progress_label"] == "LLM 生成学习资料"
