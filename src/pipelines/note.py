"""知识沉淀管道：召回已有笔记 → LLM 差量提取(JSON) → 逐条匹配 → 返回候选（纯数据）。

自 agent.py 迁出：note_pipeline + 就近携带 EXTRACT/MERGE/SUGGEST_SYSTEM_PROMPT。
note_pipeline 只做「召回已有笔记 → 差量提取（只写增量，可输出 []）→ 逐条匹配」，
返回 {new_points, merge_candidates, empty_reason, suggestion}，**不持久化、不交互**；
入库（persist_points）与交互确认由 CLI / LangGraph 节点完成。
"""

import re
from collections.abc import Callable

from ..adapters.audit import audit
from ..adapters.llm import generate_text
from ..adapters.store import (
    find_note_match,
    get_existing_notes,
    persist_note,
    read_file_tool,
    read_knowledge_note,
    recall_existing_notes,
)
from ..config import config
from ..domain.chunking import chunk_text
from ..domain.dedup import strip_note_header
from ..domain.extraction import parse_entries, parse_json_object

# ============================================================
# 提示词
# ============================================================

EXTRACT_SYSTEM_PROMPT = """你是一个知识管理助手。用户会给你一段学习内容（对话记录或文档片段），以及这个技术领域里**已有的知识笔记**。你的任务是：从学习内容中**只提取"已有笔记没有覆盖"的新知识点**，写成**有实质内容**的 Markdown 笔记。

## 差量约束（最重要）
- 先认真阅读"已有知识笔记"，判断哪些是新知识、哪些已被覆盖。
- **只输出已有笔记没覆盖的新内容**（新概念 / 新细节 / 新代码 / 新经验）；已被覆盖的内容**不要重复输出**。
- 如果学习内容与已有笔记高度重复、没有新知识点，**直接输出空数组 []**，不要硬凑。
- 没有提供已有笔记时（首次学习），正常提取全部有价值的知识点。

## 质量门槛（宁可少记、写透）
- **只提取真正有内容的知识点**：有具体概念、机制、步骤、对比、代码或可复用经验。**空洞的一句话概括、套话、常识性废话不要输出**。
- 一条笔记要有实际信息量：能讲清楚"是什么、为什么、怎么用、关键细节"中的至少两点，或带一个可运行的例子。**没有实质内容就跳过或并入已有笔记**。
- 少而精：与其输出 5 条空泛笔记，不如输出 2 条扎实的。

## 写作风格（严禁模板化）
- **绝对不要用"## 是什么""## 为什么重要""## 通俗类比""## 注意事项"这种模板式小标题**——这样写出来的笔记僵硬、像废话。
- 用**贴切内容的小标题**组织（如"## 四种检索模式""## 离线阶段的处理流程"），或直接写连贯段落。
- 好笔记的样子：
  ```markdown
  ## 四种检索模式
  1. 技能引导：适合有明确任务拆解的场景……
  2. 评分检查：用打分机制筛选证据，防止幻觉……
  （每条都写清楚是什么、适用场景、关键点）
  ## 离线阶段四步
  Load → Split → Embed → Store；Split 要按语义边界切块，避免把表格/代码围栏撕碎。
  ```

## 输出要求
只输出一个 JSON 数组，**不要**有任何解释、前言或 ```json 之类的代码块标记。

数组元素结构：
```json
[
  {
    "topic": "知识点标题（简洁、准确的技术术语）",
    "tags": ["技术名", "子领域"],
    "content": "该知识点的 Markdown 笔记正文"
  }
]
```

## 规则
- content 用中文书写，代码示例保留原始代码
- 一个学习内容往往包含多个知识点，尽量分条列出，不要合并成一条"大杂烩"
- 拿不准的、太浅的知识点，宁可不要
"""


