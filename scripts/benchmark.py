
#!/usr/bin/env python
"""benchmark：自建 ReAct Loop（agentic） vs 当前 Graph 编排的确定性管道。

回答「为什么核心功能要用确定性管道 + 图编排」：同一批 collect 任务，两种实现各跑
N 次，记录 成功率 / 耗时 / 工具调用数 / LLM 调用数 / token 成本，输出对比表
（Rich 控制台 + Markdown 落盘，默认 docs/benchmark_results.md）。

两侧口径
- **agent 侧**：`baselines/react_agent.py` 冻结的 ReAct Agent，模型自主循环
  （search/fetch → Final Answer），系统提示词叠加本文件里的 AGENT_SYSTEM_PROMPT。
- **graph 侧**：生产路径 `src/graph.py` 的 LangGraph 图，`collect_node` → 确定性管道
  `collect_pipeline`（代码编排搜索/抓取/保存，模型只做一次合成）。

成功判定（两侧同一规则，纯规则、无额外 LLM 调用）：
- 产物 = graph 侧 collect 报告文本 / agent 侧 Final Answer 文本
- 成功 = 产物非空 且 含 >= MIN_LINKS 个 http(s) 链接

插桩（不改动项目源码）
- LLM 调用/token：包裹 openai SDK 的 `chat.completions.create`（两侧共用同一入口），
  累计 `usage.prompt_tokens` / `completion_tokens`。
- 工具调用：agent 侧包裹 `react_agent.TOOL_REGISTRY` 里的函数（execute_tool 唯一入口）；
  graph 侧包裹 `pipelines/collect.py` 的 search/save 与 `adapters/fetch.py` 的 `fetch_tool`
  （并发批 `fetch_many` 内部逐个调它，仍按 URL 计数）。
  （GitHub star 数查询属管道内部质量预筛，不计入"工具调用数"。）

用法：
    python scripts/benchmark.py                     # 默认 2 任务 × 2 次 × 2 侧
    python scripts/benchmark.py --trials 3          # 每任务每侧 3 次
    python scripts/benchmark.py --side graph        # 只跑确定性管道侧（省钱）
    python scripts/benchmark.py --side agent        # 只跑 agentic 侧
    python scripts/benchmark.py --tasks "FastAPI" "Spring Boot 3"
    python scripts/benchmark.py --out docs/benchmark_results.md
    python scripts/benchmark.py --dry-run           # 只校验导入/构建，不发任何请求

⚠️ 会真实调用 LLM / Tavily / Firecrawl，消耗 API 配额与费用；默认量级
（2 任务 × 2 次 × 2 侧 ≈ 8 次 collect）适合一轮低成本验证，大样本再加大 --trials。
"""

import argparse
import io
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# scripts/ 不是包，运行 `python scripts/benchmark.py` 时把项目根目录加进 sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，显式切到 UTF-8（与项目里 Rich 的输出编码一致），避免 emoji 触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

# 成功判定：产物至少含 MIN_LINKS 个链接
MIN_LINKS = 2
URL_RE = re.compile(r"https?://[^\s)\]}>\"']+")


# agentic 侧系统提示词（叠加在冻结的 REACT_SYSTEM_PROMPT 之上，复刻旧 collect 定位）
AGENT_SYSTEM_PROMPT = """你是一个技术学习资料收集助手。给定一个技术主题，你的任务是：
1. 用 search 搜索相关资料；
2. 必要时用 fetch 抓取关键文档页面；
3. 整理成一份 Markdown 学习资料清单（每项含「链接 + 一句简介」），至少包含 2 个真实链接；
4. 最后用 Final Answer 输出这份完整清单。
不要编造链接，没有的信息诚实标注「待补充」。"""

DEFAULT_TASKS = [
    {"id": "collect-fastapi", "tech": "FastAPI"},
    {"id": "collect-springboot3", "tech": "Spring Boot 3"},
]


# ============================================================
# 插桩：LLM 调用/token、工具调用（包裹，不改动项目源码）
# ============================================================


