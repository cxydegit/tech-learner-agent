# 场景检索评估 v1.4：集子扩到 8 个技术领域 + 评测改跑生产路径 + 「名额单位」的发现

> 版本脉络：v1.0 纯评估 → v1.1 分词 + coach 门槛 → v1.2 修正诊断 + 零分填充修复 → v1.3 词法软重排 → **v1.4 集子扩充（56→72 条）+ 评测改跑生产函数 + 逐条快照**。
> 数据说明：评测脚本与黄金集未随仓库分发，下表为实测记录；**「期望笔记」列已匿名化**——原为个人知识库的文件路径。
> 运行时间：2026-10-07 ｜ 检索器：hybrid（= 生产默认）。

## 一句话结论

1. **集子从 4 个技术领域扩到 8 个（56→72 条）后**，总体 hit-rate **97%** / MRR **0.913**。**检索实现本身没动**：56 条共有条目里 **55 条命中与排名一字未动**，唯一变化的 1 条是期望集被修订导致的。
2. 本版真正的新发现是 **「名额」的单位问题**：检索的 `top_k` 限的是**分块条数**，一篇笔记常占 2~5 个名额——`top_k=8` 实测只覆盖 **3~8 篇**不同笔记。这不只是评测口径问题：**`/ask` 与 coach 注入同样按块计数**，8 条分块只覆盖 3 篇时，模型就只看到 3 篇候选。
3. 6 个未命中名额里，**5 个的标注经内容核查确认正确**（属检索侧问题），1 个标宽已删。

## 口径与环境

| 项 | 值 |
|---|---|
| 集子 | 72 条（exact 20 / semantic 15 / cross_note 8 / fuzzy 14 / discriminative 8 / negative 7） |
| 覆盖技术 | 8 个（rag / redis / llm / react / python / springai / springboot / skill-plugin） |
| embedding 模型 | `text-embedding-v3` |
| 分块器版本 | chunker v3 |
| 索引 | 884 个分块 ｜ 知识库 64 篇笔记 |
| 代码版本 | git `4c10249` |
| 注入闸门 | 可标定余弦 ≥ 0.65，取前 3 条 |

## 总览

非负样本 65 条：hit-rate **97%** ｜ MRR **0.913** ｜ recall **94%** ｜ precision@k **14%**

| 场景 | n | hit-rate | MRR | recall | precision@k |
|---|--:|--:|--:|--:|--:|
| exact | 20 | 100% | 0.925 | 100% | 12% |
| semantic | 15 | 100% | 1.000 | 100% | 12% |
| cross_note | 8 | 100% | 1.000 | 79% | 27% |
| fuzzy | 14 | 86% | 0.821 | 86% | 11% |
| discriminative | 8 | 100% | 0.792 | 100% | 12% |
| negative | 7 | — | — | — | 平均 top1 可标定余弦 0.559，过闸 1/7 |

## 逐条明细

