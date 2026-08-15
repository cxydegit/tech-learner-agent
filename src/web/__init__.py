"""Web 服务包（WEB_PLAN.md §4-② 落点）。

I1 不变量（WEB_PLAN.md §9）：`import src.web` 顶层不得加载 chromadb / langgraph。
本包 __init__ 保持空；重依赖（SqliteSaver / graph / Command / vector）全部在
runner / sessions 的函数内 lazy import。
"""
