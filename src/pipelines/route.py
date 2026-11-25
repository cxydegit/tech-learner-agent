"""coach agent 循环的提示词 + 工具实现（定制化学习路线）。

定位：pipelines 层，prompts 就近存放；graph.py 的 coach 节点组负责循环编排，
本模块只提供"单次往返"需要的东西——按 mode 的系统提示词、各模式可用工具
schema、工具执行（大内容写文件、出入参短，工具可经 ctx.updates 请求状态变更）。

三阶段范围：survey（问卷）→ planning（路线生成/确认）→ coaching（执行陪练）。
"""

import re
import time
from datetime import timedelta

from ..adapters import learner
from ..adapters import memory as memory_store
from ..adapters.audit import audit
from ..adapters.llm import generate_text
from ..config import config
from ..domain import exit_intent, survey
from ..domain import memory as memory_domain
from ..domain import roadmap as roadmap_domain
from ..domain.extraction import parse_json_object
from .collect import CollectStageError, collect_pipeline
from .note import (
    format_merge_candidates,
    note_pipeline,
    parse_merge_decision,
    persist_points,
)
from .qa import _search_notes, note_key, qa_pipeline
from .read import ReadStageError, read_pipeline

# ============================================================
# coach 系统提示词（按 mode）
# ============================================================

_FIELD_LABELS = {
    "self_level": "自评熟悉度（0-10）",
    "related": "相关技术",
    "goal": "学习目标",
    "time_budget": "时间预算",
}
_FIELD_HINTS = {
    "self_level": "0-10 之间的数字",
    "related": "自由文本（没有就填「无」）",
    "goal": "自由文本：学完后想做到什么（一句话，越具体越好，原样记录用户原话）",
    "time_budget": "每天小时数（如 2）",
}


# 规则行把证据语义和「历史 vs 当前」说死：未考据不等于已掌握，历史路线点不算当前进度。
_LTM_RULES = {
    "survey": (
        "长期记忆简报（跨会话）：以下是学习者已有证据和跨技术背景，用于校准自评、"
        "选择类比和出针对性诊断题。未考据的点只说明曾接触过，不等于已掌握；"
        "不要据此跳过必要提问，也不要让用户重复回答已有记录的信息。"),
    "planning": (
        "长期记忆简报（跨会话）：只把标为「已验收」「已声明」的点视为可跳过；"
        "标为「未考据」「验收降级」的点仍需验证，不能据此跳过。"
        "历史路线点不等于当前路线进度。"),
    "coaching": (
        "跨会话长期记忆（只补画像/当前路线没有的异常与稳定偏好；"
        "与本次画像/当前路线冲突时以本次为准）："),
}


def long_term_memory_text(state, mode: str | None = None) -> str:
    """返回本次追加进提示词的长期记忆简报原文（无内容返回空串，提示词不留空块）。

    提示词构建与 `coach_llm` 的边界调试落盘共用本函数，避免日志里记录的是
    「渲染块」而实际提示词拼的是另一份文本。放提示词尾部：简报不常变时，
    整段前缀仍能命中 prompt cache。
    """
    resolved = mode or state.get("mode") or "survey"
    text = str((state.get("memory_briefing") or {}).get("text") or "")
    if not text:
        return ""
    return "\n\n" + _LTM_RULES.get(resolved, _LTM_RULES["survey"]) + "\n" + text


def recall_hint() -> str:
    """按需记忆工具的提示词规则（仅 flag 开启时使用）。"""
    if not config.LTM_RECALL_ENABLED:
        return ""
    return (
        f"当前日期 {time.strftime('%Y-%m-%d')}。用户问过去学过什么/撤销过什么/"
        "跨技术经历时，用 recall_memory 查询；未查询前不要断言没有记录。"
        "用户未点名其他技术时，tech 用当前会话技术。"
        "没确认掌握=unknown；当前学过=active；以前/旧值=superseded；"
        "取消/放弃=revoked；所有历史才用 history。"
        "status=any 只在用户明确问所有历史时用；当前/每天/现在用 active。"
        "例：以前的目标→kind=goal,status=history；以前自评→kind=fact,"
        "status=superseded,key=self_level；以前的相关背景→kind=fact,"
        "status=superseded,key=related；每天多少时间→kind=fact,status=active,"
        "key=time_budget；框架对比→query=对比。")


def _survey_prompt(state, tech: str) -> str:
    answers = state.get("survey_answers") or {}
    field = state.get("survey_field")
    profile = state.get("learner_profile") or {}
    field_label = _FIELD_LABELS.get(field) if field else "动态诊断题"
    answers_txt = "；".join(f"{k}={v}" for k, v in answers.items()) or "（暂无）"
    profile_txt = survey.profile_summary(profile) if profile else "（尚未推导）"
    diag_remaining = max(survey.DIAGNOSTIC_QUESTIONS_MAX - len(answers.get("diagnostics") or []), 0)
    lines = [
        f"你是「技术学习陪练」的水平探测助手。用户要学习：{tech}。",
        "通过一轮简短问卷了解用户水平，为后续定制学习路线收集信息。",
        "",
        "当前问卷状态：",
        f"- 正在收集字段：{field_label}",
        f"- 已收集：{answers_txt}",
        f"- 用户画像（参考调整语气）：{profile_txt}",
        "",
        "规则：",
        f"1. 一次只问一个问题，且只问「正在收集字段」对应的问题：{field_label}。",
    ]
    if field:
        lines.append(f"2. 该字段用户需要回答成：{_FIELD_HINTS.get(field)}。")
        lines.append("3. 只输出问题本身，不要编号、解释或寒暄；根据画像调整语气（小白少用术语、多类比）。")
    else:
        lines.append(f"2. 固定字段已收齐，进入动态出题：出 1 道与「{tech}」相关的单选选择题（4 个选项、只有一个正确答案），检验前置知识；还需 {diag_remaining} 道。")
        lines.append("3. 只输出题目本身：题干 + 4 个选项（A/B/C/D），末尾单独一行「【答案】X」标注正确答案（X 为 A/B/C/D 之一，该行仅供系统读取、不会展示给用户），并在题干末尾引导用户「请直接回复选项字母（如 B）」；根据画像调整难度与语气，不要编号、解释或寒暄。")
    lines.append("4. 若收到一条【问卷校验】内部提示，说明用户上一条回答格式不对，请按提示重新问同一个字段。")
    lines.append("5. 用户主动说出长期有效的偏好/约束/目标时，用 remember 记录；问卷字段由系统记录，不要用 remember 重复记录；一次性要求不要记录。")
    if config.LTM_RECALL_ENABLED:
        lines.append(f"6. {recall_hint()}")
    ltm = long_term_memory_text(state, "survey")
    if ltm:
        lines.append(ltm)
    return "\n".join(lines)


def _planning_prompt(state, tech: str) -> str:
    profile = state.get("learner_profile") or {}
    summary = survey.profile_summary(profile) if profile else "（问卷信息缺失）"
    conv = (state.get("coach_summary") or "").strip()
    conv_block = f"\n此前对话摘要：{conv}\n" if conv else ""
    ltm = long_term_memory_text(state, "planning")
    recall_rule = f"\n- {recall_hint()}" if config.LTM_RECALL_ENABLED else ""
    return (
        f"你是「技术学习陪练」的路线规划助手。用户要学习：{tech}。\n"
        f"用户水平画像：{summary}{conv_block}\n"
        "任务：为用户生成一份个性化、可执行的分阶段学习路线（3-5 个阶段，每阶段含可检验的里程碑）。\n"
        "步骤：\n"
        "1. 调用 generate_roadmap 生成路线（goal / total_hours / stages；stages 含 name/goal/materials/est_hours/milestones）。\n"
        "2. 把生成后的路线（阶段、里程碑、预估时长）用 Markdown 完整呈现给用户，请用户确认或提出修改。\n"
        "3. 用户确认后调用 confirm_roadmap 进入执行阶段；用户提出修改时，带上修改意见重新调用 generate_roadmap。\n"
        "规则：\n"
        "- 路线必须贴合用户水平：技术小白从环境搭建和最小 demo 起步、拆小步；开发者可跳过基础直达要点。\n"
        "- 里程碑必须是可检验的动作（如「本地跑通 hello world」「能用 XX 完成一个接口」），不要空洞目标；"
        "尽量写成对话内可检验的形式（在对话中能给出讲解/回答/结论/代码），便于后续验收勾选。\n"
        "- 里程碑描述只写「要掌握的知识点 / 要完成的动作」，不要写成「请用户复述 / 说出 / 讲一遍 XX」这类"
        "以用户口头输出为完成条件的措辞；讲解与核对由助手负责，描述里不规定用户必须输出什么。\n"
        "- 阶段按依赖排序，每阶段给出预估小时数。\n"
        "- materials 可引用已收集的资料或留空（留空由后续 collect 补充）。\n"
        "- 用户提出长期有效的偏好/约束/目标时，用 remember 记录（只记稳定信息，不记一次性要求）；已有 key 尽量复用。\n"
        "- 不要编造资料链接；没有的信息写「待收集」。"
        + recall_rule
        + ltm
    )


