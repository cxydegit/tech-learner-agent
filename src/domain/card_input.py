"""卡片级校验 + 统一契约解析（纯规则，零 I/O、零框架依赖）。

Step 5「场景卡片 + 框内自由文本」的落地层：所有功能共用同一入口模型——
CLI（REPL + standalone）与未来 Web 表单都通过这里把用户输入解析成统一的契约
dict，喂同一个 LangGraph 图。Web 化时前端只换输入形态（表单渲染/校验），契约层零改动。

契约原则（focus / args 分工，不是替代）：
- collect → 结构化字段 {command, tech, focus?}：focus 是「有语义的自由文本」——
  本次收集的叙述倾向，直接决定走固定模板（无 focus）还是非固定模板（有 focus）。
- read / ask → 位置参数 {command, args: [...]}：read 的 url、ask 的问题。
- 卡片命令名 ask 对应图命令 qa（graph.py 的路由 key 是 qa）。
"""

_REQUIRED_MESSAGES = {
    "collect": "请输入技术名",
    "read": "请输入链接",
    "ask": "请输入问题",
}


def parse_card_input(command: str, tokens: list[str]) -> dict:
    """把命令的 token 列表解析为统一的卡片契约 dict。

    Args:
        command: 卡片命令名（collect / read / ask）
        tokens: shlex 切分后的参数列表（不含命令本身）

    Returns:
        成功 → 契约 dict：
          collect → {command, tech, focus?}
          read   → {command: "read", args: [url]}
          ask    → {command: "qa", args: [question]}
        失败 → {"error": "<提示文案>"}（调用方打印提示，不进管道）
    """
    command = command.strip().lower()
    if command == "collect":
        tech = (tokens[0] or "").strip() if tokens else ""
        if not tech:
            return {"error": _REQUIRED_MESSAGES["collect"]}
        payload = {"command": "collect", "tech": tech}
        focus = " ".join((t or "").strip() for t in tokens[1:]).strip() or None
        if focus:
            payload["focus"] = focus
        return payload
    if command == "read":
        url = (tokens[0] or "").strip() if tokens else ""
        if not url:
            return {"error": _REQUIRED_MESSAGES["read"]}
        return {"command": "read", "args": [url]}
    if command == "ask":
        question = " ".join((t or "").strip() for t in tokens).strip()
        if not question:
            return {"error": _REQUIRED_MESSAGES["ask"]}
        return {"command": "qa", "args": [question]}
    return {"error": f"未知命令: {command}"}
