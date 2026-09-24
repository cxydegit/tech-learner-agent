# RAG 检索评估结果（P0）

> 运行时间：2026-08-15 16:38
> 数据来源：25 条黄金集（含 5 条 hard）的检索评估。评测脚本未随仓库分发（见 [工程记录索引](../README.md)），
> 下表的「期望笔记」列已匿名化——原为个人知识库的文件路径。

## 检索 dense hit-rate@k / MRR@k

- 条目数：25，平均 top_k：7
- hit-rate@7：92%
- MRR@7：0.734
- hard 集（5 条）：hit-rate 60%，MRR 0.262
- 常规（20 条）：hit-rate 100%，MRR 0.852

| 查询 | top_k | hard | 命中 | 位置 | 期望笔记 |
|------|------|:---:|:---:|------:|------|
| LangChain 的 Deep Agents 框架由哪些组件构成？ | 5 |  | ✓ | 1 | 笔记 N |
| RAG 和微调有什么区别？ | 5 |  | ✓ | 1 | 笔记 J |
| RAG 在线推理阶段是怎么处理的？ | 5 |  | ✓ | 1 | 笔记 O |
| 向量数据库 VectorStore 是做什么的？ | 5 |  | ✓ | 1 | 笔记 P |
| 如何防止 RAG 回答出现幻觉？ | 5 |  | ✓ | 5 | 笔记 S |
| 怎么把检索结果并行交给子智能体分析？ | 5 |  | ✓ | 1 | 笔记 T |
| Redis Stack 和核心版有什么差异？ | 5 |  | ✓ | 1 | 笔记 A |
| Redis 的五大核心角色是什么？ | 5 |  | ✓ | 1 | 笔记 Q |
| Windows 上能原生运行 Redis 吗？ | 5 |  | ✓ | 1 | 笔记 L |
| Redis 五种基础数据结构各适合什么场景？ | 5 |  | ✓ | 2 | 笔记 M |
| 为什么不应把对象序列化成 JSON 存进 String？ | 5 |  | ✓ | 1 | 笔记 G |
| Redis 布隆过滤器有什么特点？ | 5 |  | ✓ | 2 | 笔记 B |
| 我之前哪些笔记提过缓存？ | 8 |  | ✓ | 1 | 笔记 Q |
| 哪些笔记讲到了向量搜索？ | 8 |  | ✓ | 1 | 笔记 P |
| 分布式锁可以用哪种 Redis 数据结构实现？ | 8 |  | ✓ | 3 | 笔记 M |
| 笔记里 RAG 实施有哪些挑战？ | 8 |  | ✓ | 1 | 笔记 T |
| 加载切分嵌入存储是哪一步流程？ | 8 |  | ✓ | 1 | 笔记 P |
| 什么情况优先选 RAG 而不是微调？ | 8 |  | ✓ | 1 | 笔记 J |
| RedisJSON 能用来做文档存储吗？ | 8 |  | ✓ | 1 | 笔记 Q |
| 如何评估模型回答是否严格基于检索证据？ | 8 |  | ✓ | 2 | 笔记 S |
| FT.SEARCH | 8 | hard | ✗ | — | 笔记 A |
| RediSearch | 8 | hard | ✗ | — | 笔记 A |
| 缓存穿透 | 8 | hard | ✓ | 7 | 笔记 B |
| RedisJSON | 8 | hard | ✓ | 6 | 笔记 A |
| 分布式锁 | 8 | hard | ✓ | 1 | 笔记 M |

## 检索 hybrid hit-rate@k / MRR@k

- 条目数：25，平均 top_k：7
- hit-rate@7：100%
- MRR@7：0.851
- hard 集（5 条）：hit-rate 100%，MRR 0.607
- 常规（20 条）：hit-rate 100%，MRR 0.912

