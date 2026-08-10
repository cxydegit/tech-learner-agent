"""知识沉淀管道：LLM 提取知识点(JSON) → 逐条去重/合并入库 → 更新索引。

自 agent.py 迁出：note_pipeline + 就近携带 EXTRACT_SYSTEM_PROMPT。
纯数据进出（返回 dict），不打印、不写会话 —— 供 LangGraph 节点复用。
"""

from typing import Callable

from ..adapters.llm import generate_text
from ..adapters.store import persist_note
from ..domain.extraction import parse_entries


# ============================================================
# 提示词
# ============================================================

EXTRACT_SYSTEM_PROMPT = """你是一个知识管理助手。用户会给你一段学习内容（对话记录或文档片段），请从中提取关键知识点，整理成可入库的结构化笔记。

## 输出要求
只输出一个 JSON 数组，**不要**有任何解释、前言或 ```json 之类的代码块标记。

数组元素结构：
```json
[
  {
    "topic": "知识点标题（简洁、准确的技术术语，如'依赖注入'）",
    "tags": ["技术名", "子领域"],
    "content": "该知识点的 Markdown 笔记正文，需包含这些小节：## 是什么 / ## 为什么重要 / ## 通俗类比 / ## 代码示例 / ## 注意事项 / ## 参考资料"
  }
]
```

## 规则
- 只提取有价值、可复用的关键知识点；忽略寒暄、客套和无关内容
- 如果内容里没有可提取的知识点，输出空数组 []
- content 用中文书写，代码示例保留原始代码
- 一个学习内容往往包含多个知识点，尽量分条列出，不要合并成一条"大杂烩"
"""


# ============================================================
# note_pipeline
# ============================================================

def note_pipeline(tech: str, conversation_log: str,
                  progress: Callable[[str], None] | None = None) -> dict:
    """确定性管道核心：LLM 提取知识点(JSON) → 逐条去重/合并入库 → 更新索引。

    与 run_note 的区别：只返回数据（results / summary / 计数），
    不打印、不写会话 —— 供 LangGraph 节点复用（Stage 3）。

    Args:
        tech: 技术名称
        conversation_log: 本轮学习的对话记录或文档内容
        progress: 可选回调，接收进度消息；None 则静默

    Returns:
        {"results": [persist 结果], "summary": str,
         "new_count": int, "merged_count": int, "raw": str}
        - results 为空表示未提取到可入库知识点
    """
    # 1. LLM 提取关键知识点（单次调用，输出 JSON）
    content = conversation_log[:12000]
    raw = generate_text(
        EXTRACT_SYSTEM_PROMPT,
        f"技术领域：{tech}\n\n===== 学习内容开始 =====\n{content}\n===== 学习内容结束 =====",
    )
    entries = parse_entries(raw)

    # 2. 逐条去重/合并并入库
    new_count = merged_count = 0
    results: list[dict] = []
    for e in entries:
        topic = (e.get("topic") or "").strip()
        body = (e.get("content") or "").strip()
        if not topic or not body:
            continue
        tags = e.get("tags") or [tech]
        result = persist_note(tech, topic, body, tags)
        results.append(result)
        if result["action"] == "new":
            new_count += 1
        else:
            merged_count += 1

    summary = (
        f"新增 {new_count} 篇，合并更新 {merged_count} 篇"
        if results else "未提取到可入库的知识点"
    )
    return {
        "results": results,
        "summary": summary,
        "new_count": new_count,
        "merged_count": merged_count,
        "raw": raw,
    }
