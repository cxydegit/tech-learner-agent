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

    # Agent 是否使用原生 function calling（true）或文本正则解析（false）。
    # 默认 false：阿里云百炼 qwen3.7-plus 等模型不返回原生 tool_calls（返回文本形式），
    # 用文本解析更稳定；遇到支持原生 function calling 的模型可设 AGENT_USE_FUNCTION_CALLING=true。
    AGENT_USE_FUNCTION_CALLING: bool = os.getenv("AGENT_USE_FUNCTION_CALLING", "false").lower() == "true"

    # 路径配置
    BASE_DIR: Path = Path(__file__).parent.parent
    MATERIALS_DIR: Path = BASE_DIR / "materials"
    REPORTS_DIR: Path = BASE_DIR / "reports"
    KNOWLEDGE_DIR: Path = BASE_DIR / "knowledge"

    # RAG / Embedding 配置
    # Embedding 后端：阿里云百炼 text-embedding-v3（走现有 OPENAI_BASE_URL 兼容端点，零新依赖）
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
    # Chroma 本地持久化目录（运行时生成，已加入 .gitignore）
    CHROMA_DIR: Path = BASE_DIR / ".chroma"
    RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "5"))
    # 语义去重阈值：余弦相似度，召回 top1 且轻量 overlap 确认后才合并
    RAG_DEDUP_THRESHOLD: float = float(os.getenv("RAG_DEDUP_THRESHOLD", "0.55"))
    # read 历史召回阈值：命中已有解读则提示复用（URL 路径片段作查询词，语义噪声大，阈值取高）
    RAG_READ_THRESHOLD: float = float(os.getenv("RAG_READ_THRESHOLD", "0.62"))
    # 文档分块参数（字符数）
    RAG_CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", "800"))
    RAG_CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", "100"))

    # LangGraph checkpointer 持久化（SqliteSaver，跨会话/跨进程恢复）
    GRAPH_DB_DIR: Path = BASE_DIR / ".graph"
    GRAPH_DB_PATH: Path = GRAPH_DB_DIR / "checkpoints.sqlite"

    # Note 模块（Step 3 差量提取）：召回已有笔记作上下文的预算参数
    NOTE_RECALL_TOP_K: int = int(os.getenv("NOTE_RECALL_TOP_K", "3"))  # 召回该 tech 已有笔记 top-k 作差量上下文
    NOTE_CONTEXT_LIMIT: int = int(os.getenv("NOTE_CONTEXT_LIMIT", "500"))  # 每条已有笔记在提取提示词里的截断字数

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