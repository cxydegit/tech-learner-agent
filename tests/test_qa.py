"""pipelines/qa 管道单测（零网络）：mock 召回 + LLM，锁分组/无命中/tech 透传行为。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_qa.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipelines import qa as qa_mod


def _seed_hits():
    """构造跨 2 篇笔记的命中集（语义检索返回结构，相似度降序）。"""
    return [
        {"id": "a::0", "path": "knowledge/rag/文档分块.md", "source": "knowledge", "tech": "rag",
         "topic": "文档分块", "url": "", "similarity": 0.93,
         "document": "纯字符分块：按空行分段落 + 800 字符截断，会撕碎 Markdown 表格。"},
        {"id": "a::1", "path": "knowledge/rag/文档分块.md", "source": "knowledge", "tech": "rag",
         "topic": "文档分块", "url": "", "similarity": 0.91,
         "document": "Markdown 感知切块：长表格/代码围栏原子成块，标题作章节前缀。"},
        {"id": "b::0", "path": "knowledge/di/依赖注入.md", "source": "knowledge", "tech": "di",
         "topic": "依赖注入", "url": "", "similarity": 0.80,
         "document": "构造器注入：依赖通过构造参数传入，对象自建依赖。"},
    ]


# ---------- 分组（联想检索核心） ----------

def test_grouping_by_path(monkeypatch):
    """同 path 多条命中归为一组，best_similarity 取最大，组按相似度降序。"""
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(qa_mod, "generate_text", lambda s, u: "答案：两种分块方式")
    res = qa_mod.qa_pipeline("分块方式", top_k=8)
    assert res["no_hit"] is False
    # 3 条命中跨 2 篇笔记 → 2 组，不重复计数同篇片段
    assert len(res["sources"]) == 2
    assert [s["path"] for s in res["sources"]] == [
        "knowledge/rag/文档分块.md", "knowledge/di/依赖注入.md"]
    assert res["sources"][0]["similarity"] == 0.93
    assert res["sources"][0]["topic"] == "文档分块"
    assert res["sources"][0]["snippet"]  # 代表性片段非空


def test_group_hits_cap_and_snippets():
    """组数上限 + 每组片段数上限 + 片段截断（直接测 _group_hits 纯规则）。"""
    hits = [
        {"path": "knowledge/a/x.md", "similarity": 0.9, "document": "ABCDEF"},
        {"path": "knowledge/a/x.md", "similarity": 0.8, "document": "片段2"},
        {"path": "knowledge/a/x.md", "similarity": 0.7, "document": "片段3"},
        {"path": "knowledge/a/x.md", "similarity": 0.6, "document": "片段4"},  # 超每组上限，丢弃
        {"path": "knowledge/b/y.md", "similarity": 0.95, "document": "b片段"},
        {"path": "knowledge/c/z.md", "similarity": 0.85, "document": "c片段"},
        {"path": "knowledge/d/w.md", "similarity": 0.75, "document": "d片段"},
        {"path": "knowledge/e/v.md", "similarity": 0.65, "document": "e片段"},
        {"path": "knowledge/f/u.md", "similarity": 0.55, "document": "f片段"},
    ]
    groups = qa_mod._group_hits(hits, max_groups=5, snippets_per_note=3, snippet_chars=3)
    # 组数上限 5
    assert len(groups) == 5
    # 同 path 4 条命中 → 只保留前 3 条片段
    a_group = next(g for g in groups if g["path"] == "knowledge/a/x.md")
    assert len(a_group["snippets"]) == 3
    # 片段截断到 snippet_chars
    assert a_group["snippets"][0] == "ABC"
    # 按 best_similarity 降序
    sims = [g["best_similarity"] for g in groups]
    assert sims == sorted(sims, reverse=True)


# ---------- 无命中 ----------

def test_no_hit(monkeypatch):
    """乱码查询 → 无命中 → no_hit=True、sources 空、answer 空，且不调 LLM。"""
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: [])

    def _fail(*args, **kwargs):
        raise AssertionError("no_hit 不应触发 LLM 调用")
    monkeypatch.setattr(qa_mod, "generate_text", _fail)
    res = qa_mod.qa_pipeline("asdfjkl;qwerty 乱码查询", top_k=8)
    assert res["no_hit"] is True
    assert res["sources"] == []
    assert res["answer"] == ""


def test_empty_question(monkeypatch):
    """空问题 → 直接 no_hit，不查库、不调 LLM（图节点防御性兜底）。"""
    called = []
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: called.append(1) or [])
    res = qa_mod.qa_pipeline("   ")
    assert res["no_hit"] is True
    assert called == []


def test_no_hit_when_model_says_not_recorded(monkeypatch):
    """检索有命中，但模型明确说「笔记里没有记录」→ no_hit=True（触发 collect 引导）。"""
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(qa_mod, "generate_text",
                        lambda s, u: "笔记里没有记录 Redis 分布式锁的实现原理。")
    res = qa_mod.qa_pipeline("Redis分布式锁的实现原理")
    assert res["no_hit"] is True
    assert "笔记里没有记录" in res["answer"]


def test_no_hit_false_when_covered(monkeypatch):
    """检索有命中且模型正常回答 → no_hit=False（不触发 collect 引导）。"""
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(qa_mod, "generate_text",
                        lambda s, u: "笔记提到两种分块方式：（来源：knowledge/rag/文档分块，相关度 0.93）")
    res = qa_mod.qa_pipeline("分块方式")
    assert res["no_hit"] is False


# ---------- tech 过滤 / LLM 调用 ----------

def test_tech_passthrough(monkeypatch):
    """tech 过滤生效：tech 参数原样透传给召回层（供未来 Web 端范围选择）。"""
    captured = {}

    def fake_search(question, top_k, tech):
        captured.update(question=question, top_k=top_k, tech=tech)
        return _seed_hits()
    monkeypatch.setattr(qa_mod, "_search_notes", fake_search)
    monkeypatch.setattr(qa_mod, "generate_text", lambda s, u: "ok")
    res = qa_mod.qa_pipeline("分块", tech="fastapi", top_k=8)
    assert res["no_hit"] is False
    assert captured["tech"] == "fastapi"
    assert captured["top_k"] == 8


def test_answer_cites_source(monkeypatch):
    """答案能引用对应笔记（canned 答案原样回传，验证来源标注路径打通）。"""
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    canned = "笔记中提到两种分块方式……（来源：knowledge/rag/文档分块，相关度 0.93）"
    monkeypatch.setattr(qa_mod, "generate_text", lambda s, u: canned)
    res = qa_mod.qa_pipeline("分块")
    assert res["answer"] == canned
    assert "knowledge/rag" in res["answer"]


def test_history_in_user_content(monkeypatch):
    """对话历史 prepend 进 user_content（多轮上下文生效）；QA_PROMPT 作 system。"""
    captured = {}

    def fake_generate(system, user):
        captured["system"] = system
        captured["user"] = user
        return "ok"
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(qa_mod, "generate_text", fake_generate)
    history = [{"question": "之前问过的问题一", "answer": "回答一"},
               {"question": "之前问过的问题二", "answer": "回答二"}]
    qa_mod.qa_pipeline("分块", history=history)
    assert captured["system"] == qa_mod.QA_PROMPT
    assert "之前问过的问题一" in captured["user"]
    assert "回答二" in captured["user"]


def test_history_answer_appendix_stripped(monkeypatch):
    """历史答案里的来源附录在注入 user_content 前被剥离（防旧会话污染复教模型）。"""
    captured = {}
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(qa_mod, "generate_text",
                        lambda s, u: captured.update(user=u) or "ok")
    history = [{"question": "旧问题",
                "answer": "旧答案。\n\n📚 来源笔记\n• knowledge/old.md（主题：x，相关度 0.9）"}]
    qa_mod.qa_pipeline("分块", history=history)
    assert "📚 来源笔记" not in captured["user"]
    assert "旧答案。" in captured["user"]


# ---------- 确定性兜底：截断来源附录 ----------

def test_strip_source_appendix_markdown_header():
    """「📚 来源笔记」式附录被截断，只剩正文。"""
    answer = ("笔记里没有记录 Redis 分布式锁的实现原理。\n\n"
              "本轮检索到的笔记片段主要涉及以下内容，供参考：\n"
              "1. Redis Stack 与核心版差异：...\n\n"
              "📚 来源笔记\n"
              "• knowledge/redis/xxx.md（主题：xx，相关度 0.58）")
    assert qa_mod._strip_source_appendix(answer) == "笔记里没有记录 Redis 分布式锁的实现原理。"


def test_strip_source_appendix_reference_phrase():
    """「…供参考：」参考引导行同样触发截断（模型复述片段的另一形态）。"""
    answer = ("简短回答。\n\n"
              "本轮检索到的笔记片段主要涉及以下内容，供参考：\n"
              "1. 片段一...\n2. 片段二...")
    assert qa_mod._strip_source_appendix(answer) == "简短回答。"


def test_strip_keeps_clean_answer():
    """无附录的正常答案原样保留（含内联出处标注，不误切正文）。"""
    answer = ("笔记提到两种分块方式：\n"
              "1. **纯字符分块**——按空行分段落（来源：knowledge/rag/文档分块，相关度 0.93）。\n"
              "2. **Markdown 感知切块**——长表格原子成块（来源：knowledge/rag/文档分块，相关度 0.91）。\n\n"
              "需要我补充更多的分块方式吗？")
    assert qa_mod._strip_source_appendix(answer) == answer


def test_pipeline_strips_appendix(monkeypatch):
    """管道返回的 answer 已去除来源附录（确定性兜底，存进 qa_history 的是干净答案）。"""
    monkeypatch.setattr(qa_mod, "_search_notes", lambda q, k, t: _seed_hits())
    monkeypatch.setattr(
        qa_mod, "generate_text",
        lambda s, u: "答案正文。\n\n📚 来源笔记\n• knowledge/redis/xxx.md（主题：x，相关度 0.9）")
    res = qa_mod.qa_pipeline("分块")
    assert res["answer"] == "答案正文。"
