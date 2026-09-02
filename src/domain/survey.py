"""用户水平问卷（survey）纯业务规则：字段收集 / 回答解析 / 完成判定 / 画像推导。

零 I/O、零框架依赖。固定字段（确定性收集、代码校验）按固定顺序逐个收集，
模型只负责把「当前该问的问题」问得友好、贴合画像；动态诊断题（LLM 生成、
用户作答）数量有界，算问卷最后一环。

answers 结构：
    {"self_level": int | None, "related": str, "goal": str,
     "time_budget": str | float,
     "diagnostics": [{"question": str, "answer": str,
                      "correct": str | None, "grade": None | "right" | "wrong"}, ...]}

goal 存用户原话（自由文本）：个性化目标直接注入 planning / coaching 提示词，
比枚举二选一信息量更大；旧数据的枚举值经 _LEGACY_GOAL_LABEL 兼容渲染。

诊断题判定（确定性，零 LLM 调用）：模型出**单选选择题**并在题目末尾内嵌标准答案
「【答案】X」，graph 展示前剥离该行（用户看不到答案），用户回复选项字母后由代码
比对正确性，判 right/wrong 二值。正确性直接注入 planning/coaching 提示词，校准
「自评 vs 实际水平」。旧数据 diagnostics 为字符串列表时跳过判定，不误报。
"""

import re

# 问卷固定字段的收集顺序（对应模型逐个提问的顺序）
SURVEY_FIELDS = ("self_level", "related", "goal", "time_budget")

# 动态诊断题数量上界（问卷最后一环，由模型逐轮出题，避免无限追问）
DIAGNOSTIC_QUESTIONS_MAX = 2

# 诊断题判定（二值）：模型出单选选择题并内嵌标准答案，代码剥离后展示、比对用户回复的
# 选项字母，确定性判对/错——不需要 LLM 再判一次（模型出题时已知道答案，代码只需比对）。
GRADE_RIGHT = "right"
GRADE_WRONG = "wrong"

# 题目中标准答案的内嵌标记（模型出题时必须附带，graph 展示前剥离，用户看不到）
_DIAG_ANSWER_RE = re.compile(r"【答案】\s*([A-Da-d])")


def extract_diag_answer(text: str) -> tuple[str, str | None]:
    """从模型出的诊断题文本解析内嵌标准答案，返回 (剥离答案后的题目, 答案字母大写|None)。

    模型出题须在末尾写「【答案】X」（X 为 A/B/C/D 之一）。该行仅供系统读取：graph 展示前
    用本函数剥离，用户看不到答案；coach_survey 再用它从原消息解析题目+答案做判定。
    未内嵌标记 → 原样返回 + None（该题无法判定，降级不误判）。纯函数、幂等。
    """
    m = _DIAG_ANSWER_RE.search(text or "")
    if not m:
        return (text or "").strip(), None
    return _DIAG_ANSWER_RE.sub("", text or "").strip(), m.group(1).upper()


def parse_diag_choice(reply: str) -> str | None:
    """从用户对诊断题的回答中提取选项字母（A-D 大写）。容忍「选B」「答案是B」等变体。

    提取不到字母（空答 / 答非所问 / 未按格式回）→ None，判定为「无法判定」而非「错」。
    """
    m = re.search(r"[A-Da-d]", reply or "")
    return m.group(0).upper() if m else None

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
    if level is None or level <= 3:
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


def _graded_diagnostics(diagnostics) -> list[dict]:
    """过滤出有判定的诊断题条目（dict 且 grade 非空）。兼容旧 [str] 数据：字符串条目跳过。"""
    return [d for d in (diagnostics or []) if isinstance(d, dict) and d.get("grade")]


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
    # 诊断自测（仅内部信号）：让 planning/coaching 看到「自评 vs 实际答对几题」的反差，
    # 据以校准路线粒度。旧 [str] 条目 / 未判定的条目不渲染、不误报。
    graded = _graded_diagnostics(profile.get("diagnostics"))
    if graded:
        right = sum(1 for d in graded if d["grade"] == GRADE_RIGHT)
        marks = "、".join(
            f"第{i + 1}题{'对' if d['grade'] == GRADE_RIGHT else '错'}"
            for i, d in enumerate(graded))
        parts.append(f"诊断自测：{len(graded)}题中{right}对、{len(graded) - right}错（{marks}）")
    return "；".join(parts)
