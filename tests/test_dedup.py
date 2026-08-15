"""domain/dedup 纯函数单测（零网络）。

锁 sanitize_filename / _topics_overlap / _with_header 的行为，防止回归。
运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_dedup.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.dedup import (
    _content_concept_overlap,
    _parse_tags,
    _same_knowledge_point,
    _tag_overlap,
    _topics_overlap,
    _with_header,
    sanitize_filename,
    strip_note_header,
)


# ---------- sanitize_filename ----------

def test_sanitize_filename_basic():
    assert sanitize_filename("Spring Boot") == "spring-boot"
    assert sanitize_filename("FastAPI") == "fastapi"


def test_sanitize_filename_chinese_and_symbols():
    assert sanitize_filename("依赖 注入!@#") == "依赖-注入"


def test_sanitize_filename_strips_edges():
    assert sanitize_filename("--rust--") == "rust"


# ---------- _topics_overlap ----------

def test_overlap_equal():
    assert _topics_overlap("依赖注入", "依赖注入")


def test_overlap_substring():
    assert _topics_overlap("依赖注入", "依赖注入的原理")


def test_overlap_case_insensitive():
    assert _topics_overlap("HTTP 缓存", "http 缓存")


def test_overlap_distinct():
    assert not _topics_overlap("HTTP 缓存", "数据库事务")


def test_overlap_empty():
    assert not _topics_overlap("", "依赖注入")
    assert not _topics_overlap("依赖注入", "")


# ---------- _with_header ----------

def test_with_header():
    header = _with_header("依赖注入", ["spring", "di"], "正文内容")
    assert header.startswith("# 依赖注入")
    assert "#spring" in header and "#di" in header
    assert header.endswith("正文内容")
    assert "> 日期：" in header


def test_with_header_no_tags():
    header = _with_header("依赖注入", None, "正文")
    assert "> 标签：" in header
    assert header.endswith("正文")


# ---------- strip_note_header ----------

def test_strip_note_header_removes_front_matter():
    content = _with_header("依赖注入", ["spring", "di"], "## 是什么\n正文内容")
    assert content.startswith("# 依赖注入")
    assert strip_note_header(content) == "## 是什么\n正文内容"


def test_strip_note_header_without_title():
    body = strip_note_header("\n> 日期：2026-08-09\n> 标签：#x\n\n正文")
    assert body == "正文"


def test_strip_note_header_plain_body():
    assert strip_note_header("## 是什么\n正文") == "## 是什么\n正文"


# ---------- 去重确认层（RAG_OPTIMIZATION P0） ----------

# 模拟真实知识笔记正文（概率数据类型 / 数据结构设计反模式），供内容信号测试
_PROB_DATA_BODY = (
    "牺牲精度换取空间的统计利器：概率类型用于海量数据统计场景，允许一定误差但内存占用极低。\n"
    "- **Bloom filter（布隆过滤器）**：用于判断元素是否存在。特点：可能存在假阳性，但绝无假阴性，"
    "适合防止缓存穿透。\n"
    "- **HyperLogLog**：用于基数统计。特点：统计唯一用户数（UV）时，标准误差约 1%，无需存储所有元素。"
)
_ANTIPATTERN_BODY = (
    "避免 Strings 滥用：将复杂对象序列化为 JSON 字符串存入 String 类型，频繁修改字段要全量"
    "反序列化再序列化，性能开销大；需要部分更新的对象场景，优先使用 Hashes 或 JSON 类型。"
)


def test_parse_tags_from_header():
    assert _parse_tags("# 主题\n\n> 日期：2026-08-09\n> 标签：#Redis #数据结构 #选型\n\n正文") == [
        "Redis", "数据结构", "选型"]
    assert _parse_tags("# 主题\n\n> 日期：2026-08-09\n\n正文") == []


def test_tag_overlap_specific_not_tech():
    """排除 tech 名标签：共享具体标签确认，只共享 #Redis 不确认。"""
    assert _tag_overlap(["Redis", "数据结构", "统计"], ["Redis", "数据结构", "进阶"], "redis")
    assert not _tag_overlap(["Redis", "统计"], ["Redis", "数据结构", "进阶"], "redis")
    assert not _tag_overlap(["Redis"], ["Redis", "数据结构"], "redis")
    # 真实 rag--检索增强生成 笔记标签：#RAG #LLM #基础概念 #Fine-tuning #架构选型
    assert _tag_overlap(["RAG", "LLM"], ["RAG", "LLM", "基础概念", "Fine-tuning", "架构选型"], "rag")


