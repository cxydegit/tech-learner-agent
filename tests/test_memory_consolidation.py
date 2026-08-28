"""记忆系统 Step 4 单测：三舱记忆整理（consolidate_memory + coach_trim 三舱写入 + coaching 提示词注入）。

零网络：monkeypatch generate_text。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_memory_consolidation.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.graph as graph_mod
import src.pipelines.route as route_mod
from src.config import config


_OK_JSON = ('{"facts_add": ["用户偏好类比讲解"], "open_add": ["AOP 切面顺序还没搞懂"], '
            '"resolved": [2], "context": "最近学了 Spring AOP。"}')

_MSGS = [{"role": "user", "content": "问题一"}, {"role": "assistant", "content": "讲解一"}]

_EXISTING = {
    "facts": ["用户每天 2 小时"],
    "open_items": [{"id": 1, "text": "待讲：事务传播"}, {"id": 2, "text": "待确认：路线是否跳过 Maven"}],
    "summary": "旧摘要",
}


def _mem(over=None):
    base = {"facts": list(_EXISTING["facts"]),
            "open_items": [dict(x) for x in _EXISTING["open_items"]],
            "summary": _EXISTING["summary"]}
    base.update(over or {})
    return base


# ============ 增量应用（确定性） ============

def test_consolidate_applies_deltas(monkeypatch):
    """facts 追加、open 追加 id 连续、resolved 按 id 移除、context 覆盖。"""
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u: _OK_JSON)
    out = route_mod.consolidate_memory(_mem(), _MSGS, "Spring")
    assert out["facts"] == ["用户每天 2 小时", "用户偏好类比讲解"]
    # id=2 已解决被移除；新增 id 从现有最大（2）之后连续
    assert out["open_items"] == [{"id": 1, "text": "待讲：事务传播"},
                                 {"id": 3, "text": "AOP 切面顺序还没搞懂"}]
    assert out["summary"] == "最近学了 Spring AOP。"


def test_consolidate_dedups_facts(monkeypatch):
    """facts 精确去重：与现有重复（strip 后相同）不追加。"""
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u:
                        '{"facts_add": ["用户每天 2 小时", "  用户每天 2 小时  ", "新事实"], '
                        '"open_add": [], "resolved": [], "context": ""}')
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out["facts"] == ["用户每天 2 小时", "新事实"]


def test_consolidate_resolved_unknown_id_ignored(monkeypatch):
    """resolved 含不存在的 id → 忽略，不误删、不报错。"""
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u:
                        '{"facts_add": [], "open_add": [], "resolved": [99, "x", 1], "context": ""}')
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out["open_items"] == [{"id": 2, "text": "待确认：路线是否跳过 Maven"}]


def test_consolidate_empty_context_keeps_old_summary(monkeypatch):
    """context 为空（LLM 没给）→ 保留旧摘要，不莫名清空。"""
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u:
                        '{"facts_add": [], "open_add": [], "resolved": [], "context": ""}')
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out["summary"] == "旧摘要"


# ============ 机械上限 ============

def test_consolidate_facts_cap_drops_oldest(monkeypatch):
    """facts 超上限丢最旧。"""
    monkeypatch.setattr(config, "COACH_FACTS_MAX", 2)
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u:
                        '{"facts_add": ["事实A", "事实B"], "open_add": [], "resolved": [], "context": ""}')
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out["facts"] == ["事实A", "事实B"]  # 旧的「用户每天 2 小时」被挤出


def test_consolidate_open_cap_drops_oldest(monkeypatch):
    """未决超上限丢最旧（id 最小）。"""
    monkeypatch.setattr(config, "COACH_OPEN_MAX", 2)
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u:
                        '{"facts_add": [], "open_add": ["新未决A", "新未决B"], "resolved": [], "context": ""}')
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    ids = [it["id"] for it in out["open_items"]]
    assert ids == sorted(ids) and len(ids) == 2
    assert 1 not in ids  # 最旧的 id=1 被挤出


def test_consolidate_context_truncated(monkeypatch):
    """context 超 COACH_SUMMARY_MAX_CHARS 机械截断。"""
    monkeypatch.setattr(config, "COACH_SUMMARY_MAX_CHARS", 10)
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u:
                        '{"facts_add": [], "open_add": [], "resolved": [], '
                        '"context": "这是一个超过十个字的摘要内容用于测试截断行为"}')
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out["summary"] == "这是一个超过十个字的摘"[:10]
    assert len(out["summary"]) == 10


# ============ 降级（安全侧） ============

def test_consolidate_llm_failure_keeps_all(monkeypatch):
    """LLM 异常 → 三舱原样保留。"""

    def boom(s, u):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(route_mod, "generate_text", boom)
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out == {"facts": ["用户每天 2 小时"],
                   "open_items": _EXISTING["open_items"], "summary": "旧摘要"}


def test_consolidate_parse_failure_keeps_all(monkeypatch):
    """JSON 解析失败（纯文本输出）→ 三舱原样保留。"""
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u: "这不是 JSON，是普通摘要文本。")
    out = route_mod.consolidate_memory(_mem(), _MSGS, "X")
    assert out["facts"] == ["用户每天 2 小时"]
    assert out["summary"] == "旧摘要"
    assert len(out["open_items"]) == 2


def test_consolidate_empty_messages_no_llm(monkeypatch):
    """无 user/assistant 文本消息 → 不调 LLM，三舱原样。"""
    called = {}
    monkeypatch.setattr(route_mod, "generate_text",
                        lambda s, u: called.setdefault("c", True) or _OK_JSON)
    out = route_mod.consolidate_memory(_mem(), [{"role": "system", "content": "内部提示"}], "X")
    assert not called
    assert out["facts"] == ["用户每天 2 小时"]


# ============ coaching 提示词注入（含画像缺口修复） ============

def test_coaching_prompt_injects_profile_facts_open(monkeypatch):
    """coaching 提示词注入画像（修复缺口）+ 事实舱 + 未决舱 + 脉络。"""
    state = {
        "mode": "coaching", "roadmap": None, "coach_summary": "最近焦点",
        "learner_profile": {"tech": "Spring", "self_level": 3, "bucket": "beginner",
                            "goal": "min_project", "time_budget": 2},
        "coach_facts": ["用户偏好类比讲解", "不要用英文术语"],
        "coach_open_items": [{"id": 3, "text": "AOP 切面顺序还没搞懂"}],
    }
    prompt = route_mod._coaching_prompt(state, "Spring")
    assert "用户画像：" in prompt
    assert "技术小白" in prompt  # profile_summary 渲染了画像
    assert "已确认的事实与偏好：" in prompt
    assert "- 用户偏好类比讲解" in prompt
    assert "未决事项" in prompt
    assert "- [3] AOP 切面顺序还没搞懂" in prompt
    assert "此前对话摘要：最近焦点" in prompt


def test_coaching_prompt_empty_tiers_not_rendered():
    """三舱全空 + 无画像 → 对应块不渲染（不出现空标题）。"""
    state = {"mode": "coaching", "roadmap": None, "coach_summary": ""}
    prompt = route_mod._coaching_prompt(state, "Spring")
    assert "用户画像" not in prompt
    assert "已确认的事实与偏好" not in prompt
    assert "未决事项" not in prompt
    assert "此前对话摘要" not in prompt


# ============ coach_trim 三舱写入 ============

def test_trim_writes_three_tiers(monkeypatch):
    """压缩触发：三舱都写入、消息裁剪；LLM 失败时三舱原样、消息仍裁剪。"""
    msgs = [{"role": "user", "content": f"消息{i}"} for i in range(config.COACH_COMPRESS_AT + 5)]
    monkeypatch.setattr(route_mod, "generate_text", lambda s, u: _OK_JSON)
    out = graph_mod.coach_trim({"mode": "coaching", "coach_messages": msgs,
                                "coach_summary": "旧摘要", "tech": "X",
                                "survey_answers": {},
                                "coach_facts": ["已有事实"],
                                "coach_open_items": [{"id": 2, "text": "旧未决"}]})
    assert out["coach_facts"] == ["已有事实", "用户偏好类比讲解"]
    assert out["coach_summary"] == "最近学了 Spring AOP。"
    assert all(it["id"] != 2 for it in out["coach_open_items"])
    assert len(out["coach_messages"]) <= config.COACH_HISTORY_KEEP * 2

    def boom(s, u):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(route_mod, "generate_text", boom)
    out2 = graph_mod.coach_trim({"mode": "coaching", "coach_messages": msgs,
                                 "coach_summary": "旧摘要", "tech": "X",
                                 "survey_answers": {},
                                 "coach_facts": ["已有事实"],
                                 "coach_open_items": [{"id": 2, "text": "旧未决"}]})
    assert out2["coach_facts"] == ["已有事实"]  # 三舱原样
    assert out2["coach_summary"] == "旧摘要"
    assert len(out2["coach_messages"]) <= config.COACH_HISTORY_KEEP * 2  # 消息仍裁剪
