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
    """一次交互式学习会话的状态模型。

    这个类将多个命令（collect / read / note）的共享数据聚合在一起，
    并提供了序列化与持久化能力，相当于一个轻量级的会话存储（checkpointer）。

    Attributes:
        tech (str): 当前学习的核心技术栈，作为全局上下文。
        level (str): 学习水平（如"入门"），用于后续 LangGraph 的条件分支。
        urls (list[str]): 待处理或已收集的 URL 列表（队列）。
        visited (set[str]): 已经读取过的 URL 集合，用于去重。
        notes (list[dict]): 累积的笔记列表，每个笔记是一个字典。
        materials_path (str): 本地材料目录路径（预留字段）。
        history (list[dict]): 操作流水日志，记录每一步动作。
        created_at (str): 会话创建时间（字符串格式）。
        updated_at (str): 会话最后更新时间（字符串格式）。
    """
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
        """更新会话的修改时间戳为当前时间。

        此方法在每次会话内容变更时被调用，用于记录最后修改时刻。
        """
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add_history(self, action: str, detail: str) -> None:
        """向会话历史中追加一条操作记录。

        Args:
            action (str): 操作类型，如 "collect", "read", "note"。
            detail (str): 操作的详细描述，如 "收集了 5 个 URL"。
        """
        self.history.append({
            "action": action,
            "detail": detail,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        """将会话对象转换为可 JSON 序列化的字典。（对象->字典）

        处理特殊类型：将 visited（set）转换为排序后的列表，
        确保 JSON 序列化稳定且有序。

        Returns:
            dict: 包含所有字段的字典，其中 visited 被转为 list[str]。
        """
        d = asdict(self)
        d["visited"] = sorted(self.visited)  # set -> list，便于 JSON 序列化
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LearnSession":
        """从字典中恢复一个 LearnSession 实例。（字典->对象）

        反向操作 to_dict，将 visited 从 list 转回 set。

        Args:
            d (dict): 包含会话数据的字典（通常由 to_dict 生成）。

        Returns:
            LearnSession: 重建的会话对象。

        Note:
            此方法要求字典中的键必须与 dataclass 字段一致，
            若字段不完整会引发 KeyError（后续可增加容错逻辑）。
        """
        d = dict(d)
        d["visited"] = set(d.get("visited", []))
        return cls(**d)

    # ---------- 持久化（相当于 checkpointer 最小版） ----------

    def save(self) -> None:
        """将当前会话持久化到 JSON 文件。

        文件保存在 SESSION_DIR 下，以 session_id 命名（.json）。
        保存前会自动更新时间戳（调用 _touch）。
        """
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._touch()
        path = SESSION_DIR / f"{self.session_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, session_id: str) -> "LearnSession":
        """从 JSON 文件中加载指定的会话。 """
        path = SESSION_DIR / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def list(cls) -> list[dict]:
        """列出所有已保存的会话摘要信息。

        遍历 SESSION_DIR 下的所有 JSON 文件，
        提取 session_id, tech, level, notes_count, updated_at。

        Returns:
            list[dict]: 每个字典代表一个会话的摘要信息。
        """
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