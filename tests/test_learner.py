"""adapters/learner 单测：画像 / 路线文件读写（临时目录 + monkeypatch config 路径）。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_learner.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters import learner as le
from src.config import config
from src.domain import roadmap as rm


def _mk_roadmap():
    stages, _ = rm.normalize_stages(
        [{"name": "环境搭建", "goal": "跑通 hello world", "est_hours": 2,
          "milestones": [{"desc": "安装完成"}]}])
    return rm.build_roadmap("spring-boot", "能跑通最小项目", 10, stages,
                            created_at="2026-08-20 10:00")


def test_profile_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    le.save_profile({"tech": "t", "bucket": "developer"})
    assert le.load_profile()["bucket"] == "developer"


def test_profile_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    assert le.load_profile() == {}


def test_roadmap_roundtrip_and_md(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    jp = le.save_roadmap(_mk_roadmap())
    assert jp.exists()
    loaded = le.load_roadmap("spring-boot")
    assert loaded["current_stage"] == "s1"
    assert loaded["tech"] == "spring-boot"
    md = (tmp_path / "roadmaps" / "spring-boot-roadmap.md").read_text(encoding="utf-8")
    assert "学习路线" in md
    assert "[ ]" in md


def test_load_roadmap_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    assert le.load_roadmap("nothing") is None


def test_save_tech_profile_merges_by_tech(tmp_path, monkeypatch):
    """画像按 tech 键归档，重复保存覆盖该 tech 条目、保留其他 tech。"""
    monkeypatch.setattr(config, "LEARNER_DIR", tmp_path / "learner")
    le.save_tech_profile("Spring Boot", {"self_level": 8, "bucket": "developer"})
    le.save_tech_profile("FastAPI", {"self_level": 4})
    profile = le.load_profile()
    assert profile["spring-boot"]["bucket"] == "developer"
    assert profile["fastapi"]["self_level"] == 4
    le.save_tech_profile("Spring Boot", {"self_level": 9})
    assert le.load_profile()["spring-boot"]["self_level"] == 9
    assert len(le.load_profile()) == 2


def test_roadmap_corrupt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROADMAP_DIR", tmp_path / "roadmaps")
    (tmp_path / "roadmaps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "roadmaps" / "bad.json").write_text("{not json", encoding="utf-8")
    assert le.load_roadmap("bad") is None