def _coaching_prompt(state, tech: str) -> str:
    """coaching 系统提示词：只放会话级稳定内容（角色 / 规则 / 画像 / 路线 / 三舱）。

    每轮变化的内容（kb 片段 / 里程碑卡点）不在这里：system 里每轮必变的 token 会让
    前缀缓存从 system 就断掉，其后整段对话历史全部重新计费。它们改由
    coach_runtime_context 拼成请求末尾的注入消息。
    拼接顺序按「越稳定越靠前」：静态角色与规则在前，画像 / 路线 / 三舱按变更频率递增
    排列——勾选里程碑或触发压缩的那几轮，最长的规则前缀仍能命中缓存。
    """
    profile = state.get("learner_profile") or {}
    profile_block = f"用户画像：{survey.profile_summary(profile)}\n" if profile else ""
    roadmap = state.get("roadmap")
    prog = ""
    if roadmap:
        prog = "当前路线：\n" + roadmap_domain.roadmap_to_markdown(roadmap) + "\n"
    # 三舱注入（画像与摘要解耦，避免 coaching 模式丢失画像上下文）
    facts = state.get("coach_facts") or []
    facts_block = ""
    if facts:
        facts_block = "已确认的事实与偏好：\n" + "\n".join(f"- {f}" for f in facts) + "\n"
    open_items = state.get("coach_open_items") or []
    open_block = ""
    if open_items:
        open_block = ("未决事项（待跟进，解决后移除）：\n"
                      + "\n".join(f"- [{it['id']}] {it['text']}" for it in open_items) + "\n")
    conv = (state.get("coach_summary") or "").strip()
    conv_block = f"此前对话摘要：{conv}\n" if conv else ""
    ltm_block = long_term_memory_text(state, "coaching")
    blocks = "".join((profile_block, prog, ltm_block, facts_block,
                      open_block, conv_block)).strip()
    suffix = f"\n\n{blocks}" if blocks else ""
    recall_rule = f"\n- {recall_hint()}" if config.LTM_RECALL_ENABLED else ""
    return (
        f"你是「技术学习陪练」的执行陪练。用户学习：{tech}。\n\n"
        "你可以用工具推进学习：collect（收集资料）/ read（解读文档）/ "
        "ask（问已学笔记）/ get_roadmap / update_roadmap（勾选里程碑）/ "
        "revise_roadmap（按用户要求调整路线，保留已完成进度）。"
        "学习内容会自动沉淀进知识库（系统后台处理），无需调用 note 工具。"
        "原则：先给用户清晰的下一步，用户确认后再用工具；工具结果要提炼成对用户有用的信息，"
        "不要原样堆砌。里程碑完成时用 update_roadmap 勾选；用户要求调整路线时，"
        "先确认改动点再调用 revise_roadmap，改完把新路线完整呈现给用户。"
        "用户明确说出长期有效的偏好/约束/目标时，调用 remember 记录；"
        "「下次讲 X」用 remember 的 open_item 打开，解决后用 close 关闭；"
        "只记稳定信息，不记一次性要求；remember 返回 conflict 时如实告知用户旧值已更新。"
        "\n里程碑推进规则："
        "\n- 勾选里程碑前，逐项核对该里程碑的所有待办是否真实完成；有任何未完成就先完成它，不要勾选、不要推进。"
        "\n- 勾选哪个里程碑：按「刚讲完、用户刚确认的内容」匹配里程碑描述，不要默认勾 current_stage 里第一个未完成项。"
        "路线指针会跟随你勾选的里程碑所属阶段前移；前面阶段未完成的项属于遗留，除非用户明确要求回头补，"
        "否则不要在讲新阶段时去勾旧项。"
        "\n- 勾选（done=true）由系统自动核对对话记录验收：待办内容必须实质出现在对话里才算完成，"
        "仅宣布完成、布置了没人做的任务会被拒绝。不要为凑证据重复输出已讲过的内容；被拒后按提示先补内容再勾选。"
        "\n- 用户在回答里程碑确认时，按他说的走：说了「直接勾/勾了吧」→ 调 update_roadmap（系统跳过验收直接勾）；"
        "表达了自己已掌握/已完成（「我会了」「搞定了」「我脑子里过了一遍」「不一一回答你了」）→ 调 update_roadmap 勾选；"
        "只说了「直接下一步/直接进入下一阶段」而没说自己已掌握 → 不要调 update_roadmap，直接按指令往下讲，"
        "并如实说明「该里程碑我未勾选，后续你可以随时命令我勾选」。"
        "\n- 不要向用户索要「补一句话」式的关键词，也不要要求用户复述、回答或自证掌握来配合勾选，"
        "更不要向用户解释验收机制。认知型待办由你讲透即可，讲解是否达标由系统核对，不转嫁给用户。"
        "\n- 同一阶段内可以连续勾选多个里程碑，但不要跨阶段勾选；一个阶段勾完后停下来向用户总结成果，并询问是否进入下一阶段；未经用户明确同意不要开始下一阶段内容。"
        "\n- 例外：用户上一轮已明确指令「直接进入下一步/下一阶段/不用再问」时，勾选后直接推进，不再询问。"
        "这条例外只免除勾选之后的确认询问，不免除勾选之前的验收：证据不足仍会被驳回。"
        + recall_rule
        + suffix
    )


# 注入消息开头的固定标识：graph 拼请求、测试定位注入消息都靠它（不要改动措辞）。
RUNTIME_CONTEXT_MARK = "（系统注入的运行时状态，非用户发言）"


def coach_runtime_context(state) -> dict | None:
    """拼「本轮临时上下文」注入消息（kb 片段 + 里程碑卡点），两者皆无时返回 None。

    这些内容每轮必变（kb）或偶发出现（卡点），放 system 会让前缀缓存每轮从 system
    就断掉；改为请求末尾的一条 system 注入消息，由 graph.coach_llm 拼进本次请求、
    **用完即弃，绝不落盘 coach_messages**——旧轮注入是过期指令，落盘还会随轮次
    无限膨胀。中部/末尾 system 消息在本代码库是既有模式（sweep/note 结果、空输出
    重试提示都这样注入），DashScope 兼容接口接受；末尾位置同时让卡点指令离生成
    位置最近，比埋在 system 中部更显眼。
    """
    parts: list[str] = []
    # 确定性读路由——命中知识库时把相关片段注入作答上下文（来源由代码标注）
    kb = state.get("kb_context") or []
    if kb:
        blocks = [f"（来源：{it.get('path', '')}）\n{it.get('snippet', '')}" for it in kb]
        parts.append("===== 知识库相关片段（按来源标注） =====\n"
                     + "\n\n".join(blocks)
                     + "\n===== 片段结束 =====\n"
                     "优先依据上述片段作答，引用时标注来源；片段未覆盖的部分可用你自己的知识补充，"
                     "但必须明确区分「来自你笔记的内容」与「基于经验的讲解」。")
    # 里程碑推进卡点：刚勾选了一个里程碑、尚未获得用户推进确认 → 强制停下总结 + 询问。
    # 结构保证（代码设置、用户回复后清除）：Agent 勾选后不会自行开始下一里程碑。
    pending = state.get("coach_milestone_pending")
    if pending:
        count = int(state.get("coach_milestone_turn_count") or 0)
        checked = (f"本回合已勾选 {count} 个里程碑（最后一个是「{pending}」）" if count > 1
                   else f"你刚勾选了里程碑「{pending}」")
        parts.append(f"{checked}，尚未获得用户推进确认。\n"
                     "请向用户总结本阶段成果，并询问是否进入下一阶段；\n"
                     "在用户明确同意前，不要开始下一阶段的内容。")
    if not parts:
        return None
    return {"role": "system", "content": RUNTIME_CONTEXT_MARK + "\n\n" + "\n\n".join(parts)}


def coach_system_prompt(state) -> str:
    """按 mode 挑选 coach 系统提示词（survey / planning / coaching）。"""
    mode = state.get("mode") or "survey"
    tech = state.get("tech") or ""
    if mode == "planning":
        return _planning_prompt(state, tech)
    if mode == "coaching":
        return _coaching_prompt(state, tech)
    return _survey_prompt(state, tech)


# ============================================================
# 工具 schema（OpenAI function calling 格式）
# ============================================================

GENERATE_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_roadmap",
        "description": "根据用户水平画像生成或修订学习路线（阶段+里程碑）。修订时把改动说明写进 revision，stages 给出完整新结构。",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "学习总目标（一句话，可复用问卷目标）"},
                "total_hours": {"type": "integer", "description": "预估总学习时长（小时）"},
                "revision": {"type": "string", "description": "修订说明；首次生成填空字符串"},
                "stages": {
                    "type": "array",
                    "description": "学习阶段列表（3-5 个，按依赖排序）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "阶段名（如 环境搭建 / 核心概念 / 实战项目 / 进阶调优）"},
                            "goal": {"type": "string", "description": "本阶段目标"},
                            "materials": {"type": "string", "description": "推荐资料（可留空由后续 collect 补充）"},
                            "est_hours": {"type": "integer", "description": "本阶段预估小时数"},
                            "milestones": {
                                "type": "array",
                                "description": "可检验的里程碑（完成一个可检验动作才算通关）",
                                "items": {"type": "object",
                                          "properties": {"desc": {"type": "string"}},
                                          "required": ["desc"]},
                            },
                        },
                        "required": ["name", "goal", "est_hours", "milestones"],
                    },
                },
            },
            "required": ["goal", "total_hours", "stages"],
        },
    },
}

CONFIRM_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "confirm_roadmap",
        "description": "用户已确认路线，进入执行阶段。调用前必须先把路线完整呈现给用户并获得明确确认。",
        "parameters": {"type": "object", "properties": {}},
    },
}

GET_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_roadmap",
        "description": "获取当前学习路线（阶段 / 当前阶段 / 里程碑进度）。",
        "parameters": {"type": "object", "properties": {}},
    },
}

COLLECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "collect",
        "description": "搜索并筛选某个技术的学习资料，生成资料清单报告（保存到 materials/）。耗时较长，会推送进度。",
        "parameters": {"type": "object", "properties": {
            "tech": {"type": "string", "description": "技术名称（如 Spring Boot）"},
            "focus": {"type": "string", "description": "可选，关注点（如 异步编程）"},
        }, "required": ["tech"]},
    },
}

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "抓取并解读一篇技术文档（URL），生成结构化解读报告（保存到 reports/）。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "文档完整 URL"},
        }, "required": ["url"]},
    },
}

NOTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note",
        "description": "把一段学习内容沉淀为知识笔记（差量提取；有相似笔记时返回候选待用户确认，用户决定后再调 note_commit）。content 必须短（≤2000 字）。",
        "parameters": {"type": "object", "properties": {
            "tech": {"type": "string", "description": "技术名称"},
            "content": {"type": "string", "description": "本次要沉淀的学习内容（较短，≤2000 字）"},
        }, "required": ["tech", "content"]},
    },
}

NOTE_COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note_commit",
        "description": "用户对相似笔记候选做出决定后提交沉淀。decision 传用户原话（如 all / 1,3 / skip）。",
        "parameters": {"type": "object", "properties": {
            "decision": {"type": "string", "description": "用户原话：all 全合并 / 编号逗号分隔逐条（如 1,3）/ skip 全部跳过"},
        }, "required": ["decision"]},
    },
}

ASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask",
        "description": "向用户的知识笔记库提问并综合回答（跨笔记联想检索，带来源标注）。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "要问的问题"},
        }, "required": ["question"]},
    },
}

UPDATE_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_roadmap",
        "description": "勾选/取消勾选一个里程碑。按里程碑描述匹配你刚讲完、用户刚确认的那块内容"
                       "（如刚讲完 JSX 本质就勾 s2-m2），不要按 current_stage 的顺序猜下一个。"
                       "勾选（done=true）由系统自动核对对话记录验收：内容没有实质出现在对话里"
                       "会被拒绝，无需也不能自行提交证据。current_stage 会跟随你勾选的里程碑所属阶段前移。",
        "parameters": {"type": "object", "properties": {
            "milestone_id": {"type": "string", "description": "里程碑 id（如 s1-m1）"},
            "done": {"type": "boolean", "description": "true 勾选 / false 取消"},
        }, "required": ["milestone_id"]},
    },
}

REVISE_ROADMAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "revise_roadmap",
        "description": "按用户要求修订当前学习路线（增删/调整阶段、改总目标、调时长）。修订保留已完成里程碑的进度。调用前先跟用户确认要改哪些；改完把新路线完整呈现给用户。",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "学习总目标（一句话，修订时按用户最新要求更新）"},
                "total_hours": {"type": "integer", "description": "预估总学习时长（小时）"},
                "revision": {"type": "string", "description": "修订说明（用户的要求 + 本次改动点）"},
                "stages": {
                    "type": "array",
                    "description": "修订后的完整学习阶段列表（3-5 个，按依赖排序；描述不变的里程碑自动保留完成进度）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "阶段名（如 环境搭建 / 核心概念 / 实战项目 / 进阶调优）"},
                            "goal": {"type": "string", "description": "本阶段目标"},
                            "materials": {"type": "string", "description": "推荐资料（可留空由后续 collect 补充）"},
                            "est_hours": {"type": "integer", "description": "本阶段预估小时数"},
                            "milestones": {
                                "type": "array",
                                "description": "可检验的里程碑（描述保持与原来一致的会自动保留完成进度）",
                                "items": {"type": "object",
                                          "properties": {"desc": {"type": "string"}},
                                          "required": ["desc"]},
                            },
                        },
                        "required": ["name", "goal", "est_hours", "milestones"],
                    },
                },
            },
            "required": ["goal", "total_hours", "stages"],
        },
    },
}

