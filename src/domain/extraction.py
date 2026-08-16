"""LLM 输出解析的纯函数（零 I/O、零框架依赖）。

自 agent.py 迁出：extract_json_object（原 Agent._extract_json_object 自由函数化）、
as_list（原 _parse_entries 内联助手）、parse_entries、parse_classify。
"""

import json
import re


def extract_json_object(s: str) -> dict:
    """从文本中提取第一个完整的 JSON 对象。

    用花括号配对 + 字符串状态机定位 JSON 边界，避免非贪婪正则
    在内容里的 `}`（如 markdown 代码块）处提前截断。

    Args:
        s: 从 "Action Input:" 之后开始的文本

    Returns:
        解析出的 dict；解析失败返回 {}
    """
    start = s.find("{")
    if start == -1:
        return {}

    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError:
                        return {}
    return {}


def as_list(data) -> list[dict]:
    """把解析结果规整为 dict 列表（丢弃非 dict 元素）。"""
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def parse_entries(raw: str) -> list[dict]:
    """从 LLM 响应中稳健地解析知识点 JSON 数组。

    兼容：去掉 ```json 代码块包裹、从文本中抽取第一个 JSON 数组。

    Returns:
        [{topic, tags, content}, ...]，解析失败返回 []
    """
    text = raw.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # 直接解析整个响应
    try:
        return as_list(json.loads(text))
    except Exception:
        pass

    # 抽取第一个 JSON 数组块
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return as_list(json.loads(m.group(0)))
        except Exception:
            pass
    return []


def parse_json_object(raw: str) -> dict:
    """从 LLM 响应中稳健地解析单个 JSON 对象。

    兼容：去掉 ```json 代码块包裹、抽取第一个 JSON 对象。

    Returns:
        dict，解析失败返回 {}
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # 兜底：用花括号配对抽取第一个 JSON 对象
    obj = extract_json_object(text)
    return obj if isinstance(obj, dict) else {}


def parse_classify(raw: str) -> dict:
    """从 LLM 响应中稳健地解析文档分类结果。

    Returns:
        {"is_technical": bool, "reason": str}，解析失败返回空 dict
    """
    return parse_json_object(raw)