MERGE_SYSTEM_PROMPT = """你是一个知识管理助手。我会给你一份**已有笔记**和一段**新提取的知识点**，请把新知识点整合进已有笔记，输出一份合并后的完整笔记正文（Markdown）。

## 要求
- 保留已有笔记中有价值的全部内容（包括原有小节和细节）。
- 把新知识点里**尚未覆盖的新内容**整合进去：可补充进对应小节，或新增小节/示例/注意事项。
- **去掉重复**：已有笔记已讲过的内容，不要重复再写一遍。
- 合并后逻辑清晰，不要出现两个标题相同的小节；代码示例保留完整。
- 中文书写。

## 矛盾处理（新旧对同一事实说法不一致时）
- 对比新旧内容，识别是否存在**相互矛盾的陈述**（如旧说"默认端口 8080"、新说"3.x 起改为 8081"）。
- 若存在矛盾：**以新内容为准**修正矛盾处（新内容是最新学习的），不要同时保留两个矛盾的旧说法。
- 只是详略不同、或新旧讲不同版本且不否定彼此，不算矛盾，正常补充即可。

## 输出
只输出一个 JSON 对象，不要任何解释或 ```json 代码块标记：
{"content": "合并后的笔记正文（不含 # 标题 / > 日期 / > 标签 等头部信息）",
 "report": "矛盾处理报告（一句话说明发现了什么矛盾、改成了什么，如：发现 1 处矛盾：旧笔记说默认端口 8080，新内容说 3.x 起为 8081，已按新内容改为 8081；未发现矛盾时输出空字符串）"}
"""


SUGGEST_SYSTEM_PROMPT = """你是一个学习规划助手。某个技术领域的学习内容已经全部沉淀进知识库（本次没有新增知识点）。请基于技术名称、已有学习资料和知识库已覆盖的主题，推荐 1-2 个**尚未覆盖、值得继续学习的方向**。

## 输入
- 技术名称
- 已有学习资料摘录
- 知识库已覆盖的主题列表

## 输出要求
- 只推荐资料中有提及、但知识库还没覆盖的方向。
- 每个方向用 1-2 句话说清楚"是什么 + 为什么值得继续学"。
- 直接输出 Markdown 文本（可用 `1. **方向名**：说明` 格式），不要 JSON、不要解释。
"""


# ============================================================
# note_pipeline：召回 → 差量提取 → 匹配（无副作用）
# ============================================================

def _build_extraction_user(tech: str, conversation_log: str, existing: list[dict]) -> str:
    """组装差量提取的 user_content：技术 + 已有笔记上下文(限量) + 学习内容。

    Token 预算：已有笔记每条截断 NOTE_CONTEXT_LIMIT 字，**篇数由召回侧限量**
    （recall_existing_notes 的 top_k），这里不再二次截断——两处各截一次的话，
    改动哪一处都不会生效。学习内容按 NOTE_CONTENT_CHARS 截断。
    """
    if existing:
        blocks = []
        for n in existing:
            topic = n.get("topic") or ""
            body = (n.get("content") or "")[: config.NOTE_CONTEXT_LIMIT]
            blocks.append(f"### {topic}\n{body}")
        existing_ctx = "\n\n".join(blocks)
    else:
        existing_ctx = "（本技术暂无已有笔记，正常提取全部知识点）"
    content = conversation_log[: config.NOTE_CONTENT_CHARS]
    return (
        f"技术领域：{tech}\n\n"
        f"===== 已有知识笔记 =====\n{existing_ctx}\n"
        f"===== 学习内容开始 =====\n{content}\n===== 学习内容结束 ====="
    )


def _sim_of(note: dict) -> float | None:
    """召回条目的相似度（保留三位）；RAG 不可用走"最近 top-k"回退时没有分数 → None。

    None 是回退路径的标记，不是"相似度为零"——两者在日志里必须能分开。
    """
    sim = note.get("similarity")
    return round(float(sim), 3) if isinstance(sim, (int, float)) else None


