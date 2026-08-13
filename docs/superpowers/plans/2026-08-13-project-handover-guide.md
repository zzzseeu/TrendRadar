# 项目接手与运维指南实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 Windows + WSL2 + Docker Desktop 环境编写一份只包含当前正式主线的项目接手与运维指南，并从 README 提供入口。

**架构：** 使用一个独立 Markdown 主文档承载首次接手、配置、运行、更新、备份和排障流程；README 只提供简短入口。新增静态文档契约测试，锁定正式 Compose 文件、关键命令、安全边界和主线术语，防止后续文档与实现漂移。

**技术栈：** Markdown、Python `unittest`、Docker Compose、Git、Bash。

---

## 文件结构

- 创建：`docs/project-handover-guide.md` —— 新维护者唯一的完整接手与运维手册。
- 创建：`tests/test_project_handover_guide.py` —— 校验文档入口、当前命令、必备配置、安全约束和禁止历史入口。
- 修改：`README.md` —— 在“配置与运行”章节增加交接手册链接。

### 任务 1：建立交接文档静态契约

**文件：**
- 创建：`tests/test_project_handover_guide.py`

- [ ] **步骤 1：编写文档入口和主线命令失败测试**

创建以下测试，要求主文档存在、README 可达，并包含当前唯一部署与人工推送命令：

```python
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/project-handover-guide.md"


class ProjectHandoverGuideTests(unittest.TestCase):
    def test_readme_links_to_the_handover_guide(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "[项目接手与运维指南](docs/project-handover-guide.md)",
            readme,
        )

    def test_guide_uses_the_single_current_compose_entrypoint(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn("docker/docker-compose-build.yml", guide)
        self.assertIn("up -d --build --force-recreate", guide)
        self.assertIn(
            "exec trendradar python -m trendradar --force-weekly",
            guide,
        )

    def test_guide_documents_the_current_schedule(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn("周二至周日", guide)
        self.assertIn("周一", guide)
        self.assertIn("上一完整自然周", guide)
        self.assertIn("普通幂等重试", guide)
```

- [ ] **步骤 2：编写配置、安全和排障失败测试**

在同一测试类中增加：

```python
    def test_guide_covers_required_configuration(self):
        guide = GUIDE.read_text(encoding="utf-8")
        for variable in (
            "AI_ANALYSIS_ENABLED",
            "AI_API_KEY",
            "AI_MODEL",
            "AI_API_BASE",
            "WEWORK_WEBHOOK_URL",
            "DOCKER_PROXY_URL",
            "ELSEVIER_API_KEY",
            "ELSEVIER_INST_TOKEN",
            "CRON_SCHEDULES",
            "RUN_MODE",
            "IMMEDIATE_RUN",
        ):
            with self.subTest(variable=variable):
                self.assertIn(variable, guide)

    def test_guide_preserves_secrets_and_business_state(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn("禁止提交 Git", guide)
        self.assertIn("docker/.env", guide)
        self.assertIn("output/", guide)
        self.assertIn("不要使用 `docker compose down -v`", guide)
        self.assertIn("不要手工删除 SQLite 检查点", guide)

    def test_guide_contains_no_removed_compose_entrypoint(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertNotIn("docker/docker-compose.yml", guide)
```

- [ ] **步骤 3：运行测试并确认失败**

运行：

```bash
.venv/bin/python -m unittest tests.test_project_handover_guide -v
```

预期：测试失败，原因是 `docs/project-handover-guide.md` 尚不存在，且 README 尚无入口。

- [ ] **步骤 4：提交失败契约**

```bash
git add tests/test_project_handover_guide.py
git commit -m "test(docs): 定义项目交接指南契约"
```

### 任务 2：编写完整接手与运维指南

**文件：**
- 创建：`docs/project-handover-guide.md`

- [ ] **步骤 1：编写项目概览、环境准备和目录说明**

文档开头必须包含：

