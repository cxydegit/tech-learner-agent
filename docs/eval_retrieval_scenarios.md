# RAG 检索评估结果（P0）

> 运行时间：2026-08-26 20:19
> 数据来源：56 条场景查询的检索评估（dense vs hybrid）。评测脚本未随仓库分发（见 [工程记录索引](README.md)），
> 下表的「期望笔记」列已匿名化——原为个人知识库的文件路径。

## 检索 dense hit-rate@k / MRR@k

- 条目数：56，平均 top_k：8
- hit-rate@8：86%
- MRR@8：0.737
- recall@k：83%，precision@k：13%

## 检索 hybrid hit-rate@k / MRR@k

- 条目数：56，平均 top_k：8
- hit-rate@8：98%
- MRR@8：0.920
- recall@k：97%，precision@k：15%

## 各场景 dense vs hybrid

| 场景 | n | dense hit | dense MRR | dense recall | dense prec@k | hybrid hit | hybrid MRR | hybrid recall | hybrid prec@k |
|------|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| cross_note | 8 | 100% | 1.000 | 81% | 27% | 100% | 1.000 | 92% | 30% |
| discriminative | 8 | 100% | 0.792 | 100% | 12% | 100% | 0.750 | 100% | 12% |
| exact | 14 | 57% | 0.333 | 57% | 7% | 100% | 0.964 | 100% | 12% |
| fuzzy | 10 | 90% | 0.783 | 90% | 11% | 90% | 0.850 | 90% | 11% |
| negative | 6 | — | — | — | — | — | — | — | — |
| semantic | 10 | 100% | 1.000 | 100% | 12% | 100% | 1.000 | 100% | 12% |

## 逐条明细

