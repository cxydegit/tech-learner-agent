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
    # 报告类生成（collect 资料报告 / read 解读报告）的独立 token 预算：对话级的 4096 对整篇
    # 报告不够。实测 29 篇资料 + 26 篇解读报告：token 中位数 ~1.8K、p90 ~3.5K、最大 ~5.0K
    # （估算值），超过 4096 的 4 篇全在长尾，而长尾正是被硬截断的那批。
    # ⚠️ 本值必须与 LLM_REPORT_TIMEOUT 联立：最坏耗时 ≈ 本值 ÷ 实测吞吐（184 tok/s 下
    # 8000 token ≈ 43s，对 120s 超时留 2.8× 余量）；换更慢的模型要同时调这两个数，否则就是
    # 「上限抬了、超时没抬」——旧事故的形态。
    REPORT_MAX_TOKENS: int = int(os.getenv("REPORT_MAX_TOKENS", "8000"))
    # 工具调用通道单次请求超时（秒）。SDK 默认 600s（10 分钟），交互式 coach 循环会被挂死。
    # 45s 的依据是实测（deepseek-flash / 腾讯 MaaS）：满 4096 token 输出实测
    # 22.8s（≈180 tok/s），即最坏合法时长 ≈ 23s，45s 留约 2× 余量。
    # ⚠️ 本文件所有超时数字都绑定当时的模型速度，换模型/网关必须重测
    LLM_REQUEST_TIMEOUT: float = float(os.getenv("LLM_REQUEST_TIMEOUT", "45"))
    # 报告生成通道（generate_text）单次请求超时（秒）：输入是数万字符抓取正文、输出是整篇
    # 学习资料，一次性长任务，失败代价高（整轮 collect 白做）。
    LLM_REPORT_TIMEOUT: float = float(os.getenv("LLM_REPORT_TIMEOUT", "120"))
    # 长调用心跳间隔（秒）：报告生成是整条管道耗时占 99% 的一步，而它期间原本没有任何中间信号
    # （用户看到「🧠 LLM 生成...」之后长时间静止）。挂心跳后每隔这么久发一条「仍在进行…（已 Ns）」。
    # 0 表示关闭。
    LLM_HEARTBEAT_SECONDS: float = float(os.getenv("LLM_HEARTBEAT_SECONDS", "30"))
    # 重试预算（工具调用通道）：瞬时错误（连接/超时/429/5xx）最多尝试 LLM_MAX_ATTEMPTS 次，
    # 且全部尝试合计不超过 LLM_RETRY_BUDGET_SECONDS（从首次请求起算，超预算立即降级）。
    # 退避 = LLM_RETRY_BASE_DELAY × 2^第几次 + 抖动。确定性错误（key/模型名/请求体不合法）
    # 不重试——重试一万次也不会有不同结果。整个调用最长 ≈ 预算 + 一次请求超时（降级那次）。
    LLM_MAX_ATTEMPTS: int = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
    LLM_RETRY_BUDGET_SECONDS: float = float(os.getenv("LLM_RETRY_BUDGET_SECONDS", "90"))
    LLM_RETRY_BASE_DELAY: float = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))

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
    # Embedding 后端：OpenAI 兼容 embeddings（默认百炼 text-embedding-v3；端点可异源，见下）
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
    # 独立 Embedding 端点（chat / embedding 异源支持）：
    # 不设 → 回落 OPENAI_*（与旧行为一致，存量 .env 零改动）；
    # 设了 → embedding 走独立 key/端点，解锁「chat=DeepSeek、embedding=OpenAI/百炼」等组合。
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL", "")
    # embeddings 单请求批量上限（默认 10 是百炼限额；OpenAI 官方上限更高（2048），换服务按需调整）
    EMBEDDING_BATCH_SIZE: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
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
    # 索引对账（孤儿分块清理）：磁盘文件删除 / 改名后自动清理 Chroma 残留分块。
    # index_paths 末尾与 /ask 惰性入口都受此开关控制。
    RAG_RECONCILE: bool = os.getenv("RAG_RECONCILE", "true").lower() == "true"
    # /ask 惰性对账节流间隔（秒）：只在此间隔内的首次 /ask 做一次元数据对账（毫秒级），
    # 避免每次提问都扫全库；index_paths 末尾的对账不走节流（写入路径自愈）。
    RAG_RECONCILE_INTERVAL: int = int(os.getenv("RAG_RECONCILE_INTERVAL", "300"))
    # /ask 路径单次对账补缺失的文件数上限：提问时补缺失会触发 embedding 调用，
    # 限量避免拖延迟；写路径（index_paths / 写笔记）不限，缺口随下次写入全量补齐。
    RAG_RECONCILE_BACKFILL_MAX: int = int(os.getenv("RAG_RECONCILE_BACKFILL_MAX", "3"))
    # 孤儿删除安全闸：当磁盘可见 Markdown 文件数低于索引中已跟踪文件数的该比例时，
    # 判定为「磁盘扫描异常 / 目录被切换」而非「文件真被删除」——跳过删孤儿只补缺失，
    # 防止整库分块被误当孤儿清空（knowledge 全库消失事故的根因防线）。0 表示关闭闸门。
    RAG_RECONCILE_MIN_DISK_RATIO: float = float(os.getenv("RAG_RECONCILE_MIN_DISK_RATIO", "0.5"))

    # LangGraph checkpointer 持久化（SqliteSaver，跨会话/跨进程恢复）
    GRAPH_DB_DIR: Path = BASE_DIR / ".graph"
    GRAPH_DB_PATH: Path = GRAPH_DB_DIR / "checkpoints.sqlite"

    # 定制化学习路线（模块 2）：coach agent 循环配置
    # 工具调用护栏：每用户回合最大连续工具调用数（超限强制 interrupt 找用户确认方向，防死循环）
    ROUTE_MAX_TOOL_CALLS_PER_TURN: int = int(os.getenv("ROUTE_MAX_TOOL_CALLS_PER_TURN", "8"))
    # 贵工具（collect / read）单轮上限：这两个工具单次要烧搜索/抓取额度 + 分钟级耗时。
    # 取 2 是依据真实事故形态——模型一轮里要了两个不同主题的 collect（是合法需求，不该禁），
    # 但再往上就只是把最坏回合时长线性拉长（每次约 +3.75 分钟）。超限不硬拒，改为停下来问用户。
    ROUTE_MAX_HEAVY_TOOLS_PER_TURN: int = int(os.getenv("ROUTE_MAX_HEAVY_TOOLS_PER_TURN", "2"))

    # 图级执行硬上限（LangGraph recursion_limit，防 agent 失控打转）
    ROUTE_RECURSION_LIMIT: int = int(os.getenv("ROUTE_RECURSION_LIMIT", "50"))

    # 上下文管理：coach 模型上下文每次只带最近 N 轮。
    # 「轮」= 一条 user 消息 + 其后到下一个 user 之前的全部消息（工具调用往返算在同一轮内）。
    # 切点只落在 user 消息上——否则会把 tool 回执与它的 assistant(tool_calls) 切散，
    # 产生模型直接拒收的非法消息序列。因此实际保留条数随工具调用密度浮动：
    # 实测每轮均值 3.3 条，保留 5 轮 ≈ 16~21 条。
    # 取值同时决定压缩频率：窗口留得越大，攒到 COACH_COMPRESS_AT 越快、压缩越频。
    # 实测（200 轮模拟）保留 5 轮 ≈ 57 次压缩，保留 10 轮 ≈ 174 次。
    COACH_HISTORY_KEEP: int = int(os.getenv("COACH_HISTORY_KEEP", "5"))
    # 消息总数超过此值触发压缩；每次裁剪至少丢掉 (此值 − 保留窗口) 条
    COACH_COMPRESS_AT: int = int(os.getenv("COACH_COMPRESS_AT", "40"))
    # 记忆系统：三舱记忆整理——LLM 只看新消息产增量，确定性代码管积累（防重写衰减）。
    # 事实/未决舱永不被 LLM 重写，只有机械上限；脉络舱允许衰减（外部真相兜底）+字符上限。
    COACH_FACTS_MAX: int = int(os.getenv("COACH_FACTS_MAX", "20"))  # 事实舱上限（超限丢最旧）
    COACH_OPEN_MAX: int = int(os.getenv("COACH_OPEN_MAX", "8"))  # 未决舱上限（超限丢最旧）
    COACH_SUMMARY_MAX_CHARS: int = int(os.getenv("COACH_SUMMARY_MAX_CHARS", "600"))  # 脉络舱机械上限

    # 工具调用通道失败时的回退开关：true → 去掉 tools 定义用纯文本再问一次（降级可用性）
    ROUTE_FALLBACK_TO_TEXT: bool = os.getenv("ROUTE_FALLBACK_TO_TEXT", "true").lower() == "true"
    # 记忆系统：coach 对话确定性写触发——自上次沉淀以来累计用户回合数 / 字符数
    # 任一达到即把这段对话自动喂给 note 管道沉淀。
    ROUTE_MEMORY_SWEEP_TURNS: int = int(os.getenv("ROUTE_MEMORY_SWEEP_TURNS", "6"))
    ROUTE_MEMORY_SWEEP_CHARS: int = int(os.getenv("ROUTE_MEMORY_SWEEP_CHARS", "2500"))
    # 并行沉淀：true → 后台 daemon 线程跑纯管道（只读+LLM），结果经进程内内存侧信道，
    # 下一用户回合排水落库，沉淀耗时不再阻塞对话；false → 退回同步路径（逃生舱）。
    ROUTE_MEMORY_SWEEP_ASYNC: bool = os.getenv("ROUTE_MEMORY_SWEEP_ASYNC", "true").lower() == "true"
    # 后台沉淀线程超时（秒）：note 提取可能耗时几十秒，阈值取远高于正常耗时；
    # 超过仍未出结果（线程死/进程重启）→ 把 inflight 快照并回 buffer，交给未来正常 fire 重扫
    ROUTE_MEMORY_SWEEP_TIMEOUT: float = float(os.getenv("ROUTE_MEMORY_SWEEP_TIMEOUT", "300"))
    # 记忆系统：coach 提问确定性读路由——提问先查库，命中相似度达标才注入上下文。
    # 检索复用 qa 的混合检索（QA_TOP_K 召回 / QA_SNIPPET_CHARS 截断），此处只控制闸门。
    # 注入阈值（可标定余弦，见 route.py::_hit_relevance）：hybrid 的归一化 similarity（top 恒 1.0）
    # 不能当绝对门槛，run_kb_retrieve 用 dense 原始余弦过闸。标定依据（一次性的离线标定实验）：
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

    # Note 模块（差量提取）：召回已有笔记作上下文的预算参数
    NOTE_RECALL_TOP_K: int = int(os.getenv("NOTE_RECALL_TOP_K", "3"))  # 召回该 tech 已有笔记 top-k 作差量上下文
    NOTE_CONTEXT_LIMIT: int = int(os.getenv("NOTE_CONTEXT_LIMIT", "500"))  # 每条已有笔记在提取提示词里的截断字数

    # QA 模块（联想检索）：检索与提示词预算参数
    QA_TOP_K: int = int(os.getenv("QA_TOP_K", "8"))  # 召回笔记片段条数
    QA_MAX_GROUPS: int = int(os.getenv("QA_MAX_GROUPS", "5"))  # 最多按来源笔记分组数
    QA_SNIPPETS_PER_NOTE: int = int(os.getenv("QA_SNIPPETS_PER_NOTE", "3"))  # 每组最多片段数
    QA_SNIPPET_CHARS: int = int(os.getenv("QA_SNIPPET_CHARS", "500"))  # 每条片段截断字数
    QA_HISTORY_ROUNDS: int = int(os.getenv("QA_HISTORY_ROUNDS", "3"))  # 多轮上下文取最近 N 轮

    # 混合检索（BM25 + RRF）：/ask 召回改走 hybrid_search_knowledge，可关回纯 dense
    QA_USE_HYBRID: bool = os.getenv("QA_USE_HYBRID", "true").lower() == "true"
    QA_RRF_K: int = int(os.getenv("QA_RRF_K", "60"))  # RRF 融合常数（名次倒数分母）
    # 词法一致性软重排：RRF 融合后按「查询词在块中的覆盖率（mini-idf 加权）」加分。
    # 仅当 BM25 正命中 ≤ QA_RERANK_MIN_HITS 篇笔记（罕见词型查询，dense 对专有名词
    # 零词法重合噪声的失败场景）时启用；概念查询 BM25 命中散落，不重排避免误伤语义排序
    # （实测分界：命中≤3 篇只改进/持平，≥4 篇会回退）。只加分不减分；w=0 等价纯 RRF。
    QA_RERANK_LEXICAL: bool = os.getenv("QA_RERANK_LEXICAL", "true").lower() == "true"
    QA_RERANK_LEXICAL_W: float = float(os.getenv("QA_RERANK_LEXICAL_W", "0.5"))
    QA_RERANK_MIN_HITS: int = int(os.getenv("QA_RERANK_MIN_HITS", "3"))

    # GitHub 星数查询（质量预筛的加分信号）：并发查询 + 条数上限。
    # 上限是硬要求——预筛的输入是全部去重后的搜索结果（最多 3~4 条 query × 10 条），
    # 逐条串行查最坏几百秒，而星数只是个加分项，不值得拖慢 collect。
    GITHUB_STAR_MAX_LOOKUPS: int = int(os.getenv("GITHUB_STAR_MAX_LOOKUPS", "10"))
    GITHUB_STAR_WORKERS: int = int(os.getenv("GITHUB_STAR_WORKERS", "5"))

    # 质量筛选（screen_results 预筛阈值与名单，全进 config 不进代码）
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
    # 抓取并发与超时（实测：5 次顺序抓取是 collect 耗时主因，改并发 + 超时）
    FETCH_MAX_WORKERS: int = int(os.getenv("FETCH_MAX_WORKERS", "5"))  # 并发抓取线程数上限
    FETCH_TIMEOUT_SECONDS: float = float(os.getenv("FETCH_TIMEOUT_SECONDS", "45"))  # 单次抓取超时上限（秒）

    # Web 服务：默认只绑 127.0.0.1（个人工具不进局域网、不暴露公网）
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