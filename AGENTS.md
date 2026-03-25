# Repository Guidelines

## Project Structure & Module Organization
仓库以根目录脚本为入口。`checkin.py` 负责签到主流程、WAF 处理与余额变更逻辑；`utils/` 放置配置解析与通知能力；`tests/` 存放 `pytest` 用例；`assets/` 保存 README 用到的截图资源；`index.html` 用于展示说明页面；`.github/workflows/checkin.yml` 定义 GitHub Actions 定时任务。新增模块优先放入 `utils/`，避免继续膨胀根目录脚本。

## Build, Test, and Development Commands
使用 `uv` 管理环境与依赖：

- `uv sync --dev`：安装运行与开发依赖。
- `uv run playwright install chromium`：安装 Playwright 浏览器。
- `uv run checkin.py`：本地执行签到脚本。
- `uv run pytest tests/`：运行测试。
- `uv run pytest --cov=. tests/`：查看覆盖率。
- `uv run ruff check .`：执行静态检查。
- `uv run ruff format .`：按仓库规则格式化代码。
- `uv run pre-commit run --all-files`：执行提交前检查。

## Coding Style & Naming Conventions
目标 Python 版本为 3.11。Ruff 已配置 `line-length = 120`、单引号、Tab 缩进；提交前保持格式一致。函数、变量、模块名使用 `snake_case`，数据对象沿用 `ProviderConfig`、`AccountConfig` 这类明确后缀。遵循 KISS/DRY：优先复用现有配置和通知封装，不要为未确定需求预留抽象。

## Testing Guidelines
测试框架为 `pytest`，测试文件命名为 `tests/test_*.py`。优先编写纯单元测试，使用 `patch`/`MagicMock` 隔离网络与外部服务。真实通知测试依赖环境变量 `ENABLE_REAL_TEST=true`，默认应保持跳过，避免在 CI 或本地误发消息。

## Commit & Pull Request Guidelines
近期提交同时存在 `feat: ...` 与 `Update checkin.yml` 风格。建议统一为简短祈使句，优先使用 Conventional Commits，例如 `feat: add custom provider validation`、`fix: handle missing waf cookies`。PR 应包含变更说明、验证命令、关联 Issue；若修改工作流、Secrets 或通知行为，需明确列出影响的环境变量；若调整 `index.html` 或截图资源，附上前后对比图。

## Security & Configuration Tips
不要提交真实 Cookie、Webhook、邮箱口令或 `.env` 文件。参考 `.env.example` 补齐本地配置，生产环境通过 GitHub `production` Environment 注入 `ANYROUTER_ACCOUNTS`、`PROVIDERS` 等 Secrets。涉及 provider 或通知渠道变更时，同时更新 README 与工作流说明，避免文档与行为脱节。