REMEMBER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "记录一条跨会话长期记忆（偏好 / 事实约束 / 目标 / 未决事项）。"
            "只在用户表达稳定、以后仍然适用的信息时调用；一次性要求（如「这次讲快点」）不要记录。"
            "用户说「记住…」「以后…」「每次…」时优先调用。"
            "同一信息请复用长期记忆块里已有的 key；同 key 新内容会覆盖旧值，"
            "返回里的 conflict 会给出「旧值→新值」，需要如实告知用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["profile", "open_item"],
                         "description": "profile=偏好/事实/目标；open_item=未决事项"},
                "category": {"type": "string", "enum": ["preference", "fact", "goal"],
                             "description": "kind=profile 必填"},
                "scope": {"type": "string", "enum": ["global", "tech"],
                          "description": "global=所有技术通用；tech=仅当前技术"},
                "tech": {"type": "string",
                         "description": "scope=tech 时必填，必须等于当前技术"},
                "key": {"type": "string", "description": (
                    "profile 必填。偏好用受控词表："
                    + "/".join(memory_domain.PREFERENCE_KEYS)
                    + "；事实用 ASCII slug（如 constraint:java_version）；目标固定 goal")},
                "text": {"type": "string", "description": "自然语言，≤200 字"},
                "op": {"type": "string", "enum": ["open", "close"],
                       "description": "kind=open_item 必填"},
                "item_id": {"type": "string",
                            "description": "close 时必填；open 时由系统生成"},
                "expires_at": {"type": "string",
                               "description": "可选，ISO 时间；open 时默认按 TTL"},
            },
            "required": ["kind"],
        },
    },
}

RECALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_memory",
        "description": (
            "按需查询跨会话长期记忆（结构化过滤：tech/kind/status/evidence/日期/文本子串；"
            "不做语义检索）。只在用户问「以前学过什么 / 是否学过 / 撤销过什么 / "
            "某技术历史 / 上次学了什么 / 我对讲解方式有什么偏好」时调用；不要每轮调用。"
            "结果带 status/evidence/date；不要把历史、归档或未考据当成当前已掌握。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": ("可选；文本子串过滤（不做 BM25/语义排序）。"
                                          "问某个具体知识点是否学过时用最短关键词，"
                                          "如“框架对比”用“对比”")},
                "tech": {"type": "string",
                         "description": ("可选技术名；用户未点名其他技术时，"
                                         "用当前会话技术；都不填才查所有技术")},
                "kind": {"type": "string",
                         "enum": ["capability", "preference", "fact", "goal", "open_item"],
                         "description": ("可选；capability=里程碑/能力点（“学过哪些内容”通常用它），"
                                         "fact=自评/时间预算/相关背景，goal=目标，"
                                         "preference=偏好，open_item=未决/下次讲")},
                "status": {"type": "string",
                           "enum": ["active", "archived", "revoked", "superseded",
                                    "closed", "expired", "historical", "history", "any"],
                           "description": ("可选；不填=active。active=当前有效/我学过；"
                                           "revoked=取消/撤销/放弃；superseded=被新值取代/"
                                           "以前/旧值；closed=已关闭未决项；"
                                           "history=所有历史状态（仅在用户问“所有历史”时用）")},
                "evidence": {"type": "string",
                             "enum": ["verified", "claimed", "degraded", "unknown"],
                             "description": ("可选；verified=系统验收；claimed=用户口头声明；"
                                             "degraded=验收降级；unknown=历史迁移/尚未确认"
                                             "（用户说“没确认/没验证”用 unknown）")},
                "key": {"type": "string",
                        "description": ("可选；精确查某个 key。常见：self_level=自评熟悉度"
                                        "（问“自评几分”），time_budget=每天可投入时间"
                                        "（问“每天多少时间”），related=相关技术/基础背景"
                                        "（问“什么基础/背景”），goal=目标")},
                "date_from": {"type": "string", "description": "可选，YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "可选，YYYY-MM-DD"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                          "description": "返回条数，默认 5"},
            },
            "required": [],
        },
    },
}

COACH_TOOLS_BY_MODE: dict[str, list[dict]] = {
    "survey": [REMEMBER_SCHEMA, RECALL_SCHEMA],
    "planning": [GENERATE_ROADMAP_SCHEMA, CONFIRM_ROADMAP_SCHEMA,
                 GET_ROADMAP_SCHEMA, REMEMBER_SCHEMA, RECALL_SCHEMA],
    # coaching 不再暴露 note/note_commit：学习内容由自动沉淀（后台线程）
    # 确定性落库 + 候选确定性确认，agent 手动 note 会同步阻塞对话（note_pipeline 耗时长）
    "coaching": [COLLECT_SCHEMA, READ_SCHEMA, ASK_SCHEMA,
                 GET_ROADMAP_SCHEMA, UPDATE_ROADMAP_SCHEMA, REVISE_ROADMAP_SCHEMA,
                 REMEMBER_SCHEMA, RECALL_SCHEMA],
}


def coach_tools_for_mode(mode: str) -> list[dict]:
    """当前 mode 的可用工具；记忆或按需访问未启用时不暴露 recall_memory。"""
    tools = list(COACH_TOOLS_BY_MODE.get(mode) or [])
    if config.LTM_ENABLED and config.LTM_RECALL_ENABLED:
        return tools
    return [tool for tool in tools
            if (tool.get("function") or {}).get("name") != "recall_memory"]


# ============================================================
# 工具参数的审计白名单（tool_call 事件用）
# **绝不整包 json.dumps(args)**：revise_roadmap 的 stages 是嵌套大数组、note 的 content
# 上限 2000 字，整包落盘等于把学习内容写进日志。白名单与工具定义放在同一个文件，
# 加新工具时一眼能看到要不要补；漏补的方向是"少记"而不是"泄露"。
# ============================================================

# 短标识字段：原样记
_PLAIN_ARG_FIELDS: dict[str, tuple[str, ...]] = {
    "collect": ("tech", "focus"),
    "read": ("url",),
    "update_roadmap": ("milestone_id", "done"),
    "note": ("tech",),
    "note_commit": ("decision",),
    "remember": ("kind", "category", "scope", "tech", "key", "op",
                 "item_id", "expires_at"),
    "recall_memory": ("tech", "kind", "status", "evidence", "key",
                      "date_from", "date_to", "limit"),
}
# 自由文本字段：只记长度 + 首 N 字（用户原话，全文不进日志）
_PREVIEW_ARG_FIELDS: dict[str, tuple[str, ...]] = {
    "ask": ("question",),
    "generate_roadmap": ("goal",),
    "revise_roadmap": ("goal", "revision"),
    "remember": ("text",),
    "recall_memory": ("query",),
}
# 计数/数值字段：列表只记条数，不记内容（stages 里是整份学习计划）
_SCALAR_ARG_FIELDS: dict[str, tuple[str, ...]] = {
    "generate_roadmap": ("total_hours", "stages"),
    "revise_roadmap": ("total_hours", "stages"),
}
_ARG_PREVIEW_CHARS = 20


def tool_arg_fields(name: str, args: dict) -> dict:
    """把工具入参压成可落盘的元数据（白名单 + 自由文本只留长度与首 N 字）。"""
    out: dict = {}
    for key in _PLAIN_ARG_FIELDS.get(name, ()):
        value = args.get(key)
        if value not in (None, ""):
            out[key] = str(value)[:200]
    for key in _PREVIEW_ARG_FIELDS.get(name, ()):
        text = str(args.get(key) or "")
        if text:
            out[f"{key}_preview"] = text[:_ARG_PREVIEW_CHARS]
            out[f"{key}_chars"] = len(text)
    for key in _SCALAR_ARG_FIELDS.get(name, ()):
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            out[f"{key}_count"] = len(value)
        elif value is not None:
            out[key] = value
    return out


# ============================================================
# 工具实现（出入参短；大内容写文件）
# ============================================================


class CoachCtx:
    """coach 工具执行上下文（graph 层构造）。updates 由工具写入，graph 节点合并进状态。

    progress 为 Web 端流式进度回调（graph 层经 _get_progress 从注册表取，CLI 为 None 静默）。
    """

    def __init__(self, state: dict, *, progress=None, thread_id: str | None = None):
        self.state = state
        self.updates: dict = {}
        self.progress = progress
        self.thread_id = thread_id  # 当前图会话的 thread_id（路线恢复定位用）

    # 属性读取：先查本批工具产生的 updates（同一 assistant 消息的多个 tool_calls 可见彼此
    # 的更新），再回退到 graph 状态——保证同批多次调用（如连勾两个里程碑）不读到旧值。
    @property
    def tech(self) -> str:
        return self.updates.get("tech", self.state.get("tech") or "")

    @property
    def survey_answers(self) -> dict:
        return self.state.get("survey_answers") or {}

    @property
    def learner_profile(self) -> dict:
        return self.state.get("learner_profile") or {}

    @property
    def materials_path(self) -> str | None:
        return self.updates.get("materials_path", self.state.get("materials_path"))

    @property
    def roadmap(self):
        return self.updates.get("roadmap", self.state.get("roadmap"))


def _generate_roadmap(args: dict, ctx: CoachCtx) -> dict:
    goal = str(args.get("goal") or "").strip()
    total_hours = args.get("total_hours")
    stages = args.get("stages") or []
    if not goal or not isinstance(total_hours, int) or total_hours <= 0 or not stages:
        return {"status": "error", "error": "缺少 goal / total_hours / stages",
                "hint": "请按 generate_roadmap 的 schema 补全后重试"}
    norm, errors = roadmap_domain.normalize_stages(stages)
    if not norm:
        return {"status": "error", "errors": errors or ["没有合法阶段"],
                "hint": "请修正阶段结构（每阶段至少 1 个里程碑）后重新调用 generate_roadmap"}
    roadmap = roadmap_domain.build_roadmap(ctx.tech, goal, total_hours, norm)
    # 记录生成路线的会话线程：CLI「按技术名继续上次陪练」靠它定位恢复
    if ctx.thread_id:
        roadmap["session_thread_id"] = ctx.thread_id
    jp = learner.save_roadmap(roadmap)
    ctx.updates["roadmap"] = roadmap
    ctx.updates["roadmap_path"] = str(jp)
    # 画像落盘（profile.json 按 tech 归档；不含诊断题原文）
    if ctx.learner_profile:
        entry = {k: v for k, v in ctx.learner_profile.items() if k != "diagnostics"}
        entry["roadmap_path"] = str(jp)
        try:
            learner.save_tech_profile(ctx.tech, entry)
        except Exception:  # noqa: BLE001, S110 —— 画像落盘失败不阻断路线生成
            pass
    # 问卷声明（目标 / 自评 / 相关技术 / 时间预算 / 诊断结果）同步进长期记忆
    _sync_planning_memory(ctx, roadmap)
    return {
        "status": "ok",
        "roadmap_path": str(jp),
        "stages": [{"id": s["id"], "name": s["name"], "est_hours": s["est_hours"]} for s in roadmap["stages"]],
        "current_stage": roadmap["current_stage"],
        "total_hours": roadmap["total_hours"],
        "note": "路线已保存。请把路线完整呈现给用户确认；用户确认后调用 confirm_roadmap。",
    }


