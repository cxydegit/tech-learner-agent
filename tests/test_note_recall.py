"""差量提取的召回改造单测：按回合分段 → 多查询 → 按笔记并集（相似度下限 + 篇数截断）。

零网络：monkeypatch 向量检索与笔记列表。
运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_note_recall.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.adapters.store as store_mod
import src.adapters.vector as vector_mod
import src.pipelines.note as note_mod
import src.pipelines.route as route_mod
from src.config import config


def _notes(*paths: str) -> list[dict]:
    return [{"path": p, "topic": Path(p).stem, "date": "2026-01-01",
             "content": f"{p} 的正文", "tags": []} for p in paths]


def _hits(mapping: dict[str, list[tuple[str, float]]]):
    """按查询里的关键词路由的假检索：模拟"这一段查得回这篇笔记"。"""
    def fake(query: str, top_k: int = 1, tech: str | None = None) -> list[dict]:
        for key, hits in mapping.items():
            if key in query:
                return [{"path": p, "similarity": s} for p, s in hits[:top_k]]
        return []
    return fake


# ============ 分段（route.py 纯函数） ============

def test_segments_split_on_assistant_messages():
    """一条助手讲解 = 一段：前后两个主题必须落在两段里，不能并成一段。"""
    buf = [{"role": "assistant", "content": "讲 A" * 20}, {"role": "user", "content": "问 A" * 20},
           {"role": "assistant", "content": "讲 B" * 20}, {"role": "user", "content": "问 B" * 20}]
    segs = route_mod._buffer_query_segments(buf)
    assert len(segs) == 2
    assert "讲 A" in segs[0] and "讲 B" not in segs[0]
    assert "讲 B" in segs[1]
    assert "问 A" in segs[0]  # 短回是主题信号，跟着它所属的讲解走


def test_long_user_paste_stays_out_of_queries(monkeypatch):
    """用户粘贴（运行结果 / 报错 / 配置）是素材，不是待提取的知识，不占查询预算。

    实测：一批 8 次查询里 6 次花在一份 15421 字的 `--dump-config` 输出上，2672 字的讲解
    只占 1 次——查询预算被素材吃掉了。
    """
    monkeypatch.setattr(config, "NOTE_RECALL_USER_MSG_CHARS", 200)
    buf = [{"role": "assistant", "content": "讲解" * 200},
           {"role": "user", "content": "dump" * 5000}]  # 20000 字粘贴
    segs = route_mod._buffer_query_segments(buf)
    assert len(segs) == 1 and "讲解" in segs[0] and "dump" not in segs[0]

    queries = note_mod._recall_queries("整批", segs)
    assert all("dump" not in q for q in queries)
    assert "讲解" in queries[0]


def test_bare_chitchat_segment_filtered():
    """没有助手内容的寒暄段滤掉：单独查一次只会带回噪声。"""
    segs = route_mod._buffer_query_segments([{"role": "user", "content": "继续"}])
    assert segs == []


def test_question_before_answer_is_one_segment():
    """不常见但会发生的顺序（用户先问、助手再答）也要并成一段，而不是拆成两条查询。"""
    buf = [{"role": "user", "content": "Context 和 useMemo 有什么区别？" * 3},
           {"role": "assistant", "content": "讲解" * 60}]
    segs = route_mod._buffer_query_segments(buf)
    assert len(segs) == 1 and "useMemo" in segs[0] and "讲解" in segs[0]


def test_sweep_passes_segments_to_pipeline(monkeypatch):
    """自动沉淀路径必须把分段传下去（结构化的缓冲在它手里，管道拿不到）。"""
    seen = {}
    monkeypatch.setattr(route_mod, "note_pipeline",
                        lambda tech, text, **k: (seen.update(k) or
                                                 {"new_points": [], "merge_candidates": [],
                                                  "empty_reason": "无新内容"}))
    buf = [{"role": "assistant", "content": "讲" * 40}, {"role": "user", "content": "问" * 40}]
    route_mod.run_memory_sweep("X", buf)
    assert len(seen["query_segments"]) == 1 and "讲" * 40 in seen["query_segments"][0]


def test_long_message_splits_into_blocks(monkeypatch):
    """一条消息里贴进一整篇资料（实测最长 15421 字）时不能只取开头：要切成多块、每块各查一次。"""
    monkeypatch.setattr(config, "NOTE_QUERY_CHARS", 100)
    seg = "\n\n".join(f"第{i}节" + "内容" * 40 for i in range(4))  # 4 个段落，共约 600 字
    queries = note_mod._recall_queries("整批正文", [seg])
    assert len(queries) > 1
    assert all(len(q) <= 100 + config.RAG_CHUNK_OVERLAP for q in queries)
    # 每一节都必须在某条查询里有表示（这正是分组 + 切块的全部意义）
    assert all(any(f"第{i}节" in q for q in queries) for i in range(4))


def test_first_block_of_each_message_is_queried(monkeypatch):
    """每组的第一块必须发出去：均匀取样会把某组的第一块跳过，那一组等于没有表示。

    实测就是这么漏掉过一条助手讲解——8 个查询名额里，某条讲解的第一块正好落在取样
    步长之间。
    """
    monkeypatch.setattr(config, "NOTE_QUERY_CHARS", 100)
    monkeypatch.setattr(config, "NOTE_RECALL_MAX_QUERIES", 4)
    segs = ["\n\n".join(f"第{i}段第{j}节" + "内容" * 20 for j in range(3)) for i in range(4)]
    queries = note_mod._recall_queries("整批正文", segs)
    assert len(queries) == 4
    assert all(f"第{i}段第0节" in queries[i] for i in range(4))


def test_message_count_over_cap_spreads_head_and_tail(monkeypatch):
    """消息条数本身就超上限（罕见）时均匀取样，保住首尾两组。"""
    monkeypatch.setattr(config, "NOTE_RECALL_MAX_QUERIES", 3)
    monkeypatch.setattr(config, "NOTE_QUERY_CHARS", 100)
    segs = [f"第{i}段" + "内容" * 60 for i in range(9)]
    queries = note_mod._recall_queries("整批正文", segs)
    assert len(queries) == 3
    assert "第0段" in queries[0] and "第8段" in queries[-1]


# ============ 召回（store.recall_existing_notes） ============

def test_recall_unions_segments_by_path(monkeypatch):
    """多段各查一次 → 按 path 并集、相似度取最大；同一篇被多段/多分块命中只占一个名额。"""
    monkeypatch.setattr(store_mod, "get_existing_notes",
                        lambda tech: _notes("react/use-memo.md", "react/context.md"))
    monkeypatch.setattr(vector_mod, "semantic_search_knowledge", _hits({
        "useMemo": [("knowledge/react/use-memo.md", 0.71), ("knowledge/react/use-memo.md", 0.50)],
        "Context": [("knowledge/react/use-memo.md", 0.83), ("knowledge/react/context.md", 0.66)],
    }))
    out = store_mod.recall_existing_notes("react", ["useMemo 怎么用", "Context 是什么"], 6)
    assert [n["path"] for n in out] == ["react/use-memo.md", "react/context.md"]
    assert out[0]["similarity"] == 0.83  # 同一篇取最高分，不是最后命中的那一条


def test_recall_floor_drops_irrelevant_without_falling_back(monkeypatch):
    """低于下限的候选直接丢；丢空也**不回退**填充——空是"这批内容没有相关笔记"，不是故障。"""
    monkeypatch.setattr(store_mod, "get_existing_notes",
                        lambda tech: _notes("springai/a.md", "springai/b.md"))
    monkeypatch.setattr(vector_mod, "semantic_search_knowledge",
                        lambda q, top_k=1, tech=None: [{"path": "knowledge/springai/a.md", "similarity": 0.086},
                                                       {"path": "knowledge/springai/b.md", "similarity": 0.038}])
    assert store_mod.recall_existing_notes("springai", ["完全无关的内容"], 6, sim_min=0.4) == []


def test_recall_falls_back_only_without_any_candidate(monkeypatch):
    """回退的判据是"一个候选都没有"（RAG 不可用 / 空索引），不是"过滤后为空"。"""
    monkeypatch.setattr(store_mod, "get_existing_notes",
                        lambda tech: _notes(*[f"r/{i}.md" for i in range(5)]))

    def boom(q, top_k=1, tech=None):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(vector_mod, "semantic_search_knowledge", boom)
    out = store_mod.recall_existing_notes("r", ["x"], 3, sim_min=0.4)
    assert [n["path"] for n in out] == ["r/2.md", "r/3.md", "r/4.md"]  # 最近 3 篇
    assert all(n["similarity"] is None for n in out)  # None = 没有分数，不是"相似度为零"


def test_recall_empty_when_tech_has_no_notes(monkeypatch):
    monkeypatch.setattr(store_mod, "get_existing_notes", lambda tech: [])
    assert store_mod.recall_existing_notes("none", ["x"], 3, sim_min=0.4) == []


def test_recall_caps_by_note_count_and_orders_by_similarity(monkeypatch):
    """top_k 数的是**笔记篇数**：命中再多也只取最相关的 3 篇，按相似度降序。"""
    monkeypatch.setattr(store_mod, "get_existing_notes",
                        lambda tech: _notes(*[f"t/{i}.md" for i in range(10)]))
    monkeypatch.setattr(vector_mod, "semantic_search_knowledge",
                        lambda q, top_k=1, tech=None: [{"path": f"knowledge/t/{i}.md",
                                                        "similarity": 0.95 - i * 0.05}
                                                       for i in range(10)])
    out = store_mod.recall_existing_notes("t", ["x"], 3, sim_min=0.4)
    assert [n["path"] for n in out] == ["t/0.md", "t/1.md", "t/2.md"]


def test_no_segments_falls_back_to_batch_head(monkeypatch):
    """不给分段（非自动沉淀的三个调用点）时维持旧行为：只拿批次开头截断的那一段查一次。"""
    queried: list[str] = []
    monkeypatch.setattr(config, "NOTE_QUERY_CHARS", 10)
    monkeypatch.setattr(store_mod, "get_existing_notes", lambda tech: _notes("t/a.md"))
    monkeypatch.setattr(note_mod, "get_existing_notes", lambda tech: _notes("t/a.md"))
    monkeypatch.setattr(vector_mod, "semantic_search_knowledge",
                        lambda q, top_k=1, tech=None: (queried.append(q) or
                                                       [{"path": "knowledge/t/a.md", "similarity": 0.7}]))
    monkeypatch.setattr(note_mod, "generate_text", lambda *a, **k: "[]")

    text = "前十个字之外的内容都不进查询"
    note_mod.note_pipeline("t", text)

    assert queried == [text[:10]]


# ============ 下限：不看库大小，一律生效 ============

def _kb(monkeypatch, notes: list[dict], sim_by_path: dict[str, float]) -> None:
    """把知识库与检索结果都钉成给定值（两个模块各自持有 get_existing_notes 的引用）。"""
    monkeypatch.setattr(store_mod, "get_existing_notes", lambda tech: notes)
    monkeypatch.setattr(note_mod, "get_existing_notes", lambda tech: notes)
    monkeypatch.setattr(vector_mod, "semantic_search_knowledge",
                        lambda q, top_k=1, tech=None: [{"path": f"knowledge/{p}", "similarity": s}
                                                       for p, s in sim_by_path.items()])


def _capture_llm(seen: dict):
    def fake(system: str, user: str, **kw) -> str:
        seen["user"] = user
        return "[]"
    return fake


def _last(events: list[dict], name: str) -> dict:
    return [e for e in events if e["event"] == name][-1]


def test_small_kb_still_filtered_by_floor(monkeypatch, audit_events):
    """库再小也照过滤：余弦 0.04–0.09 的笔记就是噪声。

    "小库就全给"实现过又撤掉了——给噪声可能诱发过抑制（输出 []，静默丢内容），
    而清空上下文只会多提取一次（下游有匹配与用户闸门兜底）。这里钉住撤掉后的行为。
    """
    _kb(monkeypatch, _notes("s/a.md", "s/b.md", "s/c.md"),
        {"s/a.md": 0.086, "s/b.md": 0.05, "s/c.md": 0.038})
    seen: dict = {}
    monkeypatch.setattr(note_mod, "generate_text", _capture_llm(seen))

    note_mod.note_pipeline("s", "这批内容与库里的笔记余弦都很低")

    assert "（本技术暂无已有笔记" in seen["user"]
    event = _last(audit_events, "note_recall")
    # kb_notes 让"库里本来就没笔记"和"有笔记但被下限滤掉了"能分开
    assert event["kb_notes"] == 3 and event["recalled_count"] == 0
    assert event["sim_min"] == config.NOTE_RECALL_SIM_MIN


def test_kb_size_does_not_change_the_floor(monkeypatch):
    """下限只由相似度决定：库 3 篇和库 30 篇，同样一把尺。"""
    monkeypatch.setattr(config, "NOTE_RECALL_TOP_K", 6)
    for n in (3, 30):
        notes = _notes(*[f"t/{i}.md" for i in range(n)])
        _kb(monkeypatch, notes, {f"t/{i}.md": 0.05 for i in range(n)})
        assert store_mod.recall_existing_notes("t", ["无关内容"], 6,
                                               sim_min=config.NOTE_RECALL_SIM_MIN) == []


# ============ 端到端：后段主题必须到达差量上下文 ============

def test_pipeline_later_topic_reaches_extraction_context(monkeypatch):
    """一批两个主题：后出现的那篇笔记必须进差量上下文——旧行为（只取批次开头）会漏掉它。"""
    queried: list[str] = []

    def fake_search(query: str, top_k: int = 1, tech: str | None = None) -> list[dict]:
        queried.append(query)
        if "useMemo" in query:
            return [{"path": "knowledge/react/use-memo.md", "similarity": 0.78}]
        if "Context" in query:
            return [{"path": "knowledge/react/context.md", "similarity": 0.81}]
        return []

    seen: dict = {}

    def fake_llm(system: str, user: str, **kw) -> str:
        seen["user"] = user
        return "[]"

    notes = _notes("react/use-memo.md", "react/context.md")
    monkeypatch.setattr(store_mod, "get_existing_notes", lambda tech: notes)
    monkeypatch.setattr(note_mod, "get_existing_notes", lambda tech: notes)
    monkeypatch.setattr(vector_mod, "semantic_search_knowledge", fake_search)
    monkeypatch.setattr(note_mod, "generate_text", fake_llm)

    buffer = [{"role": "assistant", "content": "useMemo 缓存计算结果。" * 10},
              {"role": "user", "content": "明白了，继续。" * 10},
              {"role": "assistant", "content": "Context 跨层级传递数据。" * 10},
              {"role": "user", "content": "那 Provider 呢？" * 10}]
    text = route_mod._sweep_buffer_text(buffer)
    note_mod.note_pipeline("react", text, query_segments=route_mod._buffer_query_segments(buffer))

    # 逐段各查一次，而不是把几段拼成一条长查询（拼了就会平均成一个向量，等于没分段）
    assert len(queried) == 2
    assert any("useMemo" in q for q in queried) and any("Context" in q for q in queried)
    prompt = seen["user"]
    assert "### use-memo" in prompt and "### context" in prompt
