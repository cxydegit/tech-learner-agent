"""跨笔记联想检索 Q&A 管道：召回知识库 → 按 path 分组 → 单次 LLM 综合回答 + 来源标注。

Step 4 新功能：用户问「我之前有哪些笔记提到过 X？」，系统跨笔记召回分散片段 →
按来源笔记聚合 → 一次 LLM 综合回答 + 标注来源。纯数据进出（返回 dict），无副作用、不打印；
交互（no_hit 的 collect 引导）由 CLI / LangGraph 节点完成。

⚠️ vector 导入必须保持函数内 lazy（不变量 I1：`import src.cli` 不得拉起 chromadb），
测试通过 monkeypatch `_search_notes` 桥接做零网络单测。
"""

import re

from ..adapters.llm import generate_text
from ..config import config


# ============================================================
# 提示词
# ============================================================

QA_PROMPT = """你是一个知识问答助手，回答用户关于 TA 的学习笔记的问题。我会给你一批从笔记库检索到的片段，请直接回答问题，并逐条标注出处。

## 要求
1. 只依据「检索到的笔记片段」回答；片段里没有的信息，明确说「笔记里没有记录」，不要编造。
2. 每条结论后内联标注出处，格式：（来源：<文件名>，相关度 <数值>）。
3. 区分「笔记中的内容」与「你的总结/推断」：来自笔记的照实写；自己的推断标注（我的总结/推断）。
4. Markdown 输出，简洁、直接，先给答案，不要任何前言。
5. 绝对不要复述、转写或罗列「检索到的笔记片段」本身；回答里只能出现你自己的话 + 内联出处标注，不要输出任何片段清单、参考列表或附录。

## 对话历史
对话历史（若有）仅用于理解上下文，回答仍以本轮检索到的片段为准，不要引用历史回答里本轮未出现的内容。

## 好的回答的样子（示例）
用户问：笔记里提到过哪些分块方式？
你回答：
笔记中提到两种分块方式：

1. **纯字符分块**——按空行分段落 + 800 字符截断，会撕碎 Markdown 表格/代码围栏（来源：knowledge/rag/分块.md，相关度 0.93）。
2. **Markdown 感知切块**——长表格/代码围栏原子成块，标题作章节前缀（来源：knowledge/rag/分块.md，相关度 0.91）。

需要我补充更多细节吗？
"""


# ============================================================
# 召回桥接（I1：vector 层函数内 lazy）
# ============================================================

def _search_notes(question: str, top_k: int, tech: str | None):
    """lazy import vector 层，守住 I1（`import src.cli` 不加载 chromadb）。

    测试通过 monkeypatch 本函数返回种子命中集，做零网络管道单测。
    """
    from ..adapters.vector import semantic_search_knowledge
    return semantic_search_knowledge(question, top_k, tech)


# ============================================================
# 联想检索核心：按 path 分组
# ============================================================

def _group_hits(hits: list[dict], max_groups: int, snippets_per_note: int,
                snippet_chars: int) -> list[dict]:
    """把检索命中按 path 分组（联想检索核心）。

    同一篇笔记的多条命中归为一组（避免答案把同篇片段当多条来源），每组保留：
    {path, topic, best_similarity, snippets[≤snippets_per_note 条 × snippet_chars 字]}。
    组按 best_similarity 降序，组数上限 max_groups。hits 已按相似度降序
    （chroma 按距离排序），每组按出现顺序取前几条片段即可。

    Args:
        hits: semantic_search_knowledge 返回的命中列表
        max_groups: 最多返回的组数
        snippets_per_note: 每组最多保留的片段数
        snippet_chars: 每条片段的截断字数

    Returns:
        分组列表，按 best_similarity 降序，最多 max_groups 组。
    """
    groups: dict[str, dict] = {}
    for h in hits:
        path = (h.get("path") or "").strip()
        if not path:
            continue
        g = groups.setdefault(path, {
            "path": path,
            "topic": h.get("topic", ""),
            "best_similarity": 0.0,
            "snippets": [],
        })
        sim = h.get("similarity") or 0.0
        if sim > g["best_similarity"]:
            g["best_similarity"] = sim
            g["topic"] = h.get("topic", g["topic"])
        if len(g["snippets"]) < snippets_per_note:
            g["snippets"].append((h.get("document") or "")[:snippet_chars].strip())
    ordered = [g for g in groups.values() if g["snippets"]]
    ordered.sort(key=lambda g: g["best_similarity"], reverse=True)
    return ordered[:max_groups]


def _build_qa_user(question: str, groups: list[dict], history: list[dict] | None) -> str:
    """组装 LLM 的 user_content：对话历史 + 笔记片段材料区 + 用户问题。

    Token 预算（参照 note 模块 12000 字截断纪律）：历史答案截断 500 字、问题 200 字；
    片段已在 _group_hits 截断到 snippet_chars。

    片段用「平铺材料区」呈现（[片段 N] 来自笔记：…），刻意不用 Markdown 小标题、
    不含「来源笔记」字样——避免模型把注入片段的结构照抄回吐成来源附录（结构回声，
    见 _strip_source_appendix）。
    """
    parts: list[str] = []
    if history:
        lines: list[str] = []
        for exch in history[-config.QA_HISTORY_ROUNDS:]:
            q = (exch.get("question") or "")[:200]
            # 历史答案同样先剥离来源附录：旧会话持久化的答案可能带污染，直接注入会
            # 当作 few-shot 示范复教模型输出「来源笔记」清单
            a = _strip_source_appendix(exch.get("answer") or "")[:500]
            if q or a:
                lines.append(f"用户：{q}\n助手：{a}")
        if lines:
            parts.append("===== 对话历史（最近几轮，仅作上下文） =====\n" + "\n\n".join(lines))

    blocks: list[str] = []
    for idx, g in enumerate(groups, 1):
        body = "\n".join(g["snippets"])
        blocks.append(f"[片段 {idx}] 来自笔记：{g['path']}（相关度 {g['best_similarity']:.2f}）\n{body}")
    parts.append(
        "===== 检索到的笔记片段（仅供你理解，禁止复述或罗列它们） =====\n"
        + "\n\n".join(blocks)
        + "\n===== 片段结束 ====="
    )
    parts.append(f"用户问题：{question}")
    return "\n\n".join(parts)


