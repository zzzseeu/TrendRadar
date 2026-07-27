# TrendRadar 项目执行规范

## Python 环境

- 本项目所有 Python 命令、脚本、测试、依赖检查和调试操作，必须使用项目虚拟环境 `TrendRadar/.venv`。
- 在项目根目录优先使用 `.venv/bin/python`、`.venv/bin/pip`，或使用会自动选择该环境的 `uv run`。
- 不得直接调用系统级 `python`、`python3` 或 `pip` 执行本项目任务。
- 安装或同步项目依赖时使用 `uv sync --locked`；不要把依赖安装到系统 Python 环境。
- 执行前如需确认环境，应检查 `sys.executable` 指向项目目录下的 `.venv/bin/python`。

示例：

```bash
.venv/bin/python -m trendradar
.venv/bin/python -m pytest
uv run python -m trendradar
uv sync --locked
```