class Recorder:
    """全局记分板：begin()/end() 包住单次运行，统计该次 LLM 调用与工具调用。"""

    def __init__(self):
        self.current = None

    def begin(self, task_id, side):
        self.current = {
            "task": task_id,
            "side": side,
            "llm_calls": [],
            "tool_calls": [],
            "start": time.perf_counter(),
        }

    def end(self) -> dict:
        rec = self.current
        self.current = None
        rec["elapsed"] = time.perf_counter() - rec["start"]
        rec["prompt_tokens"] = sum(c["prompt"] for c in rec["llm_calls"])
        rec["completion_tokens"] = sum(c["completion"] for c in rec["llm_calls"])
        rec["total_tokens"] = rec["prompt_tokens"] + rec["completion_tokens"]
        return rec


_recorder = Recorder()
_PATCHED = {"llm": False, "tools": False}


def _patch_llm_usage():
    """包裹 openai chat.completions.create，累计 usage（两侧共用同一 SDK 方法）。"""
    if _PATCHED["llm"]:
        return
    from openai.resources.chat.completions import Completions

    orig = Completions.create

    def wrapped(self, *args, **kwargs):
        resp = orig(self, *args, **kwargs)
        if _recorder.current is not None:
            u = getattr(resp, "usage", None)
            _recorder.current["llm_calls"].append({
                "prompt": getattr(u, "prompt_tokens", 0) or 0,
                "completion": getattr(u, "completion_tokens", 0) or 0,
            })
        return resp

    Completions.create = wrapped
    _PATCHED["llm"] = True


