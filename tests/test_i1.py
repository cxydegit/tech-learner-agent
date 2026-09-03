"""I1 不变量断言：`import src.cli` 不得把 chromadb/langgraph 放入 sys.modules。

守护点：cli.py 顶层只 import 轻量管道；重依赖（chromadb/vector、langgraph/graph）全部
函数内 lazy。qa.py 的 `_search_notes` 走函数内 lazy import vector 守住此条。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_i1.py -v
"""

import sys
from pathlib import Path

# 保证 tests/ 下能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_import_cli_keeps_heavy_deps_out():
    """import src.cli 自身不引入 chromadb / langgraph。"""
    # 先清掉可能被其他测试拉起的重依赖，再测「cli 导入链不引入」
    for name in ("chromadb", "langgraph"):
        sys.modules.pop(name, None)
    import src.cli  # noqa: F401 —— 仅验证导入不变量
    assert "chromadb" not in sys.modules
    assert "langgraph" not in sys.modules