def _confirm_roadmap(args: dict, ctx: CoachCtx) -> dict:
    if not ctx.roadmap:
        return {"status": "error", "error": "还没有路线，请先调用 generate_roadmap"}
    ctx.updates["mode"] = "coaching"
    return {"status": "ok", "current_stage": ctx.roadmap.get("current_stage"),
            "note": "路线已确认，进入执行阶段（coaching）。"}


def _get_roadmap(args: dict, ctx: CoachCtx) -> dict:
    r = ctx.roadmap
    if not r:
        return {"status": "ok", "roadmap": None, "note": "还没有生成路线"}
    return {
        "status": "ok", "tech": r.get("tech"), "goal": r.get("goal"),
        "total_hours": r.get("total_hours"), "current_stage": r.get("current_stage"),
        "stages": [{"id": s.get("id"), "name": s.get("name"), "goal": s.get("goal"),
                    "est_hours": s.get("est_hours"),
                    "milestones": [{"id": m.get("id"), "desc": m.get("desc"), "done": m.get("done")}
                                   for m in s.get("milestones") or []]}
                   for s in r.get("stages") or []],
    }


_CATEGORY_LABEL = {"preference": "偏好", "fact": "事实", "goal": "目标"}


def _record_memory_event(events: list[dict], *, where: str) -> dict | None:
    """写长期记忆事件；失败只留审计，不把工具调用打断。"""
    if not config.LTM_ENABLED:
        return None
    try:
        return memory_store.record_events(events)
    except Exception as exc:  # noqa: BLE001 —— 记忆写入失败不应阻断对话/工具
        audit("ltm_error", where=where,
              error=f"{type(exc).__name__}: {exc}"[:200])
        return None


def _remember_tech(args: dict, ctx: CoachCtx) -> tuple[str, str | None]:
    """取 open_item / tech 作用域要写入的技术名，并校验必须是当前技术。"""
    current = str(ctx.tech or "").strip()
    tech = str(args.get("tech") or "").strip()
    if not tech:
        return "", "需要 tech（当前技术名）"
    if not current:
        return tech, "当前会话没有技术名，无法记录技术作用域记忆"
    if memory_domain.tech_key(tech) != memory_domain.tech_key(current):
        return tech, f"tech 必须是当前技术「{current}」（收到「{tech}」）"
    return current, None  # 统一用当前会话的技术名展示，避免大小写/别名漂移


def _find_profile_entry(view: dict, category: str, scope: str, tech: str,
                        key: str) -> dict | None:
    target = memory_domain.profile_id(category, scope, tech, key)
    if category == "goal":
        goals = view.get("goals") or {}
        if scope == "global":
            rows = goals.get("global") or []
        else:
            rows = (goals.get("by_tech") or {}).get(
                memory_domain.tech_key(tech)) or []
        return next((row for row in rows if row.get("id") == target), None)
    bucket = (view.get("preferences") if category == "preference"
              else view.get("facts")) or []
    return next((row for row in bucket if row.get("id") == target), None)


def _remember_profile(args: dict, ctx: CoachCtx) -> dict:
    category = str(args.get("category") or "").strip()
    scope = str(args.get("scope") or "").strip()
    key = str(args.get("key") or "").strip()
    text = memory_domain.clean_text(args.get("text"))
    tech = ""
    if scope == "tech":
        tech, err = _remember_tech(args, ctx)
        if err:
            return {"status": "error", "error": err}
    if category == "goal":
        key = "goal"  # 每个 (scope, tech) 只有一个 active 目标
    err = memory_domain.validate_profile_item(category, scope, tech, key, text)
    if err:
        return {"status": "error", "error": err,
                "valid": {"category": list(memory_domain.PROFILE_CATEGORIES),
                          "scope": list(memory_domain.SCOPES),
                          "preference_keys": list(memory_domain.PREFERENCE_KEYS)}}
    try:
        before = memory_store.ensure_view()
    except Exception as exc:  # noqa: BLE001
        audit("ltm_error", where="remember_profile_read",
              error=f"{type(exc).__name__}: {exc}"[:200])
        return {"status": "error", "error": "长期记忆读取失败，本次未记录"}
    old = _find_profile_entry(before, category, scope, tech, key)
    event = memory_domain.make_event(
        "profile_recorded", session=ctx.thread_id or "", source="user",
        category=category, scope=scope, tech=tech, key=key, text=text)
    if _record_memory_event([event], where="remember_profile") is None:
        return {"status": "error", "error": "长期记忆写入失败，本次未记录"}

    action, conflict = "created", None
    if old:
        if memory_domain.texts_equal(old.get("text"), text):
            action = "confirmed"
        else:
            action = "updated"
            conflict = (f"{_CATEGORY_LABEL.get(category, category)}「{key}」已更新："
                        f"『{old.get('text')}』→『{text}』")
            audit("ltm_conflict", category=category, scope=scope,
                  tech=tech, key=key)
    return {"status": "ok", "action": action,
            "id": memory_domain.profile_id(category, scope, tech, key),
            "category": category, "scope": scope, "tech": tech,
            "key": key, "text": text, "conflict": conflict}


def _remember_open_item(args: dict, ctx: CoachCtx) -> dict:
    op = str(args.get("op") or "").strip()
    if op not in ("open", "close"):
        return {"status": "error",
                "error": "kind=open_item 时 op 必须是 open / close"}
    tech, err = _remember_tech(args, ctx)
    if err:
        return {"status": "error", "error": err}
    if op == "close":
        item_id = str(args.get("item_id") or "").strip()
        if not item_id:
            return {"status": "error",
                    "error": "close 需要 item_id（从注入块或上次 open 的返回值中取）"}
        if not item_id.startswith(f"open:{memory_domain.tech_key(tech)}:"):
            return {"status": "error", "error": "item_id 不属于当前技术"}
        event = memory_domain.make_event(
            "open_item_recorded", session=ctx.thread_id or "", source="user",
            op="close", tech=tech, item_id=item_id)
        if _record_memory_event([event], where="remember_close") is None:
            return {"status": "error", "error": "长期记忆写入失败，本次未记录"}
        return {"status": "ok", "action": "closed", "item_id": item_id}

    text = memory_domain.clean_text(args.get("text"))
    if not text:
        return {"status": "error", "error": "open 需要 text"}
    if len(text) > memory_domain.MAX_TEXT_CHARS:
        return {"status": "error",
                "error": f"text 不能超过 {memory_domain.MAX_TEXT_CHARS} 字"}
    expires = str(args.get("expires_at") or "").strip()
    if expires:
        dt = memory_domain.parse_time(expires)
        if dt is None:
            return {"status": "error", "error": "expires_at 必须是 ISO 时间"}
        expires = memory_domain.format_time(dt)
    elif config.LTM_OPEN_ITEM_TTL_DAYS > 0:
        expires = memory_domain.format_time(
            memory_domain.local_now()
            + timedelta(days=config.LTM_OPEN_ITEM_TTL_DAYS))
    item_id = memory_domain.open_item_id(tech, text)
    event = memory_domain.make_event(
        "open_item_recorded", session=ctx.thread_id or "", source="user",
        op="open", tech=tech, item_id=item_id, text=text,
        expires_at=expires or None)
    if _record_memory_event([event], where="remember_open") is None:
        return {"status": "error", "error": "长期记忆写入失败，本次未记录"}
    return {"status": "ok", "action": "opened", "item_id": item_id,
            "text": text, "expires_at": expires or None}


def _remember(args: dict, ctx: CoachCtx) -> dict:
    """长期记忆写入工具：只接受显式、稳定的信息，代码校验后落事件。"""
    if not config.LTM_ENABLED:
        return {"status": "error",
                "error": "长期记忆已关闭（LTM_ENABLED=false），无法记录"}
    kind = str(args.get("kind") or "").strip()
    if kind == "profile":
        return _remember_profile(args, ctx)
    if kind == "open_item":
        return _remember_open_item(args, ctx)
    return {"status": "error", "error": "kind 必须是 profile / open_item"}


def _recall_memory(args: dict, ctx: CoachCtx) -> dict:
    """按需记忆访问：结构化/时间线；不做 BM25/语义排序。"""
    if not config.LTM_ENABLED:
        return {"status": "error",
                "error": "长期记忆已关闭（LTM_ENABLED=false）"}
    if not config.LTM_RECALL_ENABLED:
        return {"status": "error",
                "error": "按需记忆访问未启用（LTM_RECALL_ENABLED=false）"}
    from ..adapters import memory_search as search_store
    from ..domain import memory_search as search_domain

    started = time.monotonic()
    query = str(args.get("query") or "").strip()
    tech = str(args.get("tech") or "").strip() or None
    kind = str(args.get("kind") or "").strip() or None
    status = str(args.get("status") or "active").strip() or "active"
    evidence = str(args.get("evidence") or "").strip() or None
    key = str(args.get("key") or "").strip() or None
    date_from = str(args.get("date_from") or "").strip() or None
    date_to = str(args.get("date_to") or "").strip() or None
    try:
        limit = int(args.get("limit") or config.LTM_RECALL_TOP_K)
    except (TypeError, ValueError):
        limit = config.LTM_RECALL_TOP_K
    limit = max(1, min(limit, 10))
    try:
        records = search_store.load_records()
        hits = search_domain.filter_records(
            records, query=query or None, tech=tech,
            kinds=[kind] if kind else None,
            statuses=[status] if status else None,
            evidence=[evidence] if evidence else None,
            keys=[key] if key else None,
            date_from=date_from, date_to=date_to, limit=limit)
        text, used = search_domain.render_records(
            hits, max_chars=config.LTM_RECALL_MAX_CHARS)
        mode = "substring" if query else "structured"
    except Exception as exc:  # noqa: BLE001 —— 按需查询失败不能拖垮对话
        audit("ltm_error", where="recall",
              error=f"{type(exc).__name__}: {exc}"[:200])
        return {"status": "error", "error": "记忆查询失败，本次未返回结果"}
    ids = [str(item.get("id") or "") for item in used]
    audit("ltm_recall", mode=mode, tech=tech, kind=kind, status=status,
          evidence=evidence, key=key, date_from=date_from, date_to=date_to,
          limit=limit, result_count=len(used), result_ids=ids[:20],
          result_statuses=sorted({str(item.get("status") or "")
                                  for item in used if item.get("status")}),
          query_chars=len(query), has_query=bool(query),
          elapsed_ms=round((time.monotonic() - started) * 1000, 1))
    return {"status": "ok", "mode": mode,
            "count": len(used), "text": text}