def _recall_queries(conversation_log: str, query_segments: list[str] | None) -> list[str]:
    """把每组文本切成要拿去检索的查询：一次查询一块。

    一组查一次是**必需**而不是讲究：一批对话常跨多个主题，只拿开头的文本查一次，后出现的
    主题在查询向量里没有任何分量，对应笔记必然召不回来——差量提取于是看不见它，把已覆盖的
    内容当新知识重提。

    一条助手讲解经常超过 1500 字（实测 60 条里 22 条超过，最长 3162 字）。超过的部分不是丢掉，
    是**切开、每块各查一次**：只取开头 1500 字的话，后面的内容在查询里没有表示。
    切块用 chunk_text（尽量切在段落边界，段落本身就超长才硬切；相邻块留一点重叠，
    避免把一句话从中间切断）。

    名额分配：**每组的第一块一定发出去**（讲解的开头信息最密），剩下的名额再按顺序补给长消息
    后面的块。之前是"所有块拉平了均匀取样"，实测会把某组的第一块正好跳过——那一组等于在
    查询里没有表示，正是分组要修的问题。

    没有分组时（交互式的三个调用点）退回旧行为：整批取开头 1500 字查一次。
    """
    # 重叠不能大过块本身：chunk_text 在 overlap ≥ chunk_size 时按 0 步长切分，直接抛异常
    overlap = min(config.RAG_CHUNK_OVERLAP, config.NOTE_QUERY_CHARS // 2)
    blocks_by_msg = [chunk_text(seg.strip(), chunk_size=config.NOTE_QUERY_CHARS, overlap=overlap)
                     for seg in (query_segments or []) if (seg or "").strip()]
    first_of_each = [blocks[0] for blocks in blocks_by_msg if blocks]
    if not first_of_each:
        head = conversation_log[: config.NOTE_QUERY_CHARS]
        return [head] if head.strip() else []
    limit = max(1, config.NOTE_RECALL_MAX_QUERIES)
    if len(first_of_each) > limit:  # 消息条数本身就超上限（罕见）：均匀取样，保住首尾
        span = len(first_of_each) - 1
        first_of_each = ([first_of_each[round(i * span / (limit - 1))] for i in range(limit)]
                         if limit >= 2 else first_of_each[:1])
    rest = [b for blocks in blocks_by_msg for b in blocks[1:]]
    return (first_of_each + rest)[:limit]


def _audit_recall(tech: str, *, batch_chars: int, queries: list[str], kb_notes: int,
                  recalled: list[dict]) -> None:
    """记一条召回现场：批次多大、切了几段、召回了哪几篇（**只记路径与分数**）。

    这是「召回窄了」和「提取质量差」唯一能分开的证据来源：没有它，两种现象在日志里长得一样
    ——都是"该抑制的没抑制住"。

    kb_notes（该技术笔记总篇数）必须和召回结果一起记：召回为空有两种完全不同的成因——
    库里本来就没有笔记（正常），和库里有笔记但都被相似度下限滤掉了。少了总数这两个状态分不开
    （但**分辨不了后者是哪一种**：被挡下那批的最高余弦现在没有读数，见计划文档的待测项）。

    sim_min 一并记下，读日志时不必回头查当时的配置。

    提取正文的长度也记在这里：它只在管道内部拿得到，且一次运行一条记录最便于对齐。
    """
    query_chars = sum(len(q) for q in queries)
    audit("note_recall",
          tech=tech,
          batch_chars=batch_chars,
          query_count=len(queries),
          query_chars=query_chars,
          query_truncated=batch_chars > query_chars,
          kb_notes=kb_notes,
          sim_min=config.NOTE_RECALL_SIM_MIN,
          content_chars=min(batch_chars, config.NOTE_CONTENT_CHARS),
          content_truncated=batch_chars > config.NOTE_CONTENT_CHARS,
          recalled_count=len(recalled),
          recalled=[{"path": n.get("path") or "", "sim": _sim_of(n)} for n in recalled])


def note_pipeline(tech: str, conversation_log: str,
                  materials_path: str | None = None,
                  progress: Callable[[str], None] | None = None,
                  query_segments: list[str] | None = None) -> dict:
    """差量提取管道：召回已有笔记 → LLM 只输出新增点 → 匹配生成 merge_candidates。

    与交互层的区别：只返回数据（new_points / merge_candidates / empty_reason /
    suggestion），**不持久化、不交互** —— 入库与确认由 CLI / LangGraph 节点完成。

    Args:
        tech: 技术名称
        conversation_log: 本轮学习的对话记录或文档内容
        materials_path: 可选，该技术的 materials 报告路径（"无新内容"时推荐未覆盖方向用）
        progress: 可选回调，接收进度消息；None 则静默
        query_segments: 可选，召回用的查询段（一段一次检索）。**调用方手里有结构化对话时
            才给**（自动沉淀的缓冲就是）；不给则退回"批次开头截一段"的旧行为

    Returns:
        {
          "new_points": [{topic, tags, content}],       # 与已有笔记无重叠，待新建
          "merge_candidates": [{old_path, old_topic, old_content, similarity, reason,
                                topic, tags, content}],  # 与已有笔记相似，待用户确认合并
          "empty_reason": str | None,                   # 非空表示无新内容，未沉淀
          "suggestion": str | None,                     # empty 且有 materials 时的方向推荐
          "summary": str, "raw": str,
          "new_count": int, "merged_count": int,
        }
    """
    # 1. 语义召回：该 tech 下与学习内容最相关的已有笔记（差量上下文）
    if progress:
        progress("🔎 召回已有笔记...")
    existing_all = get_existing_notes(tech)  # 全量（逐条匹配用），同时给召回日志一个"库里有几篇"的底
    queries = _recall_queries(conversation_log, query_segments)
    # 下限一律生效，不看库大小："小库就全给"曾实现过又撤掉——低相似度的笔记就是噪声，
    # 给出去可能诱发过抑制（模型判"已覆盖"→ 输出 [] → 静默丢内容），而下面限清空上下文
    # 只会多提取（下游 find_note_match 与用户闸门兜底，最坏是一篇可见的重复笔记）。
    existing = recall_existing_notes(tech, queries, config.NOTE_RECALL_TOP_K,
                                     sim_min=config.NOTE_RECALL_SIM_MIN)
    _audit_recall(tech, batch_chars=len(conversation_log), queries=queries,
                  kb_notes=len(existing_all), recalled=existing)

    # 2. 差量提取：只输出新增知识点（可输出 []）
    if progress:
        progress("🧠 LLM 差量提取知识点...")
    raw = generate_text(EXTRACT_SYSTEM_PROMPT, _build_extraction_user(tech, conversation_log, existing),
                        call_site="note.extract")
    entries = parse_entries(raw)

    if not entries:
        # 4. 无新内容路径：不沉淀；有 materials 则轻量 LLM 推荐未覆盖方向，否则只如实告知
        topics = [n["topic"] for n in existing_all]
        suggestion = suggest_directions(tech, materials_path, topics) if materials_path else None
        empty_reason = "学习内容与已有知识笔记重复，或没有可提取的新知识点"
        return {
            "new_points": [], "merge_candidates": [], "empty_reason": empty_reason,
            "suggestion": suggestion, "summary": empty_reason, "raw": raw,
            "new_count": 0, "merged_count": 0,
        }

    # 3. 逐条匹配已有笔记 → 新点 / 合并候选
    new_points: list[dict] = []
    merge_candidates: list[dict] = []
    for e in entries:
        topic = (e.get("topic") or "").strip()
        body = (e.get("content") or "").strip()
        if not topic or not body:
            continue
        # 无技术主题（未 collect）时不兜底空标签；LLM 没给 tags 就留空，避免 `#` 空标签
        tags = e.get("tags") or ([tech] if tech else [])
        # 候选召回 → 标题 fast-path → LLM 判定（find_note_match），返回第一个判定
        # same 的候选及理由；reason 供确认时展示。
        match, similarity, reason = find_note_match(tech, topic, existing_all, content=body, tags=tags)
        if match:
            merge_candidates.append({
                "old_path": match["path"],
                "old_topic": match["topic"],
                "old_content": read_knowledge_note(match["path"]),
                "similarity": similarity,
                "reason": reason,
                "topic": topic,
                "tags": tags,
                "content": body,
            })
        else:
            new_points.append({"topic": topic, "tags": tags, "content": body})

    summary = (
        f"差量提取出 {len(new_points)} 个新知识点，{len(merge_candidates)} 条与已有笔记相似待确认"
        if (new_points or merge_candidates)
        else "未提取到可入库的新知识点"
    )
    return {
        "new_points": new_points,
        "merge_candidates": merge_candidates,
        "empty_reason": None,
        "suggestion": None,
        "summary": summary,
        "raw": raw,
        "new_count": len(new_points),
        "merged_count": len(merge_candidates),
    }


# ============================================================
# 交互层复用：无新内容推荐 / 差量合并 / 合并候选展示与决策解析
# ============================================================

def suggest_directions(tech: str, materials_path: str | None, existing_topics: list[str]) -> str | None:
    """无新内容时的轻量推荐：基于 materials 摘录 + 已覆盖主题，推荐未覆盖方向。

    Returns:
        推荐文本（Markdown）；materials 文件不存在/读取失败则返回 None（只告知不推荐）。
    """
    if not materials_path:
        return None
    read = read_file_tool(materials_path)
    if not read.get("success"):
        return None
    materials = (read.get("content") or "")[:3000]
    topics_block = "\n".join(f"- {t}" for t in existing_topics[:20]) or "（暂无）"
    user_content = (
        f"技术名称：{tech}\n\n"
        f"===== 已有学习资料摘录 =====\n{materials}\n"
        f"===== 知识库已覆盖的主题 =====\n{topics_block}"
    )
    suggestion = generate_text(SUGGEST_SYSTEM_PROMPT, user_content, call_site="note.suggest")
    return suggestion.strip()


def merge_notes(old_content: str, new_content: str, topic: str) -> dict:
    """LLM 差量合并：把新知识点并入已有笔记正文，去掉重复，返回合并后的正文 + 矛盾报告。

    合并时识别新旧对同一事实的相互矛盾，**以新内容为准**修正矛盾处（新内容是最新学习的），
    并在 report 中明确说明发现了什么矛盾、改成了什么（供用户复核）。

    Args:
        old_content: 已有笔记全文（含 # 标题 / > 日期 / > 标签 头部，合并时剥头）
        new_content: 新提取的知识点正文
        topic: 新知识点主题

    Returns:
        {"content": 合并后正文（不含头部），"report": 矛盾处理报告，无矛盾为空字符串}。
        JSON 解析失败 / content 缺失 → 降级 content=原始输出、report=""（与旧行为一致，安全侧）。
    """
    old_body = strip_note_header(old_content)
    user_content = (
        f"===== 已有笔记正文 =====\n{old_body}\n\n"
        f"===== 新提取的知识点（主题：{topic}） =====\n{new_content}"
    )
    raw = generate_text(MERGE_SYSTEM_PROMPT, user_content, call_site="note.merge").strip()
    obj = parse_json_object(raw)
    content = obj.get("content")
    if not content or not str(content).strip():
        content = raw  # 解析失败 / content 缺失 → 降级：整个输出当正文（与旧行为一致）
    return {"content": str(content).strip(), "report": str(obj.get("report") or "").strip()}


def persist_points(tech: str, new_points: list[dict], merge_candidates: list[dict],
                   merge_indices: set[int]) -> dict:
    """交互确认后统一入库：new_points 全部新建；merge_candidates 按 merge_indices 合并。

    合并候选用 LLM 差量合并（merge_notes，合并时识别矛盾、以新内容为准修正）后再覆盖写入；
    未入选的候选跳过（不沉淀）。合并中发现的矛盾经 conflict_reports 透出给调用方展示。

    Args:
        tech: 技术名称
        new_points: 待新建的知识点 [{topic, tags, content}]
        merge_candidates: 合并候选列表（note_pipeline 产出）
        merge_indices: 要合并的候选 0-based 索引集合；空集表示全部跳过

    Returns:
        {"results": [persist 结果...], "new_count": int, "merged_count": int,
         "conflict_reports": [{path, topic, report}, ...]}（无矛盾为空列表）
    """
    new_count = merged_count = 0
    results: list[dict] = []
    conflict_reports: list[dict] = []
    for np_ in new_points:
        r = persist_note(tech, np_["topic"], np_["content"], np_["tags"])
        # 逐篇记账（不是逐批）：知识库的每一次变更都要能追到"哪次会话、新建还是合并"，
        # 批次计数从事件条数就能读出来，多一个 count 字段反而要求读者知道批次边界
        audit("note_persist", tech=tech, path=r.get("path"), kind=r.get("action"),
              index_ok=r.get("index_ok"))
        results.append(r)
        new_count += 1
    for idx in sorted(merge_indices):
        c = merge_candidates[idx]
        merged = merge_notes(c["old_content"], c["content"], c["topic"])
        # 合并保留旧笔记的标题（identity 属于被合并的旧笔记），标题/文件名/INDEX 保持一致；
        # 若用新点 topic 当标题，会篡改已有笔记的主题（文件名还是旧的，标题却变了）
        r = persist_note(tech, c["old_topic"], merged["content"], c["tags"], replace_path=c["old_path"])
        audit("note_persist", tech=tech, path=r.get("path"), kind=r.get("action"),
              index_ok=r.get("index_ok"), conflict=bool(merged["report"]))
        results.append(r)
        merged_count += 1
        if merged["report"]:
            conflict_reports.append({"path": c["old_path"], "topic": c["topic"],
                                     "report": merged["report"]})
    return {"results": results, "new_count": new_count, "merged_count": merged_count,
            "conflict_reports": conflict_reports}


def format_merge_candidates(candidates: list[dict]) -> str:
    """把合并候选渲染成面向用户的展示文本（/learn 图 interrupt 用，纯文本）。"""
    lines = [f"发现 {len(candidates)} 条新知识点与已有笔记相似，如何处理？"]
    for i, c in enumerate(candidates, 1):
        sim = f"（相似度 {c['similarity']:.2f}）" if c.get("similarity") is not None else ""
        reason = f"，理由：{c['reason']}" if c.get("reason") else ""
        delta = (c.get("content") or "").strip().replace("\n", " ")[:120]
        lines.append(f"[{i}] {c['topic']} → 已有「{c['old_topic']}」{sim}{reason}")
        lines.append(f"    新增点：{delta}")
    lines.append("回复：all 全合并 / 编号逗号分隔逐条（如 1,3）/ skip 全部跳过")
    return "\n".join(lines)


# 合并决策的同义词表：解析（parse_merge_decision）与审计归类（decision_kind）共用，
# 避免"日志里说的"和"代码实际认的"分叉——两处各写一份的话，加一个同义词就会只改一边。
_MERGE_ALL_WORDS = frozenset({"all", "a", "y", "yes", "全合并", "全部合并", "都合并"})
_MERGE_SKIP_WORDS = frozenset({"skip", "s", "n", "no", "跳过", "全部跳过", "none", ""})


def decision_kind(decision: str | None) -> str:
    """把用户的合并决策归一成审计用的类别：all / numbered / skip / free_text。"""
    text = (decision or "").strip().lower()
    if text in _MERGE_ALL_WORDS:
        return "all"
    if text in _MERGE_SKIP_WORDS:
        return "skip"
    if all(part.isdigit() or not part for part in re.split(r"[,，\s]+", text)):
        return "numbered"
    return "free_text"


def parse_merge_decision(answer: str | None, n: int) -> set[int]:
    """解析用户对合并候选的决定：all 全合并 / 编号逗号分隔逐条 / skip 或空全部跳过。

    Returns:
        要合并的候选 0-based 索引集合；空集表示全部跳过。
    """
    text = (answer or "").strip().lower()
    if text in _MERGE_ALL_WORDS:
        return set(range(n))
    if text in _MERGE_SKIP_WORDS:
        return set()
    picked: set[int] = set()
    for part in re.split(r"[,，\s]+", text):
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < n:
                picked.add(idx)
    return picked
