"""用户水平问卷（survey）纯业务规则：字段收集 / 回答解析 / 完成判定 / 画像推导。

零 I/O、零框架依赖。固定字段（确定性收集、代码校验）按固定顺序逐个收集，
模型只负责把「当前该问的问题」问得友好、贴合画像；动态诊断题（LLM 生成、
自由文本回答）数量有界，算问卷最后一环。

answers 结构：
    {"self_level": int | None, "related": str, "goal": str,
     "time_budget": str | float, "diagnostics": [str, ...]}

goal 存用户原话（自由文本）：个性化目标直接注入 planning / coaching 提示词，
比枚举二选一信息量更大；旧数据的枚举值经 _LEGACY_GOAL_LABEL 兼容渲染。
"""

import re

# 问卷固定字段的收集顺序（对应模型逐个提问的顺序）
SURVEY_FIELDS = ("self_level", "related", "goal", "time_budget")

# 动态诊断题数量上界（问卷最后一环，由模型逐轮出题，避免无限追问）
DIAGNOSTIC_QUESTIONS_MAX = 2

# legacy 枚举值：goal 曾是「二选一」枚举，旧 profile.json / 进行中 checkpoint 可能残留，
# 仅用于渲染兼容；新收集一律存用户原话（自由文本）。
GOAL_MIN_PROJECT = "min_project"
GOAL_DEEP = "deep"

_LEGACY_GOAL_LABEL = {
    GOAL_MIN_PROJECT: "快速上手跑通最小项目",
    GOAL_DEEP: "深入原理",
}

_BUCKET_LABEL = {
    "beginner": "技术小白（少用术语、多类比、拆小步、主动补前置知识）",
    "intermediate": "有基础（可适当用术语、注重结构化）",
    "developer": "开发者（可用术语、追求效率、跳过基础直达要点）",
}

# 固定字段对应的收集提示（graph 层给模型看的"当前要问什么"，模型负责措辞）
FIELD_QUESTIONS = {
    "self_level": "0-10 数字自评对该技术的熟悉程度",
    "related": "熟悉哪些相关技术（自由文本，没有就填「无」）",
    "goal": "这次学习的主要目标是什么（自由描述学完后想做到什么，越具体越好，"
            "如「能看懂项目代码并做小改动」「能独立写个小工具」）",
    "time_budget": "每天大概能投入多少小时学习",
}


def parse_answer_for_field(field: str, reply: str) -> tuple[object | None, str | None]:
    """把用户对某个固定字段的回答解析成规范值。

    Returns:
        (value | None, error | None)：None + error 表示无法解析，需要重问。
    """
    text = (reply or "").strip()
    if field == "self_level":
        # 注意不能用 \b：Python 正则把中文视为 \w，"大概6分"里 6 两侧都不是词边界
        m = re.search(r"(\d{1,2})(?!\d)", text)
        if not m:
            return None, "请输入 0-10 之间的数字（如 5）"
        level = int(m.group(1))
        if level > 10:
            return None, "请输入 0-10 之间的数字（如 5）"
        return level, None
    if field == "related":
        if not text:
            return None, "请简单说下你熟悉的相关技术（没有就填「无」）"
        return text, None
    if field == "goal":
        # 自由文本：原样保存用户的目标原话（同 related 的非空校验），不做枚举归类
        if not text:
            return None, "请用自己的话说下这次学习的目标（学完后你想做到什么）"
        return text, None
    if field == "time_budget":
        m = re.search(r"(\d+(?:\.\d+)?)\s*小时", text)
        if m:
            return float(m.group(1)), None
        if not text:
            return None, "请告诉我每天大概能投入多少小时（如 2）"
        return text, None  # 兜底存原文
    return None, f"未知字段: {field}"


def apply_answer(answers: dict, field: str, reply: str) -> tuple[dict, str | None]:
    """把回答解析后写入 answers（返回新 dict，不修改入参）。解析失败返回原 answers + error。"""
    value, err = parse_answer_for_field(field, reply)
    if err:
        return answers, err
    new = dict(answers)
    new[field] = value
    return new, None


def next_field(answers: dict) -> str | None:
    """返回下一个待收集的固定字段；全部收齐返回 None（进入动态诊断题阶段）。"""
    for f in SURVEY_FIELDS:
        if answers.get(f) is None:
            return f
    return None


def is_fixed_done(answers: dict) -> bool:
    return next_field(answers) is None


def diagnostics_done(answers: dict) -> bool:
    """动态诊断题是否已收集够数量。"""
    return len(answers.get("diagnostics") or []) >= DIAGNOSTIC_QUESTIONS_MAX


def is_survey_complete(answers: dict) -> bool:
    """问卷完成 = 固定字段收齐 且 动态诊断题够数。"""
    return is_fixed_done(answers) and diagnostics_done(answers)


def derive_profile(answers: dict, tech: str) -> dict:
    """根据问卷答案推导用户画像（含小白/开发者分档，供 coach 提示词差异化）。"""
    level = answers.get("self_level")
    if level is None:
        bucket = "beginner"
    elif level <= 3:
        bucket = "beginner"
    elif level <= 6:
        bucket = "intermediate"
    else:
        bucket = "developer"
    return {
        "tech": tech,
        "self_level": level,
        "related": answers.get("related") or "",
        "goal": answers.get("goal"),
        "time_budget": answers.get("time_budget"),
        "bucket": bucket,
        "diagnostics": list(answers.get("diagnostics") or []),
    }


def profile_summary(profile: dict) -> str:
    """把画像渲染成一句话摘要（注入 coach / planning 提示词）。"""
    level = profile.get("self_level")
    parts = [
        f"技术：{profile.get('tech') or ''}",
        f"自评熟悉度：{'未填' if level is None else f'{level}/10'}",
        f"画像：{_BUCKET_LABEL.get(profile.get('bucket'), profile.get('bucket'))}",
    ]
    if profile.get("related"):
        parts.append(f"相关技术：{profile['related']}")
    goal = profile.get("goal")
    if goal:
        parts.append(f"目标：{_LEGACY_GOAL_LABEL.get(goal, goal)}")
    if profile.get("time_budget") is not None:
        parts.append(f"时间预算：{profile['time_budget']}")
    return "；".join(parts)
