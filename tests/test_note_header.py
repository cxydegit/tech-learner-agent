"""笔记题头解析与索引剥离单测（零网络）。

覆盖：
- `> 日期` / `> 标签` 解析与剥离，其他引用行原样保留；
- 无题头 / 只有日期 / 只有标签 / CRLF；
- 与 chunk_markdown 衔接：题头不再单独成块，语义单元仍合并；
- 索引器把日期/标签写入 Chroma 元数据。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.chunking import chunk_markdown
from src.domain.note_header import parse_note_header


def test_parse_standard_header():
    text = ("# 标题\n\n> 日期：2026-08-09\n> 标签：#Redis #缓存\n\n"
            "## 小节\n\n正文。\n")
    date, tags, body = parse_note_header(text)
    assert date == "2026-08-09"
    assert tags == ["Redis", "缓存"]
    assert "> 日期" not in body and "> 标签" not in body
    assert "# 标题" in body and "## 小节" in body and "正文。" in body


def test_parse_crlf():
    text = "# 标题\r\n\r\n> 日期：2026-08-09\r\n> 标签：#a\r\n\r\n正文。\r\n"
    date, tags, body = parse_note_header(text)
    assert date == "2026-08-09"
    assert tags == ["a"]
    assert "\r" not in body and "> 日期" not in body


def test_no_header_returns_original():
    text = "# 标题\n\n正文。\n"
    assert parse_note_header(text) == (None, [], text)


def test_only_date_or_only_tags():
    d, t, body = parse_note_header("# 标题\n\n> 日期：2026-08-09\n\n正文。\n")
    assert (d, t) == ("2026-08-09", []) and "> 日期" not in body
    d, t, body = parse_note_header("# 标题\n\n> 标签：#a #b\n\n正文。\n")
    assert (d, t) == (None, ["a", "b"]) and "> 标签" not in body


def test_other_blockquote_preserved():
    text = "# 标题\n\n> 引用一句话\n\n正文。\n"
    assert parse_note_header(text) == (None, [], text)


def test_mixed_blockquote_keeps_unrecognized_lines():
    text = ("# 标题\n\n> 日期：2026-08-09\n> 引用一句话\n> 标签：#a\n\n正文。\n")
    date, tags, body = parse_note_header(text)
    assert date == "2026-08-09"
    assert tags == ["a"]
    assert "> 引用一句话" in body
    assert "> 日期" not in body and "> 标签" not in body


def test_content_before_blockquote_is_not_treated_as_header():
    text = "# 标题\n\n正文第一段。\n\n> 日期：2026-08-09\n"
    assert parse_note_header(text) == (None, [], text)


def test_header_not_chunked_as_standalone_block():
    """剥离题头后：两节两块，题头不再单独成块，类型提示三部分仍同块。"""
    text = (
        "# 函数定义规范与类型提示\n\n"
        "> 日期：2026-08-28\n> 标签：#Python #函数 #类型提示\n\n"
        "## 命名规范\n\n- 命名推荐 snake_case。\n\n"
        "## 类型提示机制\n\n"
        "函数定义后的 `-> int:` 表示预期返回值类型为整数。\n\n"
        "```python\ndef f() -> int:\n    return 1\n```\n\n"
        "**关键特性**：不强制运行时校验，可配 mypy 静态检查。\n"
    )
    _date, _tags, body = parse_note_header(text)
    chunks = chunk_markdown(body)
    assert len(chunks) == 2
    assert all("> 日期" not in c.text and "> 标签" not in c.text for c in chunks)
    assert any("mypy" in c.text for c in chunks)
    assert all(c.section.startswith("# 函数定义规范与类型提示") for c in chunks)


def test_index_single_file_strips_header_and_adds_metadata(tmp_path, monkeypatch):
    """索引器：题头行不进 document，日期/标签进 Chroma 元数据。"""
    import src.adapters.vector as vector_mod
    from src.config import config

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    note = tmp_path / "knowledge" / "python" / "2026-08-28-示例.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "# 示例\n\n> 日期：2026-08-28\n> 标签：#Python #测试\n\n## 节\n\n正文。\n",
        encoding="utf-8")

    class FakeCollection:
        def __init__(self):
            self.ids, self.docs, self.metas = [], [], []

        def get(self, where=None, include=None):
            return {"ids": [], "metadatas": []}

        def delete(self, ids):
            pass

        def add(self, ids, documents, metadatas):
            self.ids, self.docs, self.metas = ids, documents, metadatas

    coll = FakeCollection()
    status, err = vector_mod._index_single_file(coll, note, force=False)
    assert status == "indexed" and err is None
    assert coll.docs and all("> 日期" not in d and "> 标签" not in d for d in coll.docs)
    assert coll.metas[0]["date"] == "2026-08-28"
    assert coll.metas[0]["tags"] == "#Python #测试"
    assert coll.metas[0]["source"] == "knowledge"