```markdown
# TrendRadar 项目接手与运维指南

本文面向会使用基本命令行和 Docker、但不了解 TrendRadar 的维护者。所有命令默认在 Windows 的 WSL2 终端中执行，项目通过 Docker Desktop 和 Docker Compose 运行。

## 阅读路径

- 首次接手：从“接手前准备”开始，按顺序完成首次 PDF 验收。
- 日常维护：直接查看“更新项目”“故障排查”和“常用命令速查”。
```

随后说明项目根目录、`config/`、`docker/`、`output/rss/`、`output/news/` 和 `output/pdf/` 的当前职责。

- [ ] **步骤 2：编写环境安装、克隆和密钥准备**

明确要求 Windows 已安装并启用：

- WSL2。
- Docker Desktop 的 WSL2 Integration。
- WSL2 中的 Git。

克隆示例使用当前项目仓库：

```bash
git clone https://github.com/zzzseeu/TrendRadar.git
cd TrendRadar
cp docker/.env.example docker/.env
```

接手前权限清单只列账号类型，不写真实值：GitHub 仓库权限、AI API、企业微信 Webhook、Windows 代理、Elsevier API。

- [ ] **步骤 3：编写三级配置表和脱敏示例**

按规格列出必填、推荐和按需变量。每个表格包含“变量、用途、示例、要求”四列。示例必须使用以下安全格式：

```dotenv
AI_ANALYSIS_ENABLED=true
AI_API_KEY=<your-ai-api-key>
AI_MODEL=<openai-compatible-model-name>
AI_API_BASE=https://<your-api-host>/v1
WEWORK_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<your-key>
DOCKER_PROXY_URL=http://host.docker.internal:7892
DOCKER_NO_PROXY=apigw.hnaicc.cn,qyapi.weixin.qq.com
ELSEVIER_API_KEY=<your-elsevier-api-key>
ELSEVIER_INST_TOKEN=<your-elsevier-inst-token>
CRON_SCHEDULES="0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1"
RUN_MODE=cron
IMMEDIATE_RUN=false
```

紧接示例写明 `docker/.env` 禁止提交 Git，且容器访问 Windows 代理使用 `host.docker.internal`。

- [ ] **步骤 4：编写新闻源、AI、Prompt 和兴趣配置说明**

只描述当前文件：

- `config/config.yaml`：新闻源、RSS、AI 行为和运行配置。
- `config/ai_interests.txt`：兴趣与筛选边界。
- `config/ai_filter/prompt.txt`：结构化筛选协议。
- `config/ai_analysis_prompt.txt`：三模块研判与正文总结。
- `config/timeline.yaml`：周二至周日静默采集、周一周报。

明确修改这些受 Git 管理的文件后需要提交并推送。

- [ ] **步骤 5：编写首次启动、定时运行和强制推送**

使用统一变量降低命令重复：

```bash
COMPOSE="docker compose --env-file docker/.env -f docker/docker-compose-build.yml"
$COMPOSE config --quiet
$COMPOSE up -d --build --force-recreate
$COMPOSE ps
$COMPOSE logs --tail=100 trendradar
```

说明日志应出现 Web 服务和 `supercronic` 启动信息。人工强制命令为：

```bash
$COMPOSE exec trendradar python -m trendradar --force-weekly
```

明确其重新采集上一完整自然周、重新分析、重建 PDF 并向全部账号再次发送，不要求重建容器或删除缓存。

- [ ] **步骤 6：编写日志、结果、更新和备份流程**

更新命令使用：

```bash
$COMPOSE ps
git status --short
git pull --ff-only origin main
$COMPOSE up -d --build --force-recreate
$COMPOSE logs --tail=100 trendradar
```

备份示例使用显式目录名，禁止宽泛删除：

```bash
backup_dir="../TrendRadar-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp docker/.env "$backup_dir/docker.env"
cp -a output "$backup_dir/output"
```

