"""配置管理：加载环境变量和应用配置"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(Path(__file__).parent.parent / ".env")


class Config:
    """应用配置"""

    # API Keys
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    FIRECRAWL_API_KEY: str = os.getenv("FIRECRAWL_API_KEY", "")

    # LLM 配置
    LLM_MODEL: str = os.getenv("MODEL_NAME", "")
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))

    # 路径配置
    BASE_DIR: Path = Path(__file__).parent.parent
    MATERIALS_DIR: Path = BASE_DIR / "materials"
    REPORTS_DIR: Path = BASE_DIR / "reports"
    KNOWLEDGE_DIR: Path = BASE_DIR / "knowledge"

    # 搜索配置
    MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", "10"))
    MAX_FETCH_PAGES: int = int(os.getenv("MAX_FETCH_PAGES", "5"))

    # 抓取内容长度限制（字符数）
    MAX_FETCH_CHARS: int = int(os.getenv("MAX_FETCH_CHARS", "16000"))

    @classmethod
    def validate(cls) -> list[str]:
        """验证必要配置，返回缺失项列表"""
        missing = []
        if not cls.OPENAI_API_KEY:
            missing.append("OPENAI_API_KEY")
        if not cls.TAVILY_API_KEY:
            missing.append("TAVILY_API_KEY")
        if not cls.FIRECRAWL_API_KEY:
            missing.append("FIRECRAWL_API_KEY")
        return missing

    @classmethod
    def ensure_dirs(cls) -> None:
        """确保输出目录存在"""
        for d in [cls.MATERIALS_DIR, cls.REPORTS_DIR, cls.KNOWLEDGE_DIR]:
            d.mkdir(parents=True, exist_ok=True)


config = Config()