| 查询 | 场景 | 结果 | 期望笔记 |
|---|---|:--:|---|
| FT.SEARCH | exact | ✓1 | 笔记 A |
| RediSearch | exact | ✓1 | 笔记 A |
| HyperLogLog | exact | ✓1 | 笔记 B |
| RoPE | exact | ✓1 | 笔记 C |
| FlashAttention | exact | ✓1 | 笔记 D |
| KV Cache | exact | ✓2 | 笔记 D |
| PagedAttention | exact | ✓1 | 笔记 D |
| vLLM | exact | ✓1 | 笔记 D |
| createRoot | exact | ✓1 | 笔记 E |
| Auto-tiering | exact | ✓1 | 笔记 F |
| Context Engine | exact | ✓1 | 笔记 F |
| JSONPath | exact | ✓1 | 笔记 G |
| RLHF | exact | ✓1 | 笔记 H |
| setSearchText | exact | ✓1 | 笔记 I |
| RAG 和微调有什么区别 | semantic | ✓1 | 笔记 J |
| React 列表渲染中 key 起什么作用 | semantic | ✓1 | 笔记 K |
| React 18 有哪些核心变更 | semantic | ✓1 | 笔记 E |
| Transformer 由哪些核心组件构成 | semantic | ✓1 | 笔记 C |
| 大模型训练分哪几个阶段 | semantic | ✓1 | 笔记 H |
| Redis 布隆过滤器适合什么场景 | semantic | ✓1 | 笔记 B |
| Redis 在 Windows 上能跑吗 | semantic | ✓1 | 笔记 L |
| Redis 五种基础数据结构怎么选 | semantic | ✓1 | 笔记 M |
| Deep Agents 框架由哪些组件构成 | semantic | ✓1 | 笔记 N |
| RAG 在线推理阶段做什么 | semantic | ✓1 | 笔记 O |
| 我之前哪些笔记提到过向量数据库 | cross_note | ✓1 | 笔记 P / 笔记 Q / 笔记 F |
| 哪些笔记里出现过 RedisJSON | cross_note | ✓1 | 笔记 A / 笔记 Q |
| 笔记里哪些地方讲了 React 的渲染流程 | cross_note | ✓1 | 笔记 R / 笔记 K / 笔记 E |
| 笔记里提到 Deep Agents 的有哪些 | cross_note | ✓1 | 笔记 N / 笔记 S / 笔记 T |
| 哪些笔记提到上下文窗口 | cross_note | ✓1 | 笔记 T / 笔记 U / 笔记 V |
| 哪些笔记讲了 Redis 数据结构怎么选 | cross_note | ✓1 | 笔记 M / 笔记 G |
| 笔记里哪些地方讲注意力机制 | cross_note | ✓1 | 笔记 C / 笔记 D |
| 哪些笔记提到 Redis 的向量能力 | cross_note | ✓1 | 笔记 A / 笔记 Q / 笔记 F |
| redis 能搜文章内容吗 | fuzzy | ✓1 | 笔记 A |
| react 列表为什么插个元素顺序就乱了 | fuzzy | ✓1 | 笔记 K |
| 对象存 string 是不是不好 | fuzzy | ✓1 | 笔记 G |
| 大模型是怎么训练的 | fuzzy | ✓1 | 笔记 H |
| ai 回复每次都不一样怎么办 | fuzzy | ✗ | 笔记 U |
| redis 那个判断元素在不在的数据结构 | fuzzy | ✓2 | 笔记 B |
| react 从 17 升 18 麻烦吗 | fuzzy | ✓1 | 笔记 E |
| 给 ai 回答打分那个组件叫什么 | fuzzy | ✓1 | 笔记 S |
| transformer 怎么知道每个词的位置 | fuzzy | ✓1 | 笔记 C |
| 什么时候用微调而不是 RAG | fuzzy | ✓1 | 笔记 J |
| RedisJSON 能做什么 | discriminative | ✓3 | 笔记 A |
| RubricMiddleware 是干什么的 | discriminative | ✓1 | 笔记 S |
| Redis 有哪五大角色 | discriminative | ✓1 | 笔记 Q |
| Redis 五种基础数据结构 | discriminative | ✓2 | 笔记 M |
| 向量搜索的原理 | discriminative | ✓1 | 笔记 P |
| Transformer 内部结构 | discriminative | ✓1 | 笔记 C |
| Transformer 有哪几种架构 | discriminative | ✓1 | 笔记 W |
| 注意力机制怎么加速优化 | discriminative | ✓2 | 笔记 D |
| Spring Boot 自动配置原理 | negative | ✗0.60 | （负样本：库里没有） |
| Redis 持久化 AOF 怎么配置 | negative | ✗0.52 | （负样本：库里没有） |
| React 服务端渲染怎么做 | negative | ✗0.66 | （负样本：库里没有） |
| Python 异步协程怎么用 | negative | ✗0.56 | （负样本：库里没有） |
| Docker Compose 怎么编排多容器 | negative | ✗0.48 | （负样本：库里没有） |
| 数据库 B+ 树索引的原理 | negative | ✗0.56 | （负样本：库里没有） |
| f-string | exact | ✓1 | 笔记 X |
| Name Mangling | exact | ✓1 | 笔记 Y |
| Python 里什么样的对象不能当字典的键 | semantic | ✓1 | 笔记 Z |
| python 切片的冒号里写 -1 是什么意思 | fuzzy | ✓1 | 笔记 A1 |
| ChatClient | exact | ✓2 | 笔记 B1 |
| SpringAI 里怎样在不改业务代码的前提下换模型 | semantic | ✓1 | 笔记 C1 |
| springai 想在请求前后插一段自己的逻辑 | fuzzy | ✗ | 笔记 D1 |
| Structured Output | exact | ✓2 | 笔记 E1 |
| @RequestParam | exact | ✓1 | 笔记 F1 |
| Spring Boot 项目的代码和资源分别放哪个目录 | semantic | ✓1 | 笔记 G1 |
| springboot 打完包那个 jar 生成在哪 | fuzzy | ✓1 | 笔记 H1 |
| progressive disclosure | exact | ✓1 | 笔记 V |
| claude code 是怎么决定加载哪个 skill 的 | semantic | ✓1 | 笔记 I1 |
| skill 放在哪些目录才会生效 | semantic | ✓1 | 笔记 J1 |
| skill 和 plugin 到底有啥不一样 | fuzzy | ✓1 | 笔记 K1 |
| Kubernetes 怎么做滚动更新 | negative | ✗0.53 | （负样本：库里没有） |

