"""退出意图确定性检测（纯规则，零 I/O、零框架依赖）。

用户说「停 / 结束 / 今天就到这」等走代码判定，绝不交给模型——退出是高风险
控制流，必须机器可复现（契合 PROMPT_DESIGN「确定性信息由代码判定」）。

为避免把「我还没结束学习」这类含关键词的反向表达误判为退出，只对**短文本**
命中判定（≤8 字符）；长回答由模型按语义处理。
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