| 查询 | top_k | hard | 命中 | 位置 | 期望笔记 |
|------|------|:---:|:---:|------:|------|
| LangChain 的 Deep Agents 框架由哪些组件构成？ | 5 |  | ✓ | 1 | 笔记 N |
| RAG 和微调有什么区别？ | 5 |  | ✓ | 1 | 笔记 J |
| RAG 在线推理阶段是怎么处理的？ | 5 |  | ✓ | 1 | 笔记 O |
| 向量数据库 VectorStore 是做什么的？ | 5 |  | ✓ | 1 | 笔记 P |
| 如何防止 RAG 回答出现幻觉？ | 5 |  | ✓ | 2 | 笔记 S |
| 怎么把检索结果并行交给子智能体分析？ | 5 |  | ✓ | 1 | 笔记 T |
| Redis Stack 和核心版有什么差异？ | 5 |  | ✓ | 1 | 笔记 A |
| Redis 的五大核心角色是什么？ | 5 |  | ✓ | 1 | 笔记 Q |
| Windows 上能原生运行 Redis 吗？ | 5 |  | ✓ | 1 | 笔记 L |
| Redis 五种基础数据结构各适合什么场景？ | 5 |  | ✓ | 2 | 笔记 M |
| 为什么不应把对象序列化成 JSON 存进 String？ | 5 |  | ✓ | 1 | 笔记 G |
| Redis 布隆过滤器有什么特点？ | 5 |  | ✓ | 1 | 笔记 B |
| 我之前哪些笔记提过缓存？ | 8 |  | ✓ | 1 | 笔记 Q |
| 哪些笔记讲到了向量搜索？ | 8 |  | ✓ | 1 | 笔记 P |
| 分布式锁可以用哪种 Redis 数据结构实现？ | 8 |  | ✓ | 4 | 笔记 M |
| 笔记里 RAG 实施有哪些挑战？ | 8 |  | ✓ | 1 | 笔记 T |
| 加载切分嵌入存储是哪一步流程？ | 8 |  | ✓ | 1 | 笔记 P |
| 什么情况优先选 RAG 而不是微调？ | 8 |  | ✓ | 1 | 笔记 J |
| RedisJSON 能用来做文档存储吗？ | 8 |  | ✓ | 1 | 笔记 Q |
| 如何评估模型回答是否严格基于检索证据？ | 8 |  | ✓ | 1 | 笔记 S |
| FT.SEARCH | 8 | hard | ✓ | 5 | 笔记 A |
| RediSearch | 8 | hard | ✓ | 1 | 笔记 A |
| 缓存穿透 | 8 | hard | ✓ | 3 | 笔记 B |
| RedisJSON | 8 | hard | ✓ | 2 | 笔记 A |
| 分布式锁 | 8 | hard | ✓ | 1 | 笔记 M |

## note 去重 find_note_match

- 简单测试结果

| tech | topic | 应合并 | 实际合并到 | 相似度 | 判定 |
|------|------|:---:|------|------:|:---:|
| redis | Redis 数据结构选型 | 是 | 笔记 M | 0.83 | 正确 |
| redis | 布隆过滤器和 HyperLogLog 统计 | 是 | 笔记 B | 0.69 | 正确 |
| redis | Redis Stack 的模块组成 | 是 | 笔记 A | 0.71 | 正确 |
| rag | RAG 与微调的区别和选型 | 是 | 笔记 J | 0.84 | 正确 |
| rag | 构建向量索引的标准流程 | 是 | 笔记 P | 0.69 | 正确 |
| redis | 用少量内存统计海量数据 | 是 | 笔记 B | 0.62 | 正确 |
| rag | 为什么传统数据库做不了语义检索 | 是 | 笔记 P | 0.58 | 正确 |
| redis | Redis 数据持久化的实现 | 否 | — | — | 正确 |
| redis | Docker 部署 Redis 集群 | 否 | — | — | 正确 |
| redis | Redis 内存淘汰策略 | 否 | — | — | 正确 |
| rag | LangGraph 状态机编排 | 否 | — | — | 正确 |
| rag | 提示词工程的常见技巧 | 否 | — | — | 正确 |

- **最新测试结果见 [report/note-dedup-report.md](../report/note-dedup-report.md)**

## read 缓存 check_read_cache

- 精度：100%（漏命中 0 / 误命中 0）

| url | 应命中 | 实际 | 相似度 | 判定 |
|------|:---:|:---:|------:|:---:|
| https://fastapi.tiangolo.com/tutorial/first-steps/ | 是 | 命中 | 1.00 | 正确 |
| https://www.databricks.com/blog/what-is-retrieval-augmented-generation | 是 | 命中 | 1.00 | 正确 |
| https://redis.io/docs/latest/develop/data-types/ | 是 | 命中 | 1.00 | 正确 |
| https://docs.python.org/3/library/itertools.html | 否 | 未命中 | — | 正确 |
| https://kubernetes.io/docs/concepts/overview/ | 否 | 未命中 | — | 正确 |
