"""用户画像 + 学习路线持久化（文件 I/O 层）。

延续项目「Markdown 是源」的哲学：路线 Markdown（roadmaps/<tech>-roadmap.md）
是人可读可编辑的产物，JSON（roadmaps/<tech>.json）只存机器态供程序推进；
画像（learner/profile.json）为单用户个人工具，全局单文件。
"""

import json
from pathlib import Path

from ..config import config
from ..domain.dedup import sanitize_filename
from ..domain.roadmap import roadmap_to_markdown


def profile_path() -> Path:
    return config.LEARNER_DIR / "profile.json"


def load_profile() -> dict:
    """读取全局用户画像；无文件 / 损坏返回 {}。"""
    p = profile_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 损坏/不可解析时按空处理（个人工具，宁可重填）
        return {}


def save_profile(profile: dict) -> Path:
    """写回用户画像，返回路径。"""
    p = profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def save_tech_profile(tech: str, entry: dict) -> dict:
    """把某个技术的学习档案合并进全局 profile.json（按 tech 归档，保留历史技术）。

    Args:
        tech: 技术名（原始大小写）
        entry: 该技术的学习档案（self_level / related / goal / time_budget / bucket / roadmap_path）

    Returns:
        更新后的完整 profile dict
    """
    profile = load_profile()
    profile[sanitize_filename(tech)] = entry
    save_profile(profile)
    return profile


def roadmap_json_path(tech: str) -> Path:
    return config.ROADMAP_DIR / f"{sanitize_filename(tech)}.json"


def roadmap_md_path(tech: str) -> Path:
    return config.ROADMAP_DIR / f"{sanitize_filename(tech)}-roadmap.md"


def load_roadmap(tech: str) -> dict | None:
    """读路线机器态；无文件 / 损坏返回 None。"""
    p = roadmap_json_path(tech)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save_roadmap(roadmap: dict) -> Path:
    """写路线 JSON + 渲染 Markdown 源文件，返回 JSON 路径。"""
    jp = roadmap_json_path(roadmap["tech"])
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(roadmap, ensure_ascii=False, indent=2), encoding="utf-8")
    roadmap_md_path(roadmap["tech"]).write_text(roadmap_to_markdown(roadmap), encoding="utf-8")
    return jp