# ============================================================
# 确定性兜底：去掉 LLM 复述的来源附录（结构回声，提示词禁令不可靠）
# ============================================================

# 独立成行的来源附录标题：允许 markdown/emoji 前缀，行末须以标题收尾（防误切正文句子）
_APPENDIX_HEADER_RE = re.compile(
    r"^\s*(?:[#*>\-]+\s*)?(?:📚|📎|🗂)?\s*(?:来源笔记|参考资料|参考片段|参考材料)\s*[:：]?\s*$"
)
# 参考引导短语：模型常以「…供参考：」引入检索片段清单
_REFERENCE_PHRASE_RE = re.compile(r"供参考|仅供参考|供你参考|供您参考")


def _strip_source_appendix(answer: str) -> str:
    """从来源附录引导行处截断答案，保证输出只剩回答正文。

    模型读完注入的笔记片段后，常把 `### path（主题：…，相关度 …）` 分组头 + 片段原文
    在回答末尾复述成「📚 来源笔记」清单（结构回声）。这里按行扫描，遇到：
    - 独立成行的来源附录标题（来源笔记 / 参考资料 / 参考片段…），或
    - 含「供参考 / 仅供参考」的参考引导行
    即从该行截断。伪阳性风险低：标题须独立成行、以标题收尾；正常正文（含内联
    「来源：…」出处标注）不会匹配。
    """
    lines = (answer or "").splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if _APPENDIX_HEADER_RE.match(s) or _REFERENCE_PHRASE_RE.search(s):
            return "\n".join(lines[:i]).rstrip()
    return answer


# 模型被 QA_PROMPT 要求：片段没覆盖时明确说「笔记里没有记录」。命中这些措辞即判定未覆盖。
_NO_RECORD_RE = re.compile(r"笔记(?:中|里)(?:都)?没有|没有(?:相关|关于)|未在笔记")


def _says_not_covered(answer: str) -> bool:
    """模型是否明确表示笔记未覆盖该问题（QA_PROMPT 要求用「笔记里没有记录」措辞）。

    检索有命中但内容不相关时（如问「分布式锁」却只检索到 Redis 基础笔记），模型会按
    QA_PROMPT 要求如实说「笔记里没有记录」。命中这类措辞 → 判定 no_hit，触发 CLI 的
    collect 引导。纯措辞匹配，不额外调 LLM、不改提示词。
    """
    return bool(_NO_RECORD_RE.search((answer or "").strip()))


# ============================================================
# qa_pipeline：召回 → 分组 → 单次 LLM 综合回答（无副作用）
# ============================================================

def qa_pipeline(question: str, *, tech: str | None = None, top_k: int = 8,
                history: list[dict] | None = None) -> dict:
    """跨笔记联想检索 Q&A 管道（无副作用、不打印）。

    Args:
        question: 用户问题
        tech: 可选，限定技术领域；默认 None 跨全部笔记（"闭包"类问题天生跨笔记，
              不受当前会话 topic 限制；参数留给未来 Web 端做范围选择）
        top_k: 召回条数；默认用 config.QA_TOP_K
        history: 最近几轮 {question, answer} 对话记录（qa_history 取末 N 轮）

    Returns:
        {
          "answer": str,                                    # LLM 综合回答（检索零命中时为空串）
          "sources": [{path, topic, similarity, snippet}],  # 按来源笔记聚合，相似度降序
          "hits": list,                                     # 原始召回命中（可能为空）
          "no_hit": bool,  # 问题未被笔记覆盖：检索零命中，或模型明确表示「笔记里没有记录」
        }
    """
    question = (question or "").strip()
    if not question:
        return {"answer": "", "sources": [], "hits": [], "no_hit": True}

    # 1. 召回（try/except：RAG 未索引 / Chroma 异常时优雅降级为空 hits）
    try:
        hits = _search_notes(question, top_k or config.QA_TOP_K, tech)
    except Exception:  # noqa: BLE001
        hits = []

    # 2. 按 path 分组（联想检索核心）
    groups = _group_hits(hits, config.QA_MAX_GROUPS, config.QA_SNIPPETS_PER_NOTE,
                         config.QA_SNIPPET_CHARS)

    # 3. 无命中：如实告知，不调 LLM
    if not groups:
        return {"answer": "", "sources": [], "hits": hits, "no_hit": True}

    # 4. 单次 LLM 综合回答
    sources = [
        {"path": g["path"], "topic": g["topic"], "similarity": g["best_similarity"],
         "snippet": g["snippets"][0]}
        for g in groups
    ]
    answer = _strip_source_appendix(
        generate_text(QA_PROMPT, _build_qa_user(question, groups, history)).strip())
    # no_hit：检索零命中，或模型明确表示「笔记里没有记录」（后者触发 CLI 的 collect 引导）
    return {"answer": answer, "sources": sources, "hits": hits, "no_hit": _says_not_covered(answer)}
