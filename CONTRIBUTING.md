# 贡献指南

欢迎！感谢你愿意为 **tech-learner-agent** 贡献力量。项目仍处于早期（v0.1.0），任何形式的贡献——报告 Bug、改进文档、补充测试、提交代码——都非常有价值。

开始前建议先读一遍 [README](README.md)，了解项目定位、架构与基本用法。

- [开发环境搭建](#开发环境搭建)
- [从哪里入手](#从哪里入手)
- [代码规范](#代码规范)
- [测试要求](#测试要求)
- [提交信息规范](#提交信息规范)
- [提 Pull Request](#提-pull-request)
- [报告 Bug 与功能建议](#报告-bug-与功能建议)
- [许可证与贡献授权](#许可证与贡献授权)

## 开发环境搭建

要求：**Python 3.11+**。建议使用虚拟环境。

```bash
git clone https://github.com/cxydegit/tech-learner-agent
cd tech-learner-agent
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # 运行依赖 + 开发工具链（pytest、ruff）
```

两点说明：

- `pip install -e .` 是 **editable 安装**：site-packages 里放的是指向项目目录的链接而非副本，改 `src/` 代码即时生效，无需重装。
- `[dev]` extra（`pyproject.toml` 的 `[project.optional-dependencies].dev`）与 CI 共用同一份依赖声明，本地与 CI 的工具链不会漂移。**仅跑测试/lint 不需要填 `.env`**——测试对 LLM、搜索等外部调用做了 mock。

验证环境：

```bash
tech-learner --help     # 或 python -m src.cli --help
pytest -q               # 全部测试应通过
```

如需实际运行 CLI/Web，复制模板并填入 API Key：

```bash
cp .env.example .env    # 变量说明见 README「配置 API Keys」
```

## 从哪里入手

- 想理解架构：看 README 的架构图，再对照 `src/` 目录（`adapters/` 封装外部服务、`domain/` 领域逻辑、`pipelines/` 编排流程、`web/` Web 界面、`cli.py` 命令行入口）。
- 想改某块行为：先在 issue 里说明你的方案再动手，避免实现方向与预期不符后返工。
- 拿不准的改动：提一个最小可行 PR 讨论，比一次提交大而全的改动更容易被合入。

## 代码规范

项目用 [ruff](https://docs.astral.sh/ruff/) 做 lint，规则集中在根目录 `ruff.toml`（内含逐条取舍注释，改规则前请先读）。提交前必须通过：

```bash
ruff check src tests
```

约定与注意事项：

- **检查范围**是 `src/` 与 `tests/`；`scripts/` 是内部工具脚本，不随仓库分发，不在门禁内。
- 导入排序、过时语法、盲捕获、datetime 时区等由规则自动把关。个别刻意放行的写法用 `# noqa: <规则名>` **就地**标注并说明理由，不全局屏蔽。
- `src/baselines/react_agent.py` 是冻结的 benchmark 基线（方法体零漂移），非必要不要改动。
- 新代码请补全类型注解（参数与返回值）。
- 行宽 120、`target-version = "py311"`：不要使用 Python 3.12+ 才有的语法。
- 依赖一律声明在 `pyproject.toml`（运行依赖放 `[project].dependencies`，工具链放 `[project.optional-dependencies].dev`），**不要新增 requirements.txt**。

## 测试要求

- 框架 pytest，测试文件位于 `tests/`。
- 提交前 `pytest` 必须全绿；新功能请附带测试，修 Bug 请附带能复现该问题的回归用例。
- CI（`.github/workflows/ci.yml`）会在 **Python 3.11 与 3.12** 两个版本上跑全量测试 + `ruff check src tests`，合入前请确保本地与此一致。

## 提交信息规范

采用 Conventional Commits 风格、中文描述，与仓库现有历史保持一致：

```
<type>: <简短中文描述>
```

`type` 取值：`feat` 新功能 / `fix` 修复 / `refactor` 重构 / `docs` 文档 / `test` 测试 / `chore` 杂项（CI、依赖等）/ `perf` 性能。可带可选范围，如 `chore(release): ...`。

示例：

```
feat: route 优化——学习路线可修改
fix: report 不立即进索引
chore: CI 增加 3.11 版本矩阵
```

## 提 Pull Request

1. Fork 仓库，基于最新 `master` 创建分支（如 `fix/xxx`、`feat/xxx`）。
2. 完成改动，本地通过 lint 与测试（见上两节）。
3. 提交信息遵循上文规范；如有对应 issue，在 PR 描述中关联（如 `Closes #12`）。
4. 发起 PR 并按模板填写，等待 CI 通过后进入 review。

## 报告 Bug 与功能建议

- **Bug**：使用 `.github/ISSUE_TEMPLATE/bug_report.md` 模板，尽量包含：环境（OS / Python 版本）、复现步骤、期望行为、实际行为、相关日志。**若涉及 `.env` 内容请先脱敏再粘贴，切勿泄露 API Key。**
- **功能建议**：使用 `.github/ISSUE_TEMPLATE/feature_request.md` 模板，说明使用场景与期望能力，而不是只给结论。

## 许可证与贡献授权

本项目以 **Apache License 2.0** 发布（见 [LICENSE](LICENSE)）。向本项目提交代码或文档，即表示你同意这些内容按项目许可证进行分发。