def _sync_planning_memory(ctx: CoachCtx, roadmap: dict) -> None:
    """问卷完成/路线生成时，把画像声明同步进长期记忆（source=user）。

    重复调用 generate_roadmap（修订）会重复写同样的事件：同 key 同文本只会刷新
    确认时间；诊断结果按“最近一次覆盖”折叠，不会累加成多倍题量。
    """
    if not config.LTM_ENABLED:
        return
    tech = str(ctx.tech or roadmap.get("tech") or "").strip()
    profile = ctx.learner_profile or {}
    if not tech or not profile:
        return
    events: list[dict] = []

    def add_profile(category: str, key: str, value) -> None:
        text = str(value).strip() if value is not None else ""
        if not text:
            return
        events.append(memory_domain.make_event(
            "profile_recorded", session=ctx.thread_id or "", source="user",
            category=category, scope="tech", tech=tech, key=key, text=text))

    add_profile("goal", "goal", profile.get("goal"))
    if profile.get("self_level") is not None:
        add_profile("fact", "self_level", profile.get("self_level"))
    add_profile("fact", "related", profile.get("related"))
    if profile.get("time_budget") is not None:
        add_profile("fact", "time_budget", profile.get("time_budget"))

    graded = [d for d in (profile.get("diagnostics") or [])
              if isinstance(d, dict) and d.get("grade")]
    if graded:
        right = sum(1 for d in graded if d.get("grade") == survey.GRADE_RIGHT)
        events.append(memory_domain.make_event(
            "diagnostic_recorded", session=ctx.thread_id or "", source="system",
            tech=tech, asked=len(graded), right=right))
    if events:
        _record_memory_event(events, where="generate_roadmap")


def _record_capability_event(roadmap: dict, milestone_id: str, done: bool,
                             evidence: str | None, ctx: CoachCtx) -> None:
    """里程碑勾选/取消后写能力事件（证据等级按验收闸门结果）。"""
    if not config.LTM_ENABLED:
        return
    tech = str(roadmap.get("tech") or ctx.tech or "").strip()
    point = _milestone_desc(roadmap, milestone_id)
    if not tech or not point:
        return
    event = memory_domain.make_event(
        "capability_recorded", session=ctx.thread_id or "", source="system",
        tech=tech, point=point, milestone_id=milestone_id,
        op="add" if done else "revoke",
        evidence=evidence if done else None)
    _record_memory_event([event], where="update_roadmap")


def _collect(args: dict, ctx: CoachCtx) -> dict:
    tech = str(args.get("tech") or "").strip() or ctx.tech
    if not tech:
        return {"status": "error", "error": "collect 需要 tech"}
    blocked = _heavy_blocked(ctx, "collect")
    if blocked:
        return blocked
    focus = str(args.get("focus") or "").strip() or None
    try:
        result = collect_pipeline(tech, focus, progress=ctx.progress)
    except CollectStageError as e:
        _record_heavy_failure(ctx, "collect", e.stage)
        return {"status": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001 —— 回喂模型修正
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    ctx.updates["coach_doc"] = {"path": result["materials_path"], "type": "collect"}
    out = {"status": "ok", "materials_path": result["materials_path"],
           "url_count": len(result["urls"]),
           "report_excerpt": (result["report"] or "")[:800],
           "note": "资料报告已保存，可据此推进学习。"}
    if not result.get("resource_ok", True):
        # 这次没拿到资料（抓取全失败 / 搜索无结果）：报告里已如实标注，明确要求模型别当资料齐全
        out["resource_ok"] = False
        out["note"] = ("本次没有取到文档正文（报告里已如实标注资料不完整）。"
                       "请向用户说明情况并建议稍后重试，不要据此推进学习内容。")
    return out


def _read(args: dict, ctx: CoachCtx) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"status": "error", "error": "read 需要 url"}
    blocked = _heavy_blocked(ctx, "read")
    if blocked:
        return blocked
    try:
        result = read_pipeline(url, progress=ctx.progress)
    except ReadStageError as e:
        _record_heavy_failure(ctx, "read", e.stage)
        return {"status": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    if result.get("error"):
        return {"status": "error", "error": result["error"]}
    ctx.updates["coach_doc"] = {"path": result["report_path"], "type": "read"}
    return {"status": "ok", "report_path": result["report_path"], "title": result.get("title"),
            "report_excerpt": (result["report"] or "")[:800]}


def _ask(args: dict, ctx: CoachCtx) -> dict:
    question = str(args.get("question") or "").strip()
    if not question:
        return {"status": "error", "error": "ask 需要 question"}
    try:
        result = qa_pipeline(question, tech=ctx.tech or None, progress=ctx.progress)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "answer": result["answer"],
            "sources": [{"path": s.get("path"), "topic": s.get("topic"),
                         "similarity": s.get("similarity")} for s in result["sources"]],
            "no_hit": result["no_hit"]}


def _note(args: dict, ctx: CoachCtx) -> dict:
    tech = str(args.get("tech") or "").strip() or ctx.tech
    content = str(args.get("content") or "").strip()
    if not tech or not content:
        return {"status": "error", "error": "note 需要 tech 和 content（≤2000 字）"}
    try:
        result = note_pipeline(tech, content, materials_path=ctx.materials_path, progress=ctx.progress)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    if result.get("empty_reason"):
        return {"status": "empty", "reason": result["empty_reason"]}
    if result["merge_candidates"]:
        # 相似候选暂存状态，等用户决定后再 note_commit（复用现有 format/parse 纯函数）
        ctx.updates["coach_note_pending"] = {**result, "_tech": tech}
        return {"status": "needs_decision",
                "message": format_merge_candidates(result["merge_candidates"]),
                "note": "把上面的候选呈现给用户，让用户回复 all（全合并）/ 编号逐条（如 1,3）/ skip（跳过）；用户决定后调用 note_commit，decision 传用户原话。"}
    persisted = persist_points(tech, result["new_points"], [], set())
    out = {"status": "ok", "new_count": persisted["new_count"], "merged_count": 0,
           "results": [{"topic": r["topic"], "path": r["path"], "action": r["action"]}
                       for r in persisted["results"]]}
    warning = _index_warning(persisted)
    if warning:
        out["warning"] = warning
    return out


def _index_warning(persisted: dict) -> str:
    """索引失败提示：沉淀结果里有 index_ok=False 时生成给 agent 转述的警告。

    索引失败不阻断沉淀（笔记已在磁盘），但必须可见——静默失败会让笔记
    「保存成功但检索不到」。
    """
    failed = [r["topic"] for r in persisted.get("results", []) if r.get("index_ok") is False]
    if not failed:
        return ""
    return f"⚠️ RAG 索引更新失败：{'、'.join(failed)}（笔记已保存，稍后运行 index 会自动补齐）"


def _note_commit(args: dict, ctx: CoachCtx) -> dict:
    pending = ctx.state.get("coach_note_pending")
    if not pending:
        return {"status": "error", "error": "没有待确认的笔记，请先调用 note"}
    decision = str(args.get("decision") or "").strip()
    tech = pending.get("_tech") or ctx.tech
    indices = parse_merge_decision(decision, len(pending["merge_candidates"]))
    persisted = persist_points(tech, pending["new_points"], pending["merge_candidates"], indices)
    ctx.updates["coach_note_pending"] = None  # 提交后清掉，避免重复提交
    # 合并时发现的矛盾以新内容为准修正，报告透出给 agent 转述用户
    conflict_reports = persisted.get("conflict_reports") or []
    out = {"status": "ok", "new_count": persisted["new_count"], "merged_count": persisted["merged_count"],
           "conflict_reports": conflict_reports,
           "results": [{"topic": r["topic"], "path": r["path"], "action": r["action"]}
                       for r in persisted["results"]]}
    warning = _index_warning(persisted)
    if warning:
        out["warning"] = warning
    return out


# ============================================================
# 确定性写触发（学习内容自动沉淀）
# 纯函数：对话缓冲 → note_pipeline → 返回决策，落库/暂存由 graph 节点编排。
# ============================================================


def _sweep_buffer_text(buffer: list[dict]) -> str:
    """把沉淀缓冲（[{role, content}]）拼成 note 管道的输入文本（只取 user/assistant 纯文本）。"""
    parts = []
    for m in buffer:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            parts.append(f"{m['role']}：{m['content']}")
    return "\n".join(parts)


def _buffer_query_segments(buffer: list[dict]) -> list[str]:
    """把沉淀缓冲按**助手消息**分组，每组是要拿去检索的一段文本。

    为什么以助手消息为界：要被提取成笔记的是**助手讲出来的知识点**，用户的输入多半是贴回来
    的运行结果 / 报错 / 配置。按"助手 + 用户回复"合成一组会让粘贴把查询预算吃掉——实测
    有这么一批：8 次查询里 6 次花在一份 `--dump-config` 输出上，而 2672 字的讲解只占 1 次。

    用户消息只在**短**的时候并入当前组（≤ NOTE_RECALL_USER_MSG_CHARS）：短回是主题信号
    （"Context 和 useMemo 有什么区别？"），长的是素材，整条不进查询。

    为什么要分组（而不是整批查一次）：一批缓冲常跨多个主题，整批文本会被平均成一个向量，
    后出现的主题在查询里没有表示，对应笔记召不回来——差量提取于是看不见它们，把已覆盖的
    内容当新知识重提。

    从**结构化缓冲**取，不从拼好的字符串里正则切——缓冲本来就是 [{role, content}]。
    组内文本过长时再由管道按 1500 字切块（见 note._recall_queries）；整组短于
    NOTE_RECALL_MIN_SEGMENT_CHARS 的是寒暄，单独查一次只会带回噪声，丢掉。
    """
    segs: list[str] = []
    cur: list[str] = []
    has_assistant = False
    for m in buffer:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if role == "assistant":
            if has_assistant:  # 这一组里已经有一条助手讲解 → 收束，另起一组
                segs.append("\n".join(cur))
                cur, has_assistant = [], False
            cur.append(f"assistant：{content}")
            has_assistant = True
        elif len(content) <= config.NOTE_RECALL_USER_MSG_CHARS:
            cur.append(f"user：{content}")
        # 超长的用户消息（粘贴）整条不进查询：它是要讲解的素材，不是会被提取的知识
    if cur:
        segs.append("\n".join(cur))
    return [s for s in segs if len(s) >= config.NOTE_RECALL_MIN_SEGMENT_CHARS]