## 负样本（笔记里没有的问题，越低越好）

| 查询 | top1 可标定余弦 | 是否越过注入闸门 |
|---|--:|:--:|
| Spring Boot 自动配置原理 | 0.601 | 否 |
| Redis 持久化 AOF 怎么配置 | 0.525 | 否 |
| React 服务端渲染怎么做 | 0.664 | **是** |
| Python 异步协程怎么用 | 0.556 | 否 |
| Docker Compose 怎么编排多容器 | 0.480 | 否 |
| 数据库 B+ 树索引的原理 | 0.558 | 否 |
| Kubernetes 怎么做滚动更新 | 0.526 | 否 |

## coach 注入（直接调生产闸门）

| 条目 | 注入召回 | 注入精确 | 正确 / 误注入 |
|--:|--:|--:|---|
| 15 | 93% | 78% | 14 / 4（共注入 18） |

## 未命中的期望名额（6 个）

| 场景 | 查询 | 期望 | 缺的是 |
|---|---|--:|---|
| cross_note | 我之前哪些笔记提到过向量数据库 | 3 | 笔记 Q |
| cross_note | 哪些笔记里出现过 RedisJSON | 2 | 笔记 A |
| cross_note | 哪些笔记提到上下文窗口 | 3 | 笔记 U |
| cross_note | 笔记里哪些地方讲注意力机制 | 2 | 笔记 D |
| fuzzy | ai 回复每次都不一样怎么办 | 1 | 笔记 U |
| fuzzy | springai 想在请求前后插一段自己的逻辑 | 1 | 笔记 D1 |

经**内容核查**（直接读笔记，看它到底有没有讲该主题）逐条定论：**5 个标注正确**（笔记确实讲了，问题在检索侧）、**1 个标宽已删**（该笔记只在括号举例里出现过一次 "Context Window"，没有实质内容）。
其中两条 fuzzy 漏召回属同一类毛病——**词表鸿沟**：问的是口语（"AI 回复每次都不一样怎么办"、"想在请求前后插一段逻辑"），笔记写的是术语（"Temperature=0 的伪确定性"、"Advisors 拦截器管道机制"），dense 与 BM25 双双落空。

## 与 2026-08-26 那份报告的对账

