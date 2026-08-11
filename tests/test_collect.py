"""pipelines/collect 纯函数单测（零网络）：materials 文件名时间版本号 + excluded 汇报统计。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_collect.py -v
"""

import re
import sys
from pathlib import Path

# 保证 tests/ 下能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipelines.collect import _excluded_summary, materials_filename


# ---------- materials 文件名（时间版本号，Step 5 验收发现覆盖 bug） ----------

def test_materials_filename_has_timestamp_version():
    """每次运行带 MMDD-HHMM 时间版本号，区分多次询问。"""
    name = materials_filename("FastAPI")
    assert re.fullmatch(r"materials/fastapi-materials-\d{4}-\d{4}\.md", name)


def test_materials_filename_sanitizes_tech():
    """技术名小写、空格转连字符，版本号后缀仍存在。"""
    name = materials_filename("Spring Boot 3")
    assert name.startswith("materials/spring-boot-3-materials-")
    assert re.search(r"-\d{4}-\d{4}\.md$", name)


# ---------- excluded 汇报统计 ----------

def test_excluded_summary_empty():
    assert _excluded_summary([]) == ""


def test_excluded_summary_counts():
    s = _excluded_summary([{"url": "a", "reason": "内容农场"}, {"url": "b", "reason": "低分（-5）"}])
    assert "共排除 2 条" in s
    assert "内容农场 1 条" in s
    assert "低分（-5） 1 条" in s
