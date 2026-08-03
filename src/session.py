"""学习会话层：跨命令的状态建模与持久化（/learn REPL 的载体）。

LearnSession 是"状态 schema 的纸面模型"，为 Stage 3 的 LangGraph LearnState 铺路：
先证明状态设计（tech / urls / visited / notes / level / history），再交给框架。
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import config

SESSION_DIR = config.BASE_DIR / ".leamsessions"


@dataclass
class LearnSession:
    """一次交互式学习会话的状态模型。"""

    session_id: str
    tech: str = ""
    level: str = "入门"
    urls: list[str] = field(default_factory=list)
    visited: set[str] = field(default_factory=set)
    notes: list[dict] = field(default_factory=list)
    materials_path: str = ""
    history: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ---------- 状态维护 ----------

    def _touch(self) -> None:
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add_history(self, action: str, detail: str) -> None:
        self.history.append({
            "action": action,
            "detail": detail,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["visited"] = sorted(self.visited)  # set -> list，便于 JSON 序列化
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LearnSession":
        d = dict(d)
        d["visited"] = set(d.get("visited", []))
        return cls(**d)

    # ---------- 持久化（相当于 checkpointer 最小版） ----------

    def save(self) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._touch()
        path = SESSION_DIR / f"{self.session_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, session_id: str) -> "LearnSession":
        path = SESSION_DIR / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def list(cls) -> list[dict]:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        sessions = []
        for p in sorted(SESSION_DIR.glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            sessions.append({
                "session_id": d.get("session_id", ""),
                "tech": d.get("tech", ""),
                "level": d.get("level", ""),
                "notes_count": len(d.get("notes", [])),
                "updated_at": d.get("updated_at", ""),
            })
        return sessions