def run_memory_sweep(tech: str, buffer: list[dict], progress=None) -> dict:
    """确定性写触发：把自上次沉淀以来的对话文本喂给 note 管道沉淀。

    Args:
        tech: 技术名
        buffer: 消息对列表 [{role, content}]（user/assistant 纯文本）
        progress: 可选进度回调

    Returns:
        {
          "action": "skip" | "persisted" | "pending",
          "count": int,            # persisted: 新建知识点数
          "pending": dict | None,  # pending: coach_note_pending 负载（含 _tech/_auto）
          "message": str | None,   # 追加给 agent 的 system 提示（可选）
        }
    """
    if not tech or not buffer:
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    text = _sweep_buffer_text(buffer)
    if not text.strip():
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    if progress:
        progress("🗂️ 正在沉淀学习内容...")
    # 分段只作召回查询用；喂给差量提取的正文仍是整批 text（提取要看全部内容，召回只看"像不像"）
    result = note_pipeline(tech, text, progress=progress,
                           query_segments=_buffer_query_segments(buffer))
    if result.get("empty_reason"):
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    # LLM 输出条目全无效（topic/正文为空被过滤）→ 两个桶都空，等价于无新内容，按 skip 处理
    if not result.get("new_points") and not result.get("merge_candidates"):
        return {"action": "skip", "count": 0, "pending": None, "message": None}
    if result["merge_candidates"]:
        # 有相似候选：新点与候选一起暂存，等用户决定后经 note_commit 一次落库（与 note 工具流一致）
        pending = {**result, "_tech": tech, "_auto": True}
        msg = (f"（内部）检测到 {len(result['new_points'])} 个新知识点、"
               f"{len(result['merge_candidates'])} 个与旧笔记相似的候选，"
               "请在回复中呈现候选并请用户决定（all / 编号逗号分隔 / skip），"
               "用户决定后调用 note_commit，decision 传用户原话。")
        return {"action": "pending", "count": len(result["new_points"]),
                "pending": pending, "message": msg}
    persisted = persist_points(tech, result["new_points"], [], set())
    msg = f"（内部）已自动沉淀 {persisted['new_count']} 个新知识点。"
    warning = _index_warning(persisted)
    if warning:
        msg += f" {warning}（请在回复中提醒用户）"
    return {"action": "persisted", "count": persisted["new_count"],
            "pending": None, "message": msg}


# ============================================================
# 确定性读路由（提问先查库）
# 纯函数：元问题廉价闸门 + 知识库检索 + 相似度阈值过滤 → 命中片段列表，注入 coaching 提示词。
# ============================================================

# 廉价闸门关键词：明显过程 / 元问题（继续、现在到哪、路线对吗…）确定性跳过，不查库。
# 判定沿用 exit_intent 的「残留检查」思路：短文本整体由元关键词 + 语气词组成才算元问题，
# 避免「这个写法对不对」这类含学习内容的提问被误判跳过。
_META_RE = re.compile(
    r"(继续|接着|下一步|然后呢|接下来|之后呢|还有吗|现在到哪|到哪了|现在在哪|"
    r"什么进度|进度|当前阶段|在哪一步|第几阶段|学了多少|完成了吗|"
    r"这个路线|路线对吗|路线正确吗|路线合理吗|对不对|对吗|对不|"
    r"明白了|懂了|知道了|好的|可以|行|嗯|开始吧|就这样|先这样|"
    r"继续说|讲下去|再说一遍|什么意思|听不懂|举个例子)",
    re.IGNORECASE,
)
_META_MAX_LEN = 12  # 元问题判定只对短文本生效（长回答含学习内容，交给检索判断）
_META_PARTICLES = "吧了呀呢嘛啊哦哈嗯~～。，,.！!？? "


def _is_meta_question(text: str) -> bool:
    """确定性判定：整句是否仅为过程 / 元问题（零成本廉价闸门，命中即不查库）。"""
    t = (text or "").strip()
    if not t or len(t) > _META_MAX_LEN:
        return False
    prev = None
    while prev != t:  # 迭代剥离所有元关键词（「好的，继续」等复合短语一次清干净）
        prev = t
        t = _META_RE.sub("", t).strip()
    return not t.strip(_META_PARTICLES)


def _hit_relevance(h: dict) -> float | None:
    """命中的「可标定相关度」：可用于绝对阈值比较的分数。

    hybrid 的 ``similarity`` 是归一化相对分（top 恒 1.0，见 rrf_fuse），
    与 ROUTE_KB_INJECT_SIM 这类绝对阈值**不可比**；``dense_similarity`` 才是融合时
    保留的原始余弦。优先级：
    - hybrid 且进过 dense 榜单：dense_similarity（真实余弦）
    - hybrid 的 BM25 独有命中：无标定分 → None（宁可不注入，不拿归一化分凑数）
    - 纯 dense 路径（QA_USE_HYBRID 关闭）：similarity 即余弦
    """
    if h.get("dense_similarity") is not None:
        return h["dense_similarity"]
    if "bm25_score" in h:
        return None
    return h.get("similarity")


def run_kb_retrieve(tech: str, question: str) -> list[dict]:
    """确定性读路由：提问先查库，命中相关片段列表 [{path, snippet}]（无命中返回空）。

    两级闸门：
    - 廉价闸门（零成本）：明显过程/元问题（继续、现在到哪、路线对吗…）直接跳过，不查库；
    - 质量闸门（可标定相似度）：复用 qa 混合检索（限定 tech，召回 config.QA_RECALL_CHUNKS
      条分块），按 _hit_relevance（hybrid 用保留的 dense 原始余弦、BM25 独有命中不过闸）
      ≥ ROUTE_KB_INJECT_SIM 过滤，片段截断到 QA_SNIPPET_CHARS。混合检索的归一化
      similarity 不能当绝对门槛。
    N 的单位是**笔记篇数**（ROUTE_KB_SNIPPETS）：一篇笔记常切成多块，按块取 N 条会让
    同一篇的多个分块重复占名额（实测 3 条注入只覆盖 1~3 篇），故每篇只取最靠前的一块。
    检索异常（RAG 未索引 / Chroma 异常）优雅降级为空——模型用自己的知识正常回答。
    """
    question = (question or "").strip()
    started = time.monotonic()

    def _record(reason: str, hits_count: int = 0, top_sim: float | None = None,
                injected: int = 0) -> None:
        audit("kb_inject", tech=tech or "", question_chars=len(question),
              hits=hits_count, top_sim=top_sim, injected_count=injected,
              reason=reason, elapsed_s=round(time.monotonic() - started, 1))

    if not tech or not question:
        _record("no_tech_or_question")
        return []
    if _is_meta_question(question):
        _record("meta_question")
        return []
    try:
        hits = _search_notes(question, config.QA_RECALL_CHUNKS, tech)
    except Exception:  # noqa: BLE001 —— RAG 未索引 / Chroma 异常时降级为空
        _record("retrieve_error")
        return []
    top_sim = max(
        (rel for rel in (_hit_relevance(h) for h in hits) if rel is not None),
        default=None,
    )
    out: list[dict] = []
    seen: set[str] = set()
    for h in hits:
        rel = _hit_relevance(h)
        if rel is None or rel < config.ROUTE_KB_INJECT_SIM:
            continue
        note = note_key(h)
        if not note or note in seen:  # 同篇的后续分块不再占名额（hits 按相似度降序，首条即最佳）
            continue
        snippet = (h.get("document") or "").strip()
        if not snippet:
            continue
        seen.add(note)
        out.append({"path": note, "snippet": snippet[:config.QA_SNIPPET_CHARS]})
        if len(out) >= config.ROUTE_KB_SNIPPETS:
            break
    reason = "no_hits" if not hits else ("hit" if out else "below_threshold")
    _record(reason, len(hits), top_sim, len(out))
    return out


def _recent_user_text(msgs) -> str:
    """取 coach_messages 中最近一条 user 消息文本（推进授权判定用）。"""
    for m in reversed(msgs or []):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


# ============================================================
# 里程碑验收闸门（独立 LLM 验收员 + 确定性护栏：批量/节流/豁免）
# 提示词自觉被证伪（Agent 未完成待办即宣布完成），勾选必须经代码强制验收。
# 验收员只看「里程碑标准 + 对话记录」：模型无注入通道，想通过只能真把内容讲进对话。
# ============================================================

VERIFY_MILESTONE_PROMPT = """你是学习路线的里程碑验收员。根据对话记录判断：该里程碑是否已被实质完成。

里程碑（验收标准）：
{milestone}

判定规则：
- 对话中必须实际出现完成该里程碑所需的产出（讲解、回答、结论、代码、操作结果等实质内容）；
- 认知型里程碑（「能说清 / 理解 / 解释 XX」这类以理解或表述为完成形态的待办）：
  助手已在对话里给出覆盖该知识点实质内容的完整讲解，即视为所需产出已出现，不要求用户复述、
  回答或自证掌握；只提名词、只列提纲、只说「已讲完」不算完整讲解；
- 「宣布完成」要看是谁在宣布：助手自己宣布完成、鼓励、日程性总结不算；布置了任务但没人完成不算；
  任务被转交但对方没回应不算；
- 例外：用户本人表示该项待办自己已完成 / 已掌握 / 不必细说，算有效证据，判通过。按语义判，
  不按措辞判：只要用户是在告诉你他自己的掌握状态（不是提问，也不是要你讲解），就判通过，
  用的哪几个字不影响结论；
- 但用户只表达了「往下走」而没说已掌握（意图是推进流程，不是在声明掌握），不算完成证据；
- 未命中上述用户宣告或认知型讲解判定时，有任何一部分未覆盖就判不通过。

如果本次勾选的里程碑与最近对话内容不匹配，请从下面的里程碑清单里找出最近内容实际对应的那一条，
在 suggested_id 填它的 id（如 s2-m2）；没有明显匹配、或本次勾选就是匹配的，填 null。
suggested_id 只是给主模型的提示，不要因为它改变 verified 的判定。

所有里程碑（id: 描述）：
{milestones}

只输出一个 JSON 对象，不要 ```json 围栏、不要解释：
{{"verified": true 或 false, "missing": ["未覆盖的部分，每条一句话"], "reason": "一句话结论", "suggested_id": "s2-m2" 或 null}}

对话记录（截取最近部分）：
{transcript}"""


def _transcript_text(conversation, char_limit: int) -> str:
    """取 conversation（无界完整对话记录）尾部拼成送审文本（角色：内容），总长截到 char_limit。"""
    parts = [f"{m.get('role', '')}：{m.get('content', '')}"
             for m in (conversation or [])
             if m.get("role") in ("user", "assistant") and m.get("content")]
    text = "\n".join(parts)
    return text[-char_limit:] if char_limit and len(text) > char_limit else text


def verify_milestone(milestone_desc: str, transcript: str, milestones: str = "") -> dict:
    """LLM 验收：对话记录是否足以证明里程碑完成（独立调用，只看事实不看模型自述）。

    Args:
        milestone_desc: 本次勾选的里程碑描述（验收标准）
        transcript: 最近对话记录
        milestones: 全部里程碑的「id: 描述」清单，供验收员在内容与勾选项不匹配时
            给出 suggested_id（可选；缺省时不提供提示）

    Returns:
        {"verified": bool|None, "missing": [str], "reason": str, "suggested_id": str|None}；
        verified=None 表示验收器自身异常/解析失败 → 调用方降级放行（不卡死流程）。
    """
    payload = VERIFY_MILESTONE_PROMPT.format(
        milestone=milestone_desc, milestones=milestones or "（未提供）", transcript=transcript)
    try:
        raw = generate_text(payload, "请按要求只输出 JSON。", call_site="verify.milestone")
    except Exception:  # noqa: BLE001 —— 验收器故障降级放行
        return {"verified": None, "missing": [], "reason": "验收器异常，本次跳过验收",
                "suggested_id": None}
    obj = parse_json_object(raw)
    if not obj or not isinstance(obj.get("verified"), bool):
        return {"verified": None, "missing": [], "reason": "验收输出无法解析，本次跳过验收",
                "suggested_id": None}
    missing = [str(m)[:100] for m in (obj.get("missing") or []) if str(m).strip()]
    suggested = obj.get("suggested_id")
    suggested = str(suggested).strip() if suggested else None
    return {"verified": obj["verified"], "missing": missing,
            "reason": str(obj.get("reason") or "")[:200], "suggested_id": suggested}