def _make_tool_wrapper(name: str, func):
    def wrapped(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            ok = isinstance(result, dict) and "error" not in result and bool(result)
            return result
        except Exception:
            ok = False
            raise
        finally:
            if _recorder.current is not None:
                _recorder.current["tool_calls"].append({"tool": name, "ok": ok})

    return wrapped


def _patch_tool_counting():
    """工具调用计数：
    - agent 侧：包裹 react_agent.TOOL_REGISTRY 里的函数（execute_tool 的唯一入口）
    - graph 侧：包裹 pipelines/collect 模块顶层引用的 search/fetch/save 三个工具
    """
    if _PATCHED["tools"]:
        return

    from src.baselines import react_agent
    for name, info in react_agent.TOOL_REGISTRY.items():
        info["function"] = _make_tool_wrapper(name, info["function"])

    # graph 侧（collect_pipeline）：search/save 在 collect 模块顶层引用；fetch 改走并发批
    # （fetch_many 内部逐个调 adapters.fetch.fetch_tool），故在 adapters.fetch.fetch_tool 上计数，
    # 仍按 URL 逐个计入。agent 侧 TOOL_REGISTRY 持原始对象引用，不受此补丁影响（不双重计数）。
    from src.pipelines import collect
    for name in ("search_tool", "save_file_tool"):
        setattr(collect, name, _make_tool_wrapper(name, getattr(collect, name)))
    from src.adapters import fetch as _fetch_mod
    setattr(_fetch_mod, "fetch_tool", _make_tool_wrapper("fetch_tool", _fetch_mod.fetch_tool))

    _PATCHED["tools"] = True


# ============================================================
# 运行器：两侧各跑 N 次
# ============================================================


def _success(artifact: str) -> bool:
    return bool(artifact) and len(URL_RE.findall(artifact)) >= MIN_LINKS


def run_agent_side(task: dict, trials: int) -> list[dict]:
    """agentic 侧：冻结 ReAct Agent，模型自主循环直到 Final Answer。"""
    from src.baselines import react_agent
    from src.baselines.react_agent import Agent

    # 静音基线自带的进度打印（🔧 调用工具...），保持 benchmark 输出干净
    react_agent.console.file = io.StringIO()

    rows = []
    for i in range(trials):
        _recorder.begin(task["id"], "agent")
        try:
            agent = Agent(task_prompt=AGENT_SYSTEM_PROMPT)
            final = agent.run(task["instruction"])
            rec = _recorder.end()
            rec["artifact"] = (final or "").strip()
            rec["success"] = _success(rec["artifact"])
        except Exception as exc:
            rec = _recorder.end()
            rec["artifact"] = ""
            rec["success"] = False
            rec["error"] = str(exc)
        rows.append(rec)
    return rows


def run_graph_side(task: dict, trials: int) -> list[dict]:
    """graph 侧：生产 LangGraph 图（InMemorySaver），collect_node → 确定性管道。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from src.graph import open_graph

    rows = []
    for i in range(trials):
        _recorder.begin(task["id"], "graph")
        thread = f"bench-{task['id']}-{i}"
        try:
            with open_graph(InMemorySaver()) as g:
                res = g.invoke(
                    {"command": "collect", "tech": task["tech"], "focus": None},
                    {"configurable": {"thread_id": thread}},
                )
            rec = _recorder.end()
            rec["artifact"] = (res.get("last_output") or "").strip()
            rec["success"] = _success(rec["artifact"])
        except Exception as exc:
            rec = _recorder.end()
            rec["artifact"] = ""
            rec["success"] = False
            rec["error"] = str(exc)
        rows.append(rec)
    return rows


# ============================================================
# 聚合 + 渲染
# ============================================================


def _stats(values: list[float]) -> dict:
    """均值 / 中位数 / 极差。耗时与 token 受网络 + LLM 抖动污染，只报均值会被长尾带偏。"""
    if not values:
        return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0}
    s = sorted(values)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    return {"mean": sum(values) / n, "median": median, "min": s[0], "max": s[-1]}


def _aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    ok = sum(1 for r in rows if r["success"])
    return {
        "n": n,
        "success_rate": ok / n if n else 0.0,
        "elapsed": _stats([r["elapsed"] for r in rows]),
        "llm_calls": _stats([len(r["llm_calls"]) for r in rows]),
        "tool_calls": _stats([len(r["tool_calls"]) for r in rows]),
        "prompt_tokens": _stats([r["prompt_tokens"] for r in rows]),
        "completion_tokens": _stats([r["completion_tokens"] for r in rows]),
        "total_tokens": _stats([r["total_tokens"] for r in rows]),
    }


def _group(rows: list[dict], key) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return groups


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render_table(console: Console, agg_map: dict, totals: dict) -> None:
    table = Table(
        title="ReAct Loop (agentic) vs Graph 确定性管道 — collect 任务对比",
        header_style="bold magenta",
        expand=True,
    )
    for col in (
        "任务", "实现", "次数", "成功率", "平均耗时(s)", "平均LLM调用",
        "平均工具调用", "平均token(入/出)",
    ):
        table.add_column(col, justify="right" if col not in ("任务", "实现") else "left")

    for (task_id, side), a in sorted(agg_map.items()):
        table.add_row(
            task_id,
            "agent" if side == "agent" else "graph 确定性",
            str(a["n"]),
            _pct(a["success_rate"]),
            f"{a['elapsed']['mean']:.1f}",
            f"{a['llm_calls']['mean']:.1f}",
            f"{a['tool_calls']['mean']:.1f}",
            f"{a['prompt_tokens']['mean']:.0f}/{a['completion_tokens']['mean']:.0f}",
        )

    table.add_section()
    for side, a in sorted(totals.items()):
        table.add_row(
            "合计", "agent" if side == "agent" else "graph 确定性",
            str(a["n"]),
            _pct(a["success_rate"]),
            f"{a['elapsed']['mean']:.1f}",
            f"{a['llm_calls']['mean']:.1f}",
            f"{a['tool_calls']['mean']:.1f}",
            f"{a['prompt_tokens']['mean']:.0f}/{a['completion_tokens']['mean']:.0f}",
        )

    console.print(table)


def _fmt_range(s: dict, nd: int = 1) -> str:
    """中位数 [min~max] 紧凑表示。"""
    return f"{s['median']:.{nd}f} [{s['min']:.0f}~{s['max']:.0f}]"


def render_distribution(console: Console, agg_map: dict, totals: dict) -> None:
    """分布表：中位数 + 极差，反映网络/LLM 抖动（均值会被长尾带偏，故单独给分布）。"""
    table = Table(
        title="分布（中位数 [min~max]）— 网络与 LLM 抖动大，N<5 时只看量级方向",
        header_style="bold cyan",
        expand=True,
    )
    for col in (
        "任务", "实现", "耗时s", "LLM调用", "工具调用", "token总数",
    ):
        table.add_column(col, justify="right" if col not in ("任务", "实现") else "left")

    def _rows(groups, tag):
        for key, a in sorted(groups.items()):
            if isinstance(key, tuple):
                task_id, side = key
            else:
                task_id, side = tag, key
            table.add_row(
                task_id,
                "agent" if side == "agent" else "graph 确定性",
                _fmt_range(a["elapsed"]),
                _fmt_range(a["llm_calls"], nd=0),
                _fmt_range(a["tool_calls"], nd=0),
                _fmt_range(a["total_tokens"], nd=0),
            )

    _rows(agg_map, None)
    table.add_section()
    _rows(totals, "合计")

    console.print(table)


def write_markdown(out_path: Path, agg_map: dict, totals: dict, run_command: str) -> None:
    lines = [
        "# Benchmark 结果：ReAct Loop (agentic) vs Graph 确定性管道",
        "",
        f"> 运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"> 命令：`{run_command}`",
        "",
        "## 成功率判定",
        "",
        "- 产物 = graph 侧 collect 报告文本 / agent 侧 Final Answer 文本",
        f"- 成功 = 产物非空 且 含 >= {MIN_LINKS} 个 http(s) 链接（纯规则，无额外 LLM 调用）",
        "",
        "## 结果",
        "",
        "| 任务 | 实现 | 次数 | 成功率 | 平均耗时(s) | 平均LLM调用 | 平均工具调用 | 平均token(入/出) |",
        "|------|------|-----:|------:|------:|------:|------:|------:|",
    ]
    for (task_id, side), a in sorted(agg_map.items()):
        lines.append(
            f"| {task_id} | {'agent' if side == 'agent' else 'graph 确定性'} "
            f"| {a['n']} | {_pct(a['success_rate'])} "
            f"| {a['elapsed']['mean']:.1f} | {a['llm_calls']['mean']:.1f} "
            f"| {a['tool_calls']['mean']:.1f} "
            f"| {a['prompt_tokens']['mean']:.0f}/{a['completion_tokens']['mean']:.0f} |"
        )
    lines.append("")
    lines.append("| 合计 | 实现 | 次数 | 成功率 | 平均耗时(s) | 平均LLM调用 | 平均工具调用 | 平均token(入/出) |")
    lines.append("|------|------|-----:|------:|------:|------:|------:|------:|")
    for side, a in sorted(totals.items()):
        lines.append(
            f"| 合计 | {'agent' if side == 'agent' else 'graph 确定性'} "
            f"| {a['n']} | {_pct(a['success_rate'])} "
            f"| {a['elapsed']['mean']:.1f} | {a['llm_calls']['mean']:.1f} "
            f"| {a['tool_calls']['mean']:.1f} "
            f"| {a['prompt_tokens']['mean']:.0f}/{a['completion_tokens']['mean']:.0f} |"
        )
    lines.append("")
    lines.append("## 分布（中位数 [min~max]）")
    lines.append("")
    lines.append("耗时与 token 受网络 + LLM 抖动影响，均值易被长尾带偏，附中位数与极差。N<5 时只判断量级方向。")
    lines.append("")
    lines.append("| 任务 | 实现 | 耗时s 中位[min~max] | LLM调用 中位[min~max] | 工具调用 中位[min~max] | token总数 中位[min~max] |")
    lines.append("|------|------|------:|------:|------:|------:|")
    for (task_id, side), a in sorted(agg_map.items()):
        lines.append(
            f"| {task_id} | {'agent' if side == 'agent' else 'graph 确定性'} "
            f"| {_fmt_range(a['elapsed'])} "
            f"| {_fmt_range(a['llm_calls'], nd=0)} "
            f"| {_fmt_range(a['tool_calls'], nd=0)} "
            f"| {_fmt_range(a['total_tokens'], nd=0)} |"
        )
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- agent 侧走 `baselines/react_agent.py` 冻结基线（`AGENT_USE_FUNCTION_CALLING` 依 `.env`，默认 false 文本解析）。")
    lines.append("- graph 侧走生产 `src/graph.py` 的 LangGraph 图（InMemorySaver），`collect_node` → `collect_pipeline`。")
    lines.append("- 工具调用数：graph 侧为 搜索(3) + 抓取(≤5) + 保存(1) 的固定组合；agent 侧由模型自主决定。GitHub star 查询不计入。")
    lines.append("- 两侧共用同一 LLM 入口，token 成本为 `usage` 累计（含循环历史累积）。")
    lines.append("- 成功率是二项分布，N 小时置信区间宽；「graph≈100% vs agent 明显更低」的量级差异 N≥5 即可看出方向。")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# main
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="benchmark: 自建 ReAct Loop (agentic) vs 当前 Graph 编排的确定性管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[-1].split("⚠️")[0].strip(),
    )
    parser.add_argument("--trials", type=int, default=2, help="每任务每侧重复次数（默认 2）")
    parser.add_argument("--side", choices=("agent", "graph", "both"), default="both",
                        help="跑哪一侧（默认 both）")
    parser.add_argument("--tasks", nargs="*", default=None, help="技术名列表（默认 FastAPI / Spring Boot 3）")
    parser.add_argument("--out", default=ROOT / "docs" / "benchmark_results.md",
                        help="Markdown 结果输出路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验导入/构建，不发任何外部请求")
    args = parser.parse_args()

    _patch_llm_usage()
    _patch_tool_counting()

    if args.dry_run:
        from src.baselines.react_agent import Agent
        from src.graph import open_graph
        from langgraph.checkpoint.memory import InMemorySaver
        with open_graph(InMemorySaver()) as g:
            assert g is not None, "graph 构建失败"
        a = Agent(task_prompt="dry-run")
        assert a is not None, "Agent 初始化失败"
        print("✅ dry-run OK：导入 + 图构建 + Agent 初始化通过（未发任何外部请求）")
        return

    tasks = [{"id": f"collect-{t.lower().replace(' ', '-')}", "tech": t} for t in (args.tasks or [])]
    if not tasks:
        tasks = DEFAULT_TASKS
    for t in tasks:
        t["instruction"] = f"为技术「{t['tech']}」收集学习资料，整理成 Markdown 学习资料清单，用 Final Answer 输出。"

    console = Console()
    console.print(
        f"🚀 开始 benchmark：{len(tasks)} 个任务 × {args.trials} 次 × 侧={args.side} "
        f"（真实调用 LLM/Tavily/Firecrawl）"
    )

    rows = []
    for task in tasks:
        if args.side in ("agent", "both"):
            rows.extend(run_agent_side(task, args.trials))
        if args.side in ("graph", "both"):
            rows.extend(run_graph_side(task, args.trials))

    agg_map = _group(rows, lambda r: (r["task"], r["side"]))
    agg_map = {k: _aggregate(v) for k, v in agg_map.items()}
    totals = _group(rows, lambda r: r["side"])
    totals = {k: _aggregate(v) for k, v in totals.items()}

    render_table(console, agg_map, totals)
    render_distribution(console, agg_map, totals)

    run_command = " ".join(["python scripts/benchmark.py"] + sys.argv[1:])
    out_path = Path(args.out)
    write_markdown(out_path, agg_map, totals, run_command)
    console.print(f"\n📄 Markdown 结果已写入：{out_path}")


if __name__ == "__main__":
    main()
