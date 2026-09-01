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
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")  # 可选；设了才查 GitHub star 数（质量预筛），没设自动跳过

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
    # 去重候选送 LLM 判定的相似度下限：语义召回 top2 里低于此的不送判定（省 LLM 调用）。
    # 实测同义改写对源笔记相似度 ≥0.50，0.4 以下几乎不可能是同一篇。
    RAG_DEDUP_JUDGE_SIM_MIN: float = float(os.getenv("RAG_DEDUP_JUDGE_SIM_MIN", "0.4"))
    # read 历史召回阈值：命中已有解读则提示复用（URL 路径片段作查询词，语义噪声大，阈值取高）
    RAG_READ_THRESHOLD: float = float(os.getenv("RAG_READ_THRESHOLD", "0.62"))
    # 文档分块参数（字符数）
    RAG_CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", "800"))
    RAG_CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", "100"))
    # 分块硬上限（字符）：超出它的表格 / 代码块不再整块原子保留，按逻辑结构二次切分
    # （表格按行重复表头 / 代码按空行分组），防超大块超出 embedding 输入上限。仅拦截病态块。
    RAG_CHUNK_HARD_CAP: int = int(os.getenv("RAG_CHUNK_HARD_CAP", "8192"))
    # P3 索引对账（孤儿分块清理）：磁盘文件删除 / 改名后自动清理 Chroma 残留分块。
    # index_paths 末尾与 /ask 惰性入口都受此开关控制。
    RAG_RECONCILE: bool = os.getenv("RAG_RECONCILE", "true").lower() == "true"
    # /ask 惰性对账节流间隔（秒）：只在此间隔内的首次 /ask 做一次元数据对账（毫秒级），
    # 避免每次提问都扫全库；index_paths 末尾的对账不走节流（写入路径自愈）。
    RAG_RECONCILE_INTERVAL: int = int(os.getenv("RAG_RECONCILE_INTERVAL", "300"))
    # /ask 路径单次对账补缺失的文件数上限（P3.1）：提问时补缺失会触发 embedding 调用，
    # 限量避免拖延迟；写路径（index_paths / 写笔记）不限，缺口随下次写入全量补齐。
    RAG_RECONCILE_BACKFILL_MAX: int = int(os.getenv("RAG_RECONCILE_BACKFILL_MAX", "3"))

    # LangGraph checkpointer 持久化（SqliteSaver，跨会话/跨进程恢复）
    GRAPH_DB_DIR: Path = BASE_DIR / ".graph"
    GRAPH_DB_PATH: Path = GRAPH_DB_DIR / "checkpoints.sqlite"

    # 定制化学习路线（模块 2）：coach agent 循环配置
    # 工具调用护栏：每用户回合最大连续工具调用数（超限强制 interrupt 找用户确认方向，防死循环）
    ROUTE_MAX_TOOL_CALLS_PER_TURN: int = int(os.getenv("ROUTE_MAX_TOOL_CALLS_PER_TURN", "8"))

    # 图级执行硬上限（LangGraph recursion_limit，防 agent 失控打转）
    ROUTE_RECURSION_LIMIT: int = int(os.getenv("ROUTE_RECURSION_LIMIT", "50"))

    # 上下文管理：coach 模型上下文每次只带最近 N 轮（一问一答约 2 条/轮）
    #压缩后保留 20 条消息（最近 10 轮对话）
    COACH_HISTORY_KEEP: int = int(os.getenv("COACH_HISTORY_KEEP", "10"))
    #消息数 超过 40 条 才触发压缩
    COACH_COMPRESS_AT: int = int(os.getenv("COACH_COMPRESS_AT", "40"))
    # 记忆系统 Step 4：三舱记忆整理——LLM 只看新消息产增量，确定性代码管积累（防重写衰减）。
    # 事实/未决舱永不被 LLM 重写，只有机械上限；脉络舱允许衰减（外部真相兜底）+字符上限。
    COACH_FACTS_MAX: int = int(os.getenv("COACH_FACTS_MAX", "20"))  # 事实舱上限（超限丢最旧）
    COACH_OPEN_MAX: int = int(os.getenv("COACH_OPEN_MAX", "8"))  # 未决舱上限（超限丢最旧）
    COACH_SUMMARY_MAX_CHARS: int = int(os.getenv("COACH_SUMMARY_MAX_CHARS", "600"))  # 脉络舱机械上限

    # 工具调用通道失败时的回退开关：true → 去掉 tools 定义用纯文本再问一次（降级可用性）
    ROUTE_FALLBACK_TO_TEXT: bool = os.getenv("ROUTE_FALLBACK_TO_TEXT", "true").lower() == "true"
    # 记忆系统 Step 1：coach 对话确定性写触发——自上次沉淀以来累计用户回合数 / 字符数
    # 任一达到即把这段对话自动喂给 note 管道沉淀。
    ROUTE_MEMORY_SWEEP_TURNS: int = int(os.getenv("ROUTE_MEMORY_SWEEP_TURNS", "6"))
    ROUTE_MEMORY_SWEEP_CHARS: int = int(os.getenv("ROUTE_MEMORY_SWEEP_CHARS", "2500"))
    # 并行沉淀（v2）：true → 后台 daemon 线程跑纯管道（只读+LLM），结果经进程内内存侧信道，
    # 下一用户回合排水落库，沉淀耗时不再阻塞对话；false → 退回 v1 同步路径（逃生舱）。
    ROUTE_MEMORY_SWEEP_ASYNC: bool = os.getenv("ROUTE_MEMORY_SWEEP_ASYNC", "true").lower() == "true"
    # 后台沉淀线程超时（秒）：note 提取可能耗时几十秒，阈值取远高于正常耗时；
    # 超过仍未出结果（线程死/进程重启）→ 把 inflight 快照并回 buffer，交给未来正常 fire 重扫
    ROUTE_MEMORY_SWEEP_TIMEOUT: float = float(os.getenv("ROUTE_MEMORY_SWEEP_TIMEOUT", "300"))
    # 记忆系统 Step 2：coach 提问确定性读路由——提问先查库，命中相似度达标才注入上下文。
    # 检索复用 qa 的混合检索（QA_TOP_K 召回 / QA_SNIPPET_CHARS 截断），此处只控制闸门。
    # 注入阈值（可标定余弦，见 route.py::_hit_relevance）：hybrid 的归一化 similarity（top 恒 1.0）
    # 不能当绝对门槛，run_kb_retrieve 用 dense 原始余弦过闸。标定依据 scripts/calibrate_inject_threshold.py：
    # 正样本（coach 应注入）余弦 0.58~0.87，负样本（笔记里没有）top-1 余弦 0.45~0.62；0.65 时注入召回 90%、
    # 误注入 0/6（0.5 时误注入 3/6——22 篇语料里 0.5 过松）。
    ROUTE_KB_INJECT_SIM: float = float(os.getenv("ROUTE_KB_INJECT_SIM", "0.65"))
    ROUTE_KB_SNIPPETS: int = int(os.getenv("ROUTE_KB_SNIPPETS", "3"))  # 注入片段数上限
    # 里程碑验收闸门：勾选里程碑时核对「完成证据是否真实出现在对话里」（双层：引用 substring
    # 确定性校验 + LLM 验收语义覆盖）。提示词自觉被证伪（Agent 未完成待办即宣布完成），必须代码强制。
    ROUTE_MILESTONE_VERIFY: bool = os.getenv("ROUTE_MILESTONE_VERIFY", "true").lower() == "true"
    # 验收时送审的对话记录尾部字符上限（conversation 无界，截最近部分控成本）
    ROUTE_VERIFY_TRANSCRIPT_CHARS: int = int(os.getenv("ROUTE_VERIFY_TRANSCRIPT_CHARS", "6000"))

    # 用户画像 + 学习路线持久化目录（Markdown 是源，JSON 只存机器态）
    LEARNER_DIR: Path = BASE_DIR / "learner"
    ROADMAP_DIR: Path = BASE_DIR / "roadmaps"

    # Note 模块（Step 3 差量提取）：召回已有笔记作上下文的预算参数
    NOTE_RECALL_TOP_K: int = int(os.getenv("NOTE_RECALL_TOP_K", "3"))  # 召回该 tech 已有笔记 top-k 作差量上下文
    NOTE_CONTEXT_LIMIT: int = int(os.getenv("NOTE_CONTEXT_LIMIT", "500"))  # 每条已有笔记在提取提示词里的截断字数

    # QA 模块（Step 4 联想检索）：检索与提示词预算参数
    QA_TOP_K: int = int(os.getenv("QA_TOP_K", "8"))  # 召回笔记片段条数
    QA_MAX_GROUPS: int = int(os.getenv("QA_MAX_GROUPS", "5"))  # 最多按来源笔记分组数
    QA_SNIPPETS_PER_NOTE: int = int(os.getenv("QA_SNIPPETS_PER_NOTE", "3"))  # 每组最多片段数
    QA_SNIPPET_CHARS: int = int(os.getenv("QA_SNIPPET_CHARS", "500"))  # 每条片段截断字数
    QA_HISTORY_ROUNDS: int = int(os.getenv("QA_HISTORY_ROUNDS", "3"))  # 多轮上下文取最近 N 轮

    # P1 混合检索（BM25 + RRF）：/ask 召回改走 hybrid_search_knowledge，可关回纯 dense
    QA_USE_HYBRID: bool = os.getenv("QA_USE_HYBRID", "true").lower() == "true"
    QA_RRF_K: int = int(os.getenv("QA_RRF_K", "60"))  # RRF 融合常数（名次倒数分母）
    # P1.1 词法一致性软重排：RRF 融合后按「查询词在块中的覆盖率（mini-idf 加权）」加分。
    # 仅当 BM25 正命中 ≤ QA_RERANK_MIN_HITS 篇笔记（罕见词型查询，dense 对专有名词
    # 零词法重合噪声的失败场景）时启用；概念查询 BM25 命中散落，不重排避免误伤语义排序
    # （实测分界：命中≤3 篇只改进/持平，≥4 篇会回退）。只加分不减分；w=0 等价纯 RRF。
    QA_RERANK_LEXICAL: bool = os.getenv("QA_RERANK_LEXICAL", "true").lower() == "true"
    QA_RERANK_LEXICAL_W: float = float(os.getenv("QA_RERANK_LEXICAL_W", "0.5"))
    QA_RERANK_MIN_HITS: int = int(os.getenv("QA_RERANK_MIN_HITS", "3"))

    # Step 5 Part B 质量筛选（screen_results 预筛阈值与名单，全进 config 不进代码）
    QUALITY_DOMAIN_BONUS_OFFICIAL: int = int(os.getenv("QUALITY_DOMAIN_BONUS_OFFICIAL", "20"))
    QUALITY_DOMAIN_BONUS_PLATFORM: int = int(os.getenv("QUALITY_DOMAIN_BONUS_PLATFORM", "10"))
    QUALITY_URL_BONUS_OFFICIAL_DOCS: int = int(os.getenv("QUALITY_URL_BONUS_OFFICIAL_DOCS", "10"))
    QUALITY_URL_PENALTY_BLOG: int = int(os.getenv("QUALITY_URL_PENALTY_BLOG", "-5"))
    QUALITY_URL_PENALTY_SOURCE: int = int(os.getenv("QUALITY_URL_PENALTY_SOURCE", "-5"))
    QUALITY_MIN_SCORE: int = int(os.getenv("QUALITY_MIN_SCORE", "0"))
    # GitHub 星数四档加分：[(最小星数, 加分)] 降序判定（≥10000 +30 / ≥1000 +20 / ≥100 +10 / ≥0 +5）
    QUALITY_STAR_TIERS: tuple = ((10000, 30), (1000, 20), (100, 10), (0, 5))
    # 域名白名单：官方/权威 +20；高质平台/社区 +10；github.com 不走域名加分、走星数加分
    QUALITY_OFFICIAL_DOMAINS: tuple = (
        "python.org", "nodejs.org", "react.dev", "spring.io", "fastapi.tiangolo.com",
        "kubernetes.io", "docker.com", "developer.mozilla.org", "golang.org",
        "rust-lang.org", "microsoft.com", "oracle.com", "docs.djangoproject.com",
    )
    QUALITY_PLATFORM_DOMAINS: tuple = (
        "github.com", "stackoverflow.com", "stackexchange.com", "juejin.cn", "zhihu.com",
    )
    QUALITY_CONTENT_FARMS: tuple = ()  # 内容农场名单，默认空（不误伤），按实际搜索结果补充

    # 搜索配置
    MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", "10"))
    MAX_FETCH_PAGES: int = int(os.getenv("MAX_FETCH_PAGES", "5"))

    # 抓取内容长度限制（字符数）
    MAX_FETCH_CHARS: int = int(os.getenv("MAX_FETCH_CHARS", "16000"))
    # 抓取并发与超时（Stage 4 benchmark 发现：5 次顺序抓取是 collect 耗时主因，改并发 + 超时）
    FETCH_MAX_WORKERS: int = int(os.getenv("FETCH_MAX_WORKERS", "5"))  # 并发抓取线程数上限
    FETCH_TIMEOUT_SECONDS: float = float(os.getenv("FETCH_TIMEOUT_SECONDS", "45"))  # 单次抓取超时上限（秒）

    # Web 服务（WEB_PLAN.md §4-⑥）：默认只绑 127.0.0.1（个人工具不进局域网、不暴露公网）
    WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8000"))

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
        for d in [cls.MATERIALS_DIR, cls.REPORTS_DIR, cls.KNOWLEDGE_DIR,
                  cls.LEARNER_DIR, cls.ROADMAP_DIR]:
            d.mkdir(parents=True, exist_ok=True)


config = Config()