def _update_roadmap(args: dict, ctx: CoachCtx) -> dict:
    r = ctx.roadmap
    if not r:
        return {"status": "error", "error": "还没有路线，请先走问卷 + 路线规划"}
    milestone_id = str(args.get("milestone_id") or "").strip()
    done = bool(args.get("done", True))
    if not milestone_id:
        return {"status": "error", "error": "update_roadmap 需要 milestone_id（如 s1-m1）"}
    try:
        updated = roadmap_domain.complete_milestone(r, milestone_id, done)
    except KeyError:
        ids = [m["id"] for s in (r.get("stages") or [])
               for m in (s.get("milestones") or [])]
        return {"status": "error", "error": f"未知里程碑 {milestone_id}", "available": ids}

    # 证据等级：默认 claimed（用户声明完成 / 验收被关闭）；验收通过 → verified；
    # 验收器异常/解析失败降级放行 → degraded；取消勾选的事件不带 evidence。
    evidence: str | None = "claimed"
    if done:
        # 闸 1（确定性）：跨阶段护栏——用户回合内只允许勾选同一阶段，同阶段不限次数。
        # 原「一轮只能勾一个」会卡住「Agent 已连续讲完多个小里程碑、事后一次性补勾」的
        # 真实场景（日志：同轮补勾 s1-m3/m4 被拒）；内容风险由闸 4 验收覆盖。
        # 阶段由本回合第一次 done=True 勾选钉住（失败也钉住，避免失败后跳到别的阶段）；
        # 用户回复后由 coach_human 清零。
        stage_pos = roadmap_domain.find_milestone(r, milestone_id)
        stage_id = r["stages"][stage_pos[0]]["id"] if stage_pos else None
        turn_stage = ctx.updates.get("coach_milestone_turn_stage",
                                     ctx.state.get("coach_milestone_turn_stage"))
        if turn_stage is None:
            ctx.updates["coach_milestone_turn_stage"] = stage_id
        elif stage_id != turn_stage:
            return _gate_event("milestone_cross_stage", {
                "status": "rejected",
                "error": f"本回合已在阶段「{turn_stage}」勾选，不能跨阶段勾选「{milestone_id}」。"
                         f"请先完成本阶段，用户确认后再勾选下一阶段。",
            }, milestone=milestone_id, turn_stage=turn_stage, stage=stage_id)
        # 闸 2（确定性）豁免：用户明确声明完成（对话外完成的动作）→ 跳过验收。
        # 额度每回合只有一次：同轮可连勾多个里程碑，但用户那句完成声明只可能指其中一个，
        # 不能让同一句话豁免整个阶段（否则「这个我知道」会绕过全部验收）。
        latest_user = _recent_user_text(ctx.state.get("coach_messages"))
        exempt_used = bool(ctx.updates.get("coach_milestone_exempt_used",
                                           ctx.state.get("coach_milestone_exempt_used")))
        exempt = (config.ROUTE_MILESTONE_VERIFY
                  and not exempt_used
                  and exit_intent.is_completion_claim(latest_user))
        verify_enabled = config.ROUTE_MILESTONE_VERIFY and not exempt
        if exempt:
            ctx.updates["coach_milestone_exempt_used"] = True
        if verify_enabled:
            # 闸 3（确定性）拒绝节流：本回合 update_roadmap 已被验收拒绝 ≥2 次 → 不再受理。
            # 真实事故：验收拒绝后模型换措辞无限重试，两回合烧光全部工具预算（连续两次
            # "已达上限"），用户的问题始终没被回答——确定性封锁重试循环，把剩余预算
            # 留给"直接回复用户补内容"。
            rejects = int(ctx.updates.get("coach_verify_rejects",
                                          ctx.state.get("coach_verify_rejects") or 0))
            if rejects >= 2:
                return _gate_event("milestone_throttle", {
                    "status": "blocked",
                    "error": "本回合 update_roadmap 已被验收拒绝 2 次，不再受理。",
                    "instruction": "停止调用 update_roadmap。立刻把缺失的内容作为回复完整讲给用户"
                                   "（内容只有发到对话里才算数）；下一轮用户回复后再勾选。",
                }, milestone=milestone_id, rejects=rejects)
            # 闸 4（语义）LLM 验收：独立验收员只看「里程碑标准 + 对话记录」——宣布完成/布置未做/
            # 转交无回应都不算完成。模型无注入通道（无 evidence 参数），想通过只能真把内容讲进对话。
            if ctx.progress:
                ctx.progress("🔍 正在核对里程碑完成情况...")
            verdict = verify_milestone(_milestone_desc(updated, milestone_id),
                                       _transcript_text(ctx.state.get("conversation"),
                                                        config.ROUTE_VERIFY_TRANSCRIPT_CHARS),
                                       _milestone_catalog(updated))
            # 验收员可能指出「最近内容其实匹配另一个里程碑」：只接受路线里真实存在的 id，
            # 且不能是本次勾选的 id，避免模型幻觉把 Agent 引到不存在的里程碑。
            suggested = verdict.get("suggested_id")
            valid_ids = {m.get("id") for s in (updated.get("stages") or [])
                         for m in (s.get("milestones") or [])}
            if suggested and (suggested == milestone_id or suggested not in valid_ids):
                suggested = None
            # 验收结论本身要留痕：通过对/拒绝都是信号（"模型宣布完成但被独立验收员否掉"
            # 正是里程碑事故的形态），只记被拒那次会让"验收几乎从没触发"这类问题查不出来
            audit("milestone_verify", milestone=milestone_id, verified=verdict["verified"],
                  missing_count=len(verdict.get("missing") or []),
                  reason=str(verdict.get("reason") or "")[:200],
                  suggested_id=suggested,
                  rejects=rejects, advance_directive=exit_intent.is_advance_directive(latest_user))
            if verdict["verified"] is False:
                ctx.updates["coach_verify_rejects"] = rejects + 1
                # 用户只授权「往下走」、没声明掌握：验收判不了完成是预期结果，不是待补的作业。
                # 确定性给出对外话术，避免模型自己发挥成「你补一句话我就勾」。
                if exit_intent.is_advance_directive(latest_user):
                    hint = (f"另外，最近内容更匹配里程碑「{suggested}」；"
                            f"如果你要记录刚讲完的内容，后续勾它，而不是 {milestone_id}。"
                            if suggested else "")
                    return {"status": "not_checked", "missing": verdict["missing"],
                            "reason": verdict["reason"], "suggested_id": suggested,
                            "note": "用户已授权直接推进，但没有声明自己已掌握该里程碑，验收无法确认完成。"
                                    "不要重试本工具、不要向用户索要措辞、不要解释验收机制；"
                                    "直接按用户的推进指令往下讲，并在回复里如实说明"
                                    "「该里程碑我未勾选，后续你可以随时命令我勾选」。" + hint}
                if suggested:
                    note = (f"验收未通过：最近对话内容更匹配里程碑「{suggested}」，"
                            f"而不是你勾的「{milestone_id}」。"
                            f"如果你要记录的是刚讲完的内容，请改勾 {suggested}；"
                            f"如果你确实要勾 {milestone_id}，先把它的要求讲进对话。"
                            "不要向用户索要「补一句话」式的关键词，也不要向用户解释验收机制。")
                else:
                    note = ("验收未通过：以上内容在对话里没有实质完成证据。停止重试本工具；"
                            "先把缺失内容作为回复完整讲给用户，下一轮再勾选。"
                            "不要向用户索要「补一句话」式的关键词，也不要向用户解释验收机制。")
                return {"status": "rejected", "missing": verdict["missing"],
                        "reason": verdict["reason"], "suggested_id": suggested,
                        "action": "teach_then_recheck", "note": note}
            # 走到这里只剩 True / None（None=验收器异常降级放行）
            evidence = "verified" if verdict["verified"] is True else "degraded"

    learner.save_roadmap(updated)
    ctx.updates["roadmap"] = updated
    _record_capability_event(updated, milestone_id, done, evidence, ctx)
    prog = roadmap_domain.stage_progress(updated, updated["current_stage"])
    # 里程碑推进卡点：勾选（done=True）后，若用户上一条回复不是明确推进指令，
    # 设置待确认标记 → 提示词强制停下总结 + 询问（用户回复后由 coach_human 清空）。
    # 用户上一轮明确「直接推进/不用再问」→ 免确认直接推进，不设卡点。
    if done:
        turn_count = int(ctx.updates.get("coach_milestone_turn_count",
                                         ctx.state.get("coach_milestone_turn_count") or 0)) + 1
        ctx.updates["coach_milestone_turn_count"] = turn_count
        if not exit_intent.is_advance_directive(_recent_user_text(ctx.state.get("coach_messages"))):
            ctx.updates["coach_milestone_pending"] = milestone_id
        note = (f"该里程碑已勾选（已通过验收）。本回合已勾选 {turn_count} 个里程碑；"
                "同一阶段内可以继续勾选，但不要跨阶段。"
                "勾完本阶段后请向用户总结并询问是否进入下一阶段；"
                "在用户明确同意前不要开始下一阶段内容。")
    else:
        note = "已取消勾选该里程碑。"
    return {"status": "ok", "milestone_id": milestone_id, "done": done,
            "current_stage": updated["current_stage"],
            "progress": f"{prog['done']}/{prog['total']}",
            "roadmap_status": updated.get("status"),
            "note": note}


def _milestone_desc(roadmap: dict, milestone_id: str) -> str:
    """按 id 取里程碑描述（验收标准），找不到返回 id 本身兜底。"""
    loc = roadmap_domain.find_milestone(roadmap, milestone_id)
    if not loc:
        return milestone_id
    si, mi = loc
    return (roadmap["stages"][si]["milestones"][mi].get("desc") or milestone_id)


def _milestone_catalog(roadmap: dict) -> str:
    """把全部里程碑拼成「id: 描述」清单（验收员判断内容实际匹配哪一项用）。"""
    return "\n".join(
        f"- {m.get('id')}: {m.get('desc')}"
        for s in (roadmap.get("stages") or [])
        for m in (s.get("milestones") or [])
    )


