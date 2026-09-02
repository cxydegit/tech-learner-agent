"""Web 服务包。

约束：`import src.web` 顶层不得加载 chromadb / langgraph。
本包 __init__ 保持空；重依赖（SqliteSaver / graph / Command / vector）全部在
runner / sessions 的函数内 lazy import。
"""
