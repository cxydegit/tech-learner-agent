"""文件浏览 API：列出 / 读取 materials/ reports/ knowledge/ 的 markdown。

路径白名单限定三目录，防路径穿越：
- 列目录：扁平列出三目录下所有 `.md`（含子目录），path 用相对项目根的 posix 路径；
- 读文件：校验 path 落在白名单内（杜绝绝对路径 / 盘符 / `..` 跳出），再读取内容。

本模块顶层只依赖轻量模块（config），无重依赖。
"""

from pathlib import Path

from ..config import config

# 白名单三目录：(key, Path)。key 同时是 path 前缀与返回字段名。
_ALLOWED_DIRS: tuple[tuple[str, Path], ...] = (
    ("materials", config.MATERIALS_DIR),
    ("reports", config.REPORTS_DIR),
    ("knowledge", config.KNOWLEDGE_DIR),
)


def _resolve(path: str) -> Path | None:
    """校验并解析白名单内文件路径；非法（穿越/越权/非文件）返回 None。"""
    path = (path or "").replace("\\", "/").lstrip("/")
    # 硬校验前缀：杜绝绝对路径 / 盘符（C:/...）绕过 BASE_DIR 拼接
    if not path.startswith(tuple(f"{k}/" for k, _ in _ALLOWED_DIRS)):
        return None
    try:
        p = (config.BASE_DIR / path).resolve()
    except Exception:  # noqa: BLE001 —— 非法路径（含 NUL 等）直接拒绝
        return None
    for _key, d in _ALLOWED_DIRS:
        if p.is_relative_to(d.resolve()) and p.is_file():
            return p
    return None


def list_docs() -> dict:
    """列出三目录的 markdown 文件：{materials: [...], reports: [...], knowledge: [...]}。"""
    out: dict[str, list[dict]] = {}
    for key, d in _ALLOWED_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        files = []
        for p in sorted(d.rglob("*.md")):
            files.append({
                "path": p.relative_to(config.BASE_DIR).as_posix(),
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
        out[key] = files
    return out


def read_doc(path: str) -> dict | None:
    """读取单个 markdown 文件内容；路径穿越 / 越权 / 不存在返回 None。"""
    resolved = _resolve(path)
    if resolved is None:
        return None
    return {
        "path": path,
        "content": resolved.read_text(encoding="utf-8", errors="replace"),
    }