def _revise_roadmap(args: dict, ctx: CoachCtx) -> dict:
    """修订当前学习路线：校验 → build_roadmap 重建结构 → merge_progress 保留已完成进度 → 落盘。

    与 _generate_roadmap 的区别：只允许在已有路线上升级（无路线报错）；build 后必须走
    merge_progress 合并旧进度，避免把已勾选里程碑清零（build_roadmap 是全量重建）。
    """
    old = ctx.roadmap
    if not old:
        return {"status": "error", "error": "还没有路线，请先走问卷 + 路线规划"}
    goal = str(args.get("goal") or "").strip()
    total_hours = args.get("total_hours")
    stages = args.get("stages") or []
    if not goal or not isinstance(total_hours, int) or total_hours <= 0 or not stages:
        return {"status": "error", "error": "缺少 goal / total_hours / stages",
                "hint": "请按 revise_roadmap 的 schema 补全后重试"}
    norm, errors = roadmap_domain.normalize_stages(stages)
    if not norm:
        return {"status": "error", "errors": errors or ["没有合法阶段"],
                "hint": "请修正阶段结构（每阶段至少 1 个里程碑）后重新调用 revise_roadmap"}
    new = roadmap_domain.build_roadmap(ctx.tech, goal, total_hours, norm)
    merged = roadmap_domain.merge_progress(old, new)
    if ctx.thread_id:
        merged["session_thread_id"] = ctx.thread_id
    jp = learner.save_roadmap(merged)
    ctx.updates["roadmap"] = merged
    ctx.updates["roadmap_path"] = str(jp)
    kept = sum(1 for s in (merged.get("stages") or [])
               for m in (s.get("milestones") or []) if m.get("done"))
    return {
        "status": "ok", "roadmap_path": str(jp),
        "stages": [{"id": s["id"], "name": s["name"], "est_hours": s["est_hours"]}
                   for s in merged["stages"]],
        "current_stage": merged["current_stage"],
        "total_hours": merged["total_hours"],
        "kept_done": kept,
        "note": "路线已修订，已完成里程碑进度已保留。请把新路线完整呈现给用户。",
    }


_TOOL_IMPL = {
    "generate_roadmap": _generate_roadmap,
    "confirm_roadmap": _confirm_roadmap,
    "get_roadmap": _get_roadmap,
    "collect": _collect,
    "read": _read,
    "ask": _ask,
    "note": _note,
    "note_commit": _note_commit,
    "update_roadmap": _update_roadmap,
    "revise_roadmap": _revise_roadmap,
    "remember": _remember,
    "recall_memory": _recall_memory,
}

# 「贵工具」：单次执行会烧外部额度（Tavily 搜索 / Firecrawl 抓取）+ 分钟级耗时。
# ask（本地 RAG + 一次 LLM）与 note（本地沉淀）不算贵，不进这个集合。
# 用途：① 单轮上限（graph._coach_guard）② 失败后拒绝同回合重跑（见 _heavy_blocked）。
HEAVY_TOOLS = frozenset({"collect", "read"})

# 早期阶段：流水线还没烧抓取与生成，允许放行一次重跑；其余阶段失败即拒（重跑要全部重演）
_EARLY_STAGES = frozenset({"search"})


def _gate_event(kind: str, payload: dict, **fields) -> dict:
    """记一条「代码否决模型」的事件，并原样返回工具回执。

    否决族（贵工具失败闸 / 里程碑各闸）是行为信号里最值钱的一类：出问题时用户只看到
    一句话，而这里留下的是"模型想做什么、被哪条闸拦下、当时计数多少"——没有它，
    「模型宣布完成 → 代码否决 → 换措辞重试 → 烧光预算」这条链在日志里完全不可见。
    """
    audit("tool_gate", kind=kind, status=payload.get("status"), **fields)
    return payload


def _heavy_blocked(ctx: CoachCtx, tool: str) -> dict | None:
    """贵工具失败闸：同回合内失败过的贵工具不再让它重跑。

    重跑 = 重新搜索 + 重新抓取 + 重新生成（含外部额度与分钟级耗时），而这些在失败前大多
    已经成功过。搜索段的首次失败例外放行（早期失败，流水线未烧）。
    """
    rec = (ctx.state.get("coach_heavy_failures") or {}).get(tool) or {}
    count, stage = int(rec.get("count") or 0), rec.get("stage")
    if count == 0 or (count == 1 and stage in _EARLY_STAGES):
        return None
    return _gate_event("heavy_blocked", {
        "status": "blocked",
        "hint": (f"{tool} 本回合已经失败过一次（{stage} 阶段），不要再调用它：重跑会重新搜索、"
                 "重新抓取、重新生成，白烧搜索/抓取额度。请直接把情况告诉用户，建议稍后再试。"),
    }, tool=tool, stage=stage, failures=count)


def _record_heavy_failure(ctx: CoachCtx, tool: str, stage: str) -> None:
    """记下贵工具的失败（同回合后续调用据 _heavy_blocked 拦截）。"""
    prior = dict(ctx.state.get("coach_heavy_failures") or {})
    rec = prior.get(tool) or {}
    ctx.updates["coach_heavy_failures"] = {
        **prior, tool: {"count": int(rec.get("count") or 0) + 1, "stage": stage},
    }


def run_coach_tool(name: str, args: dict, ctx: CoachCtx) -> dict:
    """按名分发工具；未知工具返回错误（护栏层保证不会出现）。"""
    fn = _TOOL_IMPL.get(name)
    if fn is None:
        return {"status": "error", "error": f"未知工具: {name}"}
    return fn(args, ctx)


# ============================================================
# 三舱记忆整理（事实/未决/脉络）
# 核心原则：LLM 只看新消息产增量；已积累的记忆永不再过 LLM 的手（衰减结构性归零）。
# ============================================================

CONSOLIDATE_MEMORY_PROMPT = """你是学习会话的记忆整理助手。我会给你：现有「稳定事实」列表、现有「未决事项」列表（带编号）、以及一批刚发生的对话。请为长期记忆做一次增量整理。

## 输出
只输出一个 JSON 对象，不要任何解释或 ```json 代码块标记：
{"facts_add": ["新稳定事实，每条一句话"],
 "open_add": ["新未决事项，每条一句话"],
 "resolved": [已解决的未决事项编号，如 2],
 "context": "最近学习进展的一段话（150 字以内）"}

## 判定标准
- facts_add（稳定事实=整个会话都该记住的）：用户画像的变化（水平/目标/时间预算）、学习偏好（如「喜欢类比」「少贴长代码」）、对陪练的纠正（如「不要用英文术语」）、重要决定（如「先跳过 Maven 直接学 Gradle」）。与「现有稳定事实」重复或同义的不输出；寒暄、过程性内容、普通问答不输出。
- open_add（未决事项=仍开放的问题/承诺/待确认）：用户提出但还没解决的问题、陪练答应之后要讲的主题、待用户确认的事。
- resolved：现有未决事项中，已被新对话回答/完成/用户明确不再关心的编号。没有则输出 []。
- context：只写最近学了什么、当前焦点、下一步意向；画像、决定、未决事项不要写进来（它们有独立位置，写进 context 会被反复重写）。

## 正例
- 用户说「这个类比好懂，后面多举例子」→ facts_add: ["用户反馈类比讲解效果好，希望多用类比"]
- 陪练讲完了「[2] 待讲：事务传播」→ resolved: [2]
- 用户问「AOP 切面执行顺序还没搞懂」→ open_add: ["AOP 切面执行顺序还没搞懂"]

## 反例（不要这样做）
- 把「用户问了 X」塞进 facts_add——已回答的普通问答不是事实
- 在 context 里复述画像或决定
- 输出解释文字、markdown 或编造未给出的编号
"""


def consolidate_memory(existing: dict, messages: list[dict], tech: str) -> dict:
    """三舱记忆增量整理：LLM 提取增量 → 确定性应用（去重追加 / 按 id 淘汰 / 机械上限）。

    Args:
        existing: 现有三舱 {"facts": [str], "open_items": [{"id": int, "text": str}],
                  "summary": str}
        messages: 本次被压缩掉的旧消息（只取 user/assistant 文本，工具机器态不入）
        tech: 技术名

    Returns:
        应用增量后的完整三舱 {"facts", "open_items", "summary"}。
        LLM 失败 / JSON 解析失败 → 三舱原样返回（宁可少记一窗增量，不拿既有积累冒险）。
    """
    facts = list(existing.get("facts") or [])
    open_items = list(existing.get("open_items") or [])
    old_summary = existing.get("summary") or ""

    parts = []
    for m in messages:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            parts.append(f"{m['role']}：{m['content']}")
    block = "\n".join(parts[-50:])  # 单次整理输入封顶
    if not block.strip():
        return {"facts": facts, "open_items": open_items, "summary": old_summary}

    facts_txt = "\n".join(f"- {f}" for f in facts) or "（无）"
    open_txt = "\n".join(f"- [{it['id']}] {it['text']}" for it in open_items) or "（无）"
    user_content = (f"技术：{tech}\n\n"
                    f"===== 现有稳定事实 =====\n{facts_txt}\n"
                    f"===== 现有未决事项 =====\n{open_txt}\n"
                    f"===== 刚发生的对话 =====\n{block}")
    try:
        raw = generate_text(CONSOLIDATE_MEMORY_PROMPT, user_content, call_site="coach.consolidate")
    except Exception as exc:  # noqa: BLE001 —— LLM 不可用时三舱原样保留
        # 静默降级也要留痕：不然"记忆没涨"看起来像"这轮确实没有新事实"，
        # 而实际是每次压缩都在丢增量
        audit("error", kind=type(exc).__name__, stage="coach.consolidate",
              message=str(exc)[:200])
        return {"facts": facts, "open_items": open_items, "summary": old_summary}
    obj = parse_json_object(raw)
    if not obj:
        # 只记长度不记原文：这段 raw 是记忆增量（学习内容），绝不能进日志
        audit("error", kind="unparsable_output", stage="coach.consolidate",
              message=f"记忆增量解析失败（{len(raw or '')} 字符）")
        return {"facts": facts, "open_items": open_items, "summary": old_summary}

    # 确定性应用：facts 去重追加
    known = {f.strip() for f in facts}
    for f in obj.get("facts_add") or []:
        f = str(f).strip()
        if f and f not in known:
            facts.append(f)
            known.add(f)
    # open 追加（id 全局递增）
    next_id = max((it.get("id") or 0) for it in open_items) if open_items else 0
    for t in obj.get("open_add") or []:
        t = str(t).strip()
        if t:
            next_id += 1
            open_items.append({"id": next_id, "text": t})
    # resolved 按 id 确定性淘汰（不存在的 id 忽略）
    resolved: set[int] = set()
    for r in obj.get("resolved") or []:
        try:
            resolved.add(int(r))
        except (TypeError, ValueError):
            continue
    open_items = [it for it in open_items if it.get("id") not in resolved]

    # 机械上限：facts / open 超限丢最旧；脉络舱字符上限兜底（防膨胀，原死配置补上机械约束）
    facts = facts[-config.COACH_FACTS_MAX:]
    open_items = sorted(open_items, key=lambda it: it.get("id") or 0)[-config.COACH_OPEN_MAX:]
    ctx = str(obj.get("context") or "").strip()
    summary = ctx[:config.COACH_SUMMARY_MAX_CHARS] if ctx else old_summary
    return {"facts": facts, "open_items": open_items, "summary": summary}
