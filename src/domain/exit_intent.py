"""用户控制流意图的确定性检测（纯规则，零 I/O、零框架依赖）。

两类判定，均不交给模型——退出与「推进授权」都是控制流，必须机器可复现
（契合 PROMPT_DESIGN「确定性信息由代码判定」）：

- ``is_exit_intent``：用户说「停 / 结束 / 今天就到这」→ 结束会话。为避免把
  「我还没结束学习」这类反向表达误判为退出，只对**短文本**（≤8 字符）命中判定。
- ``is_advance_directive``：用户**明确指令**直接推进学习（「直接进入下一阶段 /
  直接推进 / 不用再问」）→ 里程碑勾选后免确认。关键词都带「直接」或豁免类词，
  指令性强，放宽到 ≤30 字符命中即判；误判代价低（少问/多问一次）。
"""

import re

_EXIT_RE = re.compile(
    r"(结束|退出|停止|停|不学了|今天就到|就到这里|算了吧|先这样吧|太乱了|收工|不搞了|放弃)",
    re.IGNORECASE,
)
_MAX_LEN = 8
# 语气词/标点：退出短语的允许残留（"结束了" → 残留"了" → 仍判退出）
_PARTICLES = "吧了啊呢嘛好呀哦这~～。，,.！!？?"


def is_exit_intent(text: str) -> bool:
    """用户输入是否表达退出意图。

    判定规则（防误判「我还没结束学习呢」这类反向短句）：
    1. 短文本（≤8 字符）命中退出关键词；
    2. 且关键词外的残留只含语气词/标点——即整句基本就是"结束/不学了"的意思。
    """
    t = (text or "").strip()
    if not t or len(t) > _MAX_LEN:
        return False
    m = _EXIT_RE.search(t)
    if not m:
        return False
    residue = (t[: m.start()] + t[m.end():]).strip(_PARTICLES)
    return not residue


# 明确推进/豁免指令关键词：带「直接」前缀的推进短语，或「不用/别问」类豁免词。
# 命中即判定（不做严格残留检查——「不用再确认了，直接进入下一阶段」这类复合句
# 残留非语气词，但仍是明确指令）；只限短文本防长句里的普通内容误判。
_ADVANCE_RE = re.compile(
    r"(直接(进入|开始|推进|进行|做|学|上)?(下一|下一阶段|下个阶段|下一步|下阶段|下一里程碑|下一个里程碑|后续|后面)"
    r"|直接(推进|继续|开始|进行)"
    r"|(不用|无需|别)(再)?(问|确认|问了|确认了|问我|确认我|过问))",
    re.IGNORECASE,
)
_ADV_MAX_LEN = 30


def is_advance_directive(text: str) -> bool:
    """用户上一条回复是否为「直接推进 / 免确认」的明确指令。

    命中（如「直接进入下一阶段」「直接推进」「不用再问」）→ True：里程碑勾选后
    免去确认询问直接推进。未命中（如「进入下一阶段之前先把当前讲完」）→ False。
    """
    t = (text or "").strip()
    if not t or len(t) > _ADV_MAX_LEN:
        return False
    return bool(_ADVANCE_RE.search(t))


# 用户完成声明关键词：里程碑常在对话外完成（如「本地跑通 hello world」在 IDE 里），
# 对话中天然无证据；用户明确声明完成 → 验收豁免（信任用户对自己离线动作的判断）。
_CLAIM_RE = re.compile(
    r"(学会了|都会了|搞定了|做完了|完成了|跑通了|装好了|弄好了|直接勾|勾了吧|勾选吧|跳过吧)",
    re.IGNORECASE,
)
_CLAIM_MAX_LEN = 40
# 否定/疑问守卫：「还没搞定」「搞定了没？」含关键词但不是完成声明
_CLAIM_NEGATE_RE = re.compile(r"(还没|尚未|没有|没|别|先别|吗|么)")


def is_completion_claim(text: str) -> bool:
    """用户上一条回复是否明确声明「已完成 / 直接勾选」（里程碑验收豁免）。

    命中且不含否定/疑问词（「还没搞定」「搞定了没？」不误判）→ True：跳过验收
    直接勾选。误豁免代价 = 少验一次（用户本就是完成与否的最终权威），保守取词。
    """
    t = (text or "").strip()
    if not t or len(t) > _CLAIM_MAX_LEN:
        return False
    if not _CLAIM_RE.search(t):
        return False
    return not _CLAIM_NEGATE_RE.search(t)