| 查询 | 场景 | top_k | dense | hybrid | 期望笔记 |
|------|------|------:|------:|------:|------|
| FT.SEARCH | exact | 8 | ✗ | ✓1 | 笔记 A |
| RediSearch | exact | 8 | ✗ | ✓1 | 笔记 A |
| HyperLogLog | exact | 8 | ✓3 | ✓1 | 笔记 B |
| RoPE | exact | 8 | ✗ | ✓1 | 笔记 C |
| FlashAttention | exact | 8 | ✗ | ✓1 | 笔记 D |
| KV Cache | exact | 8 | ✗ | ✓2 | 笔记 D |
| PagedAttention | exact | 8 | ✓3 | ✓1 | 笔记 D |
| vLLM | exact | 8 | ✗ | ✓1 | 笔记 D |
| createRoot | exact | 8 | ✓2 | ✓1 | 笔记 E |
| Auto-tiering | exact | 8 | ✓2 | ✓1 | 笔记 F |
| Context Engine | exact | 8 | ✓2 | ✓1 | 笔记 F |
| JSONPath | exact | 8 | ✓2 | ✓1 | 笔记 G |
| RLHF | exact | 8 | ✓1 | ✓1 | 笔记 H |
| setSearchText | exact | 8 | ✓1 | ✓1 | 笔记 I |
| RAG 和微调有什么区别 | semantic | 8 | ✓1 | ✓1 | 笔记 J |
| React 列表渲染中 key 起什么作用 | semantic | 8 | ✓1 | ✓1 | 笔记 K |
| React 18 有哪些核心变更 | semantic | 8 | ✓1 | ✓1 | 笔记 E |
| Transformer 由哪些核心组件构成 | semantic | 8 | ✓1 | ✓1 | 笔记 C |
| 大模型训练分哪几个阶段 | semantic | 8 | ✓1 | ✓1 | 笔记 H |
| Redis 布隆过滤器适合什么场景 | semantic | 8 | ✓1 | ✓1 | 笔记 B |
| Redis 在 Windows 上能跑吗 | semantic | 8 | ✓1 | ✓1 | 笔记 L |
| Redis 五种基础数据结构怎么选 | semantic | 8 | ✓1 | ✓1 | 笔记 M |
| Deep Agents 框架由哪些组件构成 | semantic | 8 | ✓1 | ✓1 | 笔记 N |
| RAG 在线推理阶段做什么 | semantic | 8 | ✓1 | ✓1 | 笔记 O |
| 我之前哪些笔记提到过向量数据库 | cross_note | 8 | ✓1 | ✓1 | 笔记 P / 笔记 Q / 笔记 F |
| 哪些笔记里出现过 RedisJSON | cross_note | 8 | ✓1 | ✓1 | 笔记 A / 笔记 Q |
| 笔记里哪些地方讲了 React 的渲染流程 | cross_note | 8 | ✓1 | ✓1 | 笔记 R / 笔记 K / 笔记 E |
| 笔记里提到 Deep Agents 的有哪些 | cross_note | 8 | ✓1 | ✓1 | 笔记 N / 笔记 S / 笔记 T |
| 哪些笔记提到上下文窗口 | cross_note | 8 | ✓1 | ✓1 | 笔记 T / 笔记 U / 笔记 D |
| 哪些笔记讲了 Redis 数据结构怎么选 | cross_note | 8 | ✓1 | ✓1 | 笔记 M / 笔记 G |
| 笔记里哪些地方讲注意力机制 | cross_note | 8 | ✓1 | ✓1 | 笔记 C / 笔记 D |
| 哪些笔记提到 Redis 的向量能力 | cross_note | 8 | ✓1 | ✓1 | 笔记 A / 笔记 Q / 笔记 F |
| redis 能搜文章内容吗 | fuzzy | 8 | ✓2 | ✓1 | 笔记 A |
| react 列表为什么插个元素顺序就乱了 | fuzzy | 8 | ✓1 | ✓1 | 笔记 K |
| 对象存 string 是不是不好 | fuzzy | 8 | ✓1 | ✓1 | 笔记 G |
| 大模型是怎么训练的 | fuzzy | 8 | ✓1 | ✓1 | 笔记 H |
| ai 回复每次都不一样怎么办 | fuzzy | 8 | ✗ | ✗ | 笔记 U |
| redis 那个判断元素在不在的数据结构 | fuzzy | 8 | ✓3 | ✓2 | 笔记 B |
| react 从 17 升 18 麻烦吗 | fuzzy | 8 | ✓1 | ✓1 | 笔记 E |
| 给 ai 回答打分那个组件叫什么 | fuzzy | 8 | ✓1 | ✓1 | 笔记 S |
| transformer 怎么知道每个词的位置 | fuzzy | 8 | ✓1 | ✓1 | 笔记 C |
| 什么时候用微调而不是 RAG | fuzzy | 8 | ✓1 | ✓1 | 笔记 J |
| RedisJSON 能做什么 | discriminative | 8 | ✓3 | ✓2 | 笔记 A |
| RubricMiddleware 是干什么的 | discriminative | 8 | ✓1 | ✓1 | 笔记 S |
| Redis 有哪五大角色 | discriminative | 8 | ✓1 | ✓1 | 笔记 Q |
| Redis 五种基础数据结构 | discriminative | 8 | ✓2 | ✓2 | 笔记 M |
| 向量搜索的原理 | discriminative | 8 | ✓1 | ✓1 | 笔记 P |
| Transformer 内部结构 | discriminative | 8 | ✓1 | ✓2 | 笔记 C |
| Transformer 有哪几种架构 | discriminative | 8 | ✓1 | ✓1 | 笔记 V |
| 注意力机制怎么加速优化 | discriminative | 8 | ✓2 | ✓2 | 笔记 D |
| Spring Boot 自动配置原理 | negative | 8 | ✗0.48 | ✗1.00 |  |
| Redis 持久化 AOF 怎么配置 | negative | 8 | ✗0.56 | ✗1.00 |  |
| React 服务端渲染怎么做 | negative | 8 | ✗0.68 | ✗1.00 |  |
| Python 异步协程怎么用 | negative | 8 | ✗0.51 | ✗1.00 |  |
| Docker Compose 怎么编排多容器 | negative | 8 | ✗0.54 | ✗1.00 |  |
| 数据库 B+ 树索引的原理 | negative | 8 | ✗0.56 | ✗1.00 |  |

## 负样本假阳性置信度（越低越好，可标定相似度）

| retriever | 负样本数 | 平均 top1 可标定相似度 | 过阈值 假阳性 |
|------|--:|--:|--:|
| dense | 6 | 0.554 | 1 / 6 |
| hybrid | 6 | 0.516 | 0 / 6 |

## coach 注入模拟（tech 限定 + 可标定余弦≥阈值前 3 条）

| retriever | 条目 | 注入召回 | 注入精确 | 正确/误注入 |
|------|--:|--:|--:|--:|
| dense | 10 | 90% | 82% | 9 / 2（共注入 11） |
| hybrid | 10 | 90% | 75% | 9 / 3（共注入 12） |

## note 去重 find_note_match

- 合并精确率：0%，合并召回率：0%
- 误合并率：0%，漏合并率：0%
- 分布：正确合并 0 / 正确不合并 0 / 错合并 0 / 误合并 0 / 漏合并 0

| tech | topic | 应合并 | 实际合并到 | 相似度 | 理由 | 判定 |
|------|------|:---:|------|------:|------|:---:|

## read 缓存 check_read_cache

- 精度：0%（漏命中 0 / 误命中 0）

| url | 应命中 | 实际 | 相似度 | 判定 |
|------|:---:|:---:|------:|:---:|