恢复流程写成“停止服务 → 恢复明确文件 → 检查权限 → 启动服务”，不得使用 `rm -rf`、`git reset --hard` 或 `docker compose down -v`。

- [ ] **步骤 7：编写故障排查、维护清单和命令速查**

故障排查至少覆盖：

- 容器启动失败：`$COMPOSE config --quiet`、`$COMPOSE ps -a`、`$COMPOSE logs --tail=200 trendradar`。
- 代理失败：确认 Windows 代理监听端口、`DOCKER_PROXY_URL` 和 `host.docker.internal`。
- 单个新闻源失败：查看来源名称、HTTP 状态和解析错误；其余来源可继续生成周报。
- AI 空响应或 JSON 校验失败：检查模型名、API Base、额度、超时和日志中的重试信息。
- Elsevier 只有摘要：检查两个 Elsevier 环境变量是否进入容器。
- PDF 或企业微信失败：检查 20 MB 限制、Chromium/Poppler 日志和 Webhook。
- 定时器未触发：检查 `RUN_MODE`、`CRON_SCHEDULES`、`supercronic` 日志和时区。
- 周锁阻止人工任务：确认没有另一个同周任务正在运行，等待其结束后重试。

最后提供首次接手清单、每周维护清单，以及启动、日志、停止、重启、强制推送、调度状态、进入容器、更新和 Compose 校验命令。

- [ ] **步骤 8：运行文档契约并确认主文档相关断言只剩 README 入口失败**

运行：

```bash
.venv/bin/python -m unittest tests.test_project_handover_guide -v
```

预期：主文档内容测试通过，README 入口测试失败。

- [ ] **步骤 9：提交主文档**

```bash
git add docs/project-handover-guide.md
git commit -m "docs: 编写项目接手与运维指南"
```

### 任务 3：接入 README 并验证文档

**文件：**
- 修改：`README.md`

- [ ] **步骤 1：增加 README 入口**

在“配置与运行”章节首段加入：

```markdown
首次部署、密钥配置、定时运行、项目更新和故障排查，请参阅 [项目接手与运维指南](docs/project-handover-guide.md)。
```

- [ ] **步骤 2：运行文档契约测试**

运行：

```bash
.venv/bin/python -m unittest tests.test_project_handover_guide -v
```

预期：全部测试通过，0 个失败。

- [ ] **步骤 3：验证当前部署命令和 CLI 参数**

运行：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml config --quiet
.venv/bin/python -m trendradar --help
bash tests/test_portable_deployment.sh
```

预期：Compose 校验退出码为 0；帮助信息包含 `--force-weekly`；portable 检查输出 `PASS`。

- [ ] **步骤 4：运行静态文档安全检查**

运行：

```bash
rg -n 'sk-[A-Za-z0-9]|webhook/send\?key=[A-Za-z0-9]{8,}|docker/docker-compose\.yml|docker compose down -v|git reset --hard|rm -rf' \
  docs/project-handover-guide.md README.md
```

预期：只允许命中明确的禁止性说明；不得出现真实密钥格式、已移除入口或可直接执行的破坏性命令。

- [ ] **步骤 5：检查 Markdown 链接和差异格式**

运行：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import re

root = Path.cwd()
for source in (root / "README.md", root / "docs/project-handover-guide.md"):
    text = source.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (source.parent / target).resolve()
        if not resolved.exists():
            raise SystemExit(f"broken link: {source}: {target}")
print("PASS: Markdown local links exist")
PY
git diff --check
```

预期：输出 `PASS: Markdown local links exist`，`git diff --check` 无输出。

- [ ] **步骤 6：提交 README 入口**

```bash
git add README.md
git commit -m "docs: 增加项目交接指南入口"
```

- [ ] **步骤 7：最终检查工作树**

运行：

```bash
git status --short
git log -4 --oneline
```

预期：工作树干净；最近提交依次包含文档契约、主文档和 README 入口。