def test_tag_overlap_ignores_meta_tags():
    """元标签（部署/踩坑/最佳实践…）不参与确认：只共享 #部署 不确认。

    实证：windows 笔记 #Redis #踩坑 #部署 #Windows 若让「Docker 部署 Redis 集群」
    误并入，就是因为共享了元标签 #部署。
    """
    assert not _tag_overlap(["Redis", "Docker", "部署"], ["Redis", "踩坑", "部署", "Windows"], "redis")
    assert not _tag_overlap(["Redis", "持久化"], ["Redis", "最佳实践", "避坑"], "redis")


def test_content_concept_overlap_same_topic():
    """同知识点的措辞不同改写：判别性概念高度出现在旧笔记正文 → 确认。"""
    new = ("牺牲精度换取空间的统计利器：布隆过滤器用于判断元素是否存在，可能存在假阳性但绝无假阴性，"
           "适合防止缓存穿透；HyperLogLog 用于基数统计，统计唯一用户数时标准误差约 1%。")
    assert _content_concept_overlap(new, _PROB_DATA_BODY)


def test_content_concept_overlap_unrelated():
    """无关内容：判别性概念几乎不出现 → 不确认。"""
    assert not _content_concept_overlap("今天天气不错，我出门散步，顺便买了杯咖啡。", _PROB_DATA_BODY)


def test_same_knowledge_point_by_title():
    """标题确认：Redis 数据结构选型 → 基础数据类型选型决策。"""
    existing = {"topic": "基础数据类型选型决策", "tags": ["Redis", "数据结构", "选型"], "content": ""}
    assert _same_knowledge_point("Redis 数据结构选型", ["Redis", "数据结构"], "", existing, "redis") == "same"


def test_same_knowledge_point_by_tag():
    """标题对不上但具体标签确认：布隆/HyperLogLog → 概率数据类型。"""
    existing = {"topic": "概率数据类型", "tags": ["Redis", "数据结构", "进阶"], "content": _PROB_DATA_BODY}
    # 标题 overlap 0.33 < 0.4 不确认，靠 #数据结构 标签确认
    assert _same_knowledge_point("布隆过滤器和 HyperLogLog 统计", ["Redis", "数据结构"],
                                 "", existing, "redis") == "same"


def test_same_knowledge_point_by_content():
    """标题与标签都失败、措辞完全不同的同义改写：内容判别性概念救回。"""
    existing = {"topic": "概率数据类型", "tags": ["Redis", "数据结构", "进阶"], "content": _PROB_DATA_BODY}
    new_content = ("牺牲精度换取空间的统计利器：布隆过滤器判断元素是否存在（允许假阳性），"
                   "HyperLogLog 统计唯一用户数，误差约 1%，两者内存占用都极低。")
    # 标题 overlap 0.33 < 0.4、标签只共享 tech 名 → 靠内容确认
    assert _same_knowledge_point("用少量内存统计海量数据", ["Redis", "统计"],
                                 new_content, existing, "redis") == "same"


def test_same_knowledge_point_none_confirm():
    """高相似但无关（误合并根源）：标题/标签/内容都不确认 → no，不合并。"""
    existing = {"topic": "数据结构设计反模式", "tags": ["Redis", "最佳实践", "避坑"], "content": _ANTIPATTERN_BODY}
    assert _same_knowledge_point("Redis 数据持久化的实现", ["Redis", "持久化"],
                                 "RDB 与 AOF 两种持久化方式的区别。", existing, "redis") == "no"
