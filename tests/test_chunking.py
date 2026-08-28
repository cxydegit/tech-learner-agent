"""Markdown 感知分块器（chunker v2）结构性单测（无网络）。

覆盖 chunk_markdown 的核心不变量：
- 长表格 / 长代码围栏整体原子成块，绝不按字符硬切
- 每块携带标题路径前缀（section）
- overlap 永不始于原子块内部
- CRLF 归一化、连续标题、前引块等边界

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_chunking.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src（pytest 无 src 布局配置时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.config import config
from src.domain.chunking import Chunk, chunk_markdown, chunk_text


def big_table() -> str:
    """超过 chunk_size(800) 的长表格，底部放哨兵行（旧 chunk_text 必切中它）。"""
    rows = ["| 列A | 列B | 列C |", "|---|---|---|"]
    for i in range(30):
        rows.append(f"| 内容{i} | 数据项 值{i} | https://example.com/{i} |")
    rows.append("| 哨兵行 SENTINEL | 完整保留 | 末尾 |")
    return "\n".join(rows)


def long_fence() -> str:
    """超过 chunk_size 的长代码围栏。"""
    body = "\n".join(f"line{i} = {i}" for i in range(200))
    return f"```python\n{body}\n```"


# 1. 长表格整体完整（含哨兵行），且表格块以 | 行收尾（无单元格残片）
def test_long_table_stays_whole():
    chunks = chunk_markdown("# 大表\n\n" + big_table())
    tables = [c for c in chunks if "哨兵行" in c.text]
    assert len(tables) == 1, "长表格必须整体成块"
    assert "| 哨兵行 SENTINEL | 完整保留 | 末尾 |" in tables[0].text
    # 表格块整块都是表格行（跳过章节前缀行后），无单元格中间残片
    lines = tables[0].text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].lstrip().startswith("|"):
        start += 1
    body = lines[start:]
    assert body and all(l.lstrip().startswith("|") for l in body)


# 2. 长代码围栏整体完整，围栏成对闭合
def test_long_fence_stays_whole():
    chunks = chunk_markdown("# 代码示例\n\n" + long_fence())
    fences = [c for c in chunks if "line0" in c.text]
    assert len(fences) == 1, "长代码围栏必须整体成块"
    assert "line199" in fences[0].text
    assert fences[0].text.count("```") == 2  # 起始 + 闭合


# 3. 章节上下文前缀正确（"# 顶层 › ## 小节"）
def test_section_prefix():
    chunks = chunk_markdown("# 顶层\n\n## 小节\n\n正文内容。\n")
    para = [c for c in chunks if "正文内容" in c.text]
    assert len(para) == 1
    assert para[0].text.startswith("# 顶层 › ## 小节\n\n正文内容")
    assert para[0].section == "# 顶层 › ## 小节"


# 4. CRLF 归一化后 chunk 列表一致
def test_crlf_normalization():
    text = "# 标题\r\n\r\n段落一。\r\n段落二。\r\n"
    a = chunk_markdown(text.replace("\r\n", "\n"))
    b = chunk_markdown(text)
    assert [c.text for c in a] == [c.text for c in b]
    assert [c.section for c in a] == [c.section for c in b]


# 5. 表格紧跟代码围栏 → 各自完整成块，互不混入
def test_table_then_fence():
    text = "# 混合\n\n| 表 |\n|---|\n| 数据 |\n\n```bash\nls -la\n```\n"
    chunks = chunk_markdown(text)
    table_chunk = [c for c in chunks if "| 数据 |" in c.text]
    fence_chunk = [c for c in chunks if "ls -la" in c.text]
    assert len(table_chunk) == 1 and len(fence_chunk) == 1
    assert "ls -la" not in table_chunk[0].text
    assert "| 数据 |" not in fence_chunk[0].text


# 6. 连续标题 + 空节不产空 chunk，section 正确嵌套
def test_consecutive_headings_no_empty_chunks():
    text = "# 一\n\n## 二\n\n### 三\n\n正文。\n"
    chunks = chunk_markdown(text)
    assert all(c.text.strip() for c in chunks)
    assert len(chunks) == 1, "空节（无正文）不应产生 chunk"
    assert chunks[0].section == "# 一 › ## 二 › ### 三"


# 7. oversize 原子块整块嵌入（len > chunk_size 也完整）
def test_oversize_atomic_whole():
    chunks = chunk_markdown(big_table())
    assert any("哨兵行" in c.text for c in chunks)
    for c in chunks:
        if "哨兵行" in c.text:
            assert len(c.text) > config.RAG_CHUNK_SIZE
            assert "| 哨兵行 SENTINEL" in c.text


# 8. overlap 永不以表格行开头（表格不进 buf → overlap 必为纯正文尾部）
def test_overlap_never_starts_with_table():
    prose = "\n\n".join(
        f"段落内容 第{i} 个，用于填充分块缓冲区的普通正文文字。" for i in range(60)
    )
    text = f"# 标题\n\n{prose}\n\n{big_table()}"
    chunks = chunk_markdown(text)
    assert len(chunks) > 2, "语料应产生多个分块"
    for c in chunks:
        if "哨兵行" in c.text:
            continue  # 完整表格块，允许以 | 开头
        body = c.text[len("# 标题\n\n"):] if c.text.startswith("# 标题") else c.text
        assert not body.lstrip().startswith("|"), f"overlap 从表格中间开始: {body[:40]!r}"


# 9. 前引块（> 日期 / 标签）归入其所属标题节
def test_blockquote_in_section():
    text = "# 笔记\n\n> 日期：2026-08-07\n> 标签：#rag\n\n正文。\n"
    chunks = chunk_markdown(text)
    assert chunks
    assert all(c.section == "# 笔记" for c in chunks)
    assert any("日期：2026-08-07" in c.text for c in chunks)


# 10. Chunk 字段存在；chunk_text 旧接口仍返回 list[str]
def test_chunk_type_and_legacy_chunk_text():
    chunks = chunk_markdown("# 标题\n\n正文。\n")
    assert chunks and isinstance(chunks[0], Chunk)
    assert isinstance(chunks[0].text, str) and isinstance(chunks[0].section, str)
    old = chunk_text("# 标题\n\n正文。\n")
    assert isinstance(old, list) and all(isinstance(x, str) for x in old)


# 11. 真实语料回归：spring-boot-materials.md 的 ## 一 大表不再被切出单元格残片
def test_real_corpus_no_cell_fragment():
    corpus = Path(__file__).resolve().parent.parent / "materials" / "spring-boot-materials.md"
    if not corpus.exists():
        pytest.skip("真实语料不存在")
    text = corpus.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_markdown(text)
    assert chunks, "真实语料不应产生空分块"

    for c in chunks:
        lines = c.text.splitlines()
        if not any(l.lstrip().startswith("|") for l in lines):
            continue
        # 跳过章节前缀行（前缀不以 | 开头），从首个表格行起整块都应是表格行
        start = 0
        while start < len(lines) and not lines[start].lstrip().startswith("|"):
            start += 1
        body = lines[start:]
        assert body and all(l.lstrip().startswith("|") for l in body), (
            f"表格被切开或夹带正文: …{c.text[:100]!r}"
        )


# 12. 超大表格（> 硬上限）→ 按行二次切分：每块重复表头、无数据行丢失
def test_oversize_table_split_with_repeated_header():
    rows = ["| 列A | 列B | 列C |", "|---|---|---|"] + [f"| 行{i} | 数据值 {i} | 列C 内容 {i} |" for i in range(20)]
    text = "# 标题\n\n" + "\n".join(rows)
    chunks = chunk_markdown(text, hard_cap=200)
    subs = [c for c in chunks if "| 行" in c.text]
    assert len(subs) > 1, "超过硬上限的表格必须被二次切分"
    prefix = "# 标题\n\n"
    for c in subs:
        assert c.section == "# 标题"
        body = c.text[len(prefix):]
        assert body.startswith("| 列A | 列B | 列C |"), "每个子块必须重复表头"
        assert len(body) <= 200, f"子块超硬上限: {len(body)}"
    all_rows = "\n".join(c.text[len(prefix):] for c in subs)
    for i in range(20):
        assert f"| 行{i} | 数据值 {i} | 列C 内容 {i} |" in all_rows, f"数据行 {i} 丢失"


# 13. 超大代码围栏（> 硬上限）→ 按空行分组：每块都是完整闭合围栏、无代码丢失
def test_oversize_fence_split_complete():
    code = "```python\n" + "\n\n".join(f"# 段落{i}\nline{i} = {i}" for i in range(10)) + "\n```"
    text = "# 标题\n\n" + code
    chunks = chunk_markdown(text, hard_cap=120)
    subs = [c for c in chunks if "line" in c.text]
    assert len(subs) > 1, "超过硬上限的代码围栏必须被二次切分"
    for c in subs:
        assert c.text.count("```") == 2, "每个子块必须是完整闭合围栏"
    all_code = "\n".join(c.text for c in subs)
    for i in range(10):
        assert f"line{i} = {i}" in all_code, f"代码行 {i} 丢失"


# 14. 截断保底：单个超长行 / 段落即使单独也超硬上限 → 截断，不崩、不产超限块
def test_oversize_single_unit_truncated():
    row = "| " + "x" * 1000 + " |"
    text = "# 标题\n\n| 表头 |\n|---|\n" + row
    chunks = chunk_markdown(text, hard_cap=120)
    assert chunks
    prefix = "# 标题\n\n"
    for c in chunks:
        assert len(c.text[len(prefix):]) <= 120, f"截断后仍超硬上限: {len(c.text[len(prefix):])}"


# 15. 未超硬上限的原子块仍整块保留（硬上限只拦病态超大块）
def test_oversize_atomic_within_cap_stays_whole():
    chunks = chunk_markdown("# 大表\n\n" + big_table(), hard_cap=2000)
    tables = [c for c in chunks if "哨兵行" in c.text]
    assert len(tables) == 1, "未超硬上限的表格应整块保留"
    assert "| 哨兵行 SENTINEL | 完整保留 | 末尾 |" in tables[0].text