- 只在 **56 条共有的条目**上做（集子变过，总量不可直接比）：**55 条命中与排名一字未动**；唯一变化是「哪些笔记提到上下文窗口」从 ✓2 → ✓1，原因是该条的期望集本次被修订（删掉一条标宽的、补进一条整篇讲该主题的）。
- 结论：**检索行为没有变化**。两次差异来自集子与期望集的修订，不是检索退化。
- 诚实边界：8/26 那次没有留下索引与语料状态的快照，因此那次数字只能作历史记录、无法充当逐条对账的另一半——这正是本次起加入「环境记录 + 逐条快照」的原因。

## 「名额单位」的发现（本版重点）

`top_k` 限的是**分块条数**，而一篇笔记（800 字一块）常占 2~5 个名额。实测 8 条 cross_note 查询：`top_k=8` 平均只覆盖 5 篇不同笔记，最差 3 篇。

| 影响面 | 现在的行为 | 后果 |
|---|---|---|
| cross_note 多篇召回 | 8 个名额可能只装下 3 篇笔记 | 召回均值 79%；3 个未命中名额只差 2~6 个名额 |
| `/ask` 组装上下文 | 先取 8 条分块、再按笔记分组（≤5 组） | 分块只覆盖 3 篇时，模型只看到 3 篇候选 |
| coach 注入 | 取前 3 **条**（不是 3 篇） | 同一篇可能占满 3 条 |
| note 召回管道 | **已按「笔记篇数」计数**（先前的同名修正） | 两者口径不一致 |

改法（确定性、已列入计划）：取更多分块后**先按笔记去重、再截断到 N 篇**；按当前数据预估 cross_note 召回 79% → 约 86%。

## 集子变动（56 → 72）

- 新增 16 条，补齐此前零覆盖的四个技术领域：Python 4 / SpringAI 4 / Spring Boot 3 / Skill-Plugin 4，另加 1 条负样本（Kubernetes 滚动更新，实测 top1 0.526，未过闸）。
- 期望集修订：**删 1 条**（只在括号举例里出现一次 "Context Window"、无实质内容的）、**补 1 条**（整篇在讲"怎么省上下文"的）。
- 新增标注口径：期望笔记必须有**实质内容**（独立小节 / 至少一条要点 / 整篇在讲它）才算「提到」，顺带提及不算——已写进集子元数据，遇到边界不再逐次争论。
- 每条新条目都过了两道校验：期望路径存在 + 关键术语确实出现在该笔记里。

## 已知边界与遗留

1. **React 服务端渲染**这条负样本 top1 可标定余弦 **0.664**，与注入闸门 0.65 只差 0.014（贴边假阳性）；语料变大后负样本整体水位从 0.516 抬到 0.559 → 闸门需要一次重标定。
2. **词表鸿沟**是 fuzzy 场景的系统性弱点（两条漏召回都属于它），dense + BM25 都覆盖不到口语问法。
3. 期望集的**完备性审查**（旧查询是否少标了新笔记）尚未做——10-07 只偶然撞见过一个候选。
4. 评测脚本与黄金集未随仓库分发；自本版起每次运行落「逐条快照 + 环境记录」（模型 / 索引指纹 / 分块器版本 / 语料篇数 / 配置），使后续改动的对比可复跑。

## 关于复跑

```text
（评测脚本未随仓库分发，原命令无法直接执行。当时的评估口径：先 `--check` 零费用校验测试集本身，
再以 hybrid 检索跑全量场景集，逐条比对期望笔记是否落在 top-k 内；每次运行落逐条快照供下次对账。）
```

本版相关改动：`src/adapters/vector.py`（新增 `candidates` 参数：候选池深度与返回条数解耦，供诊断读取真实名次）、
`tests/test_vector_hybrid.py`（5 例）、评测脚本改为直接调用生产 `run_kb_retrieve` / `_hit_relevance` 并记录运行环境。
