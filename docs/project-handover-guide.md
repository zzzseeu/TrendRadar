# TrendRadar 项目接手与运维指南

本文面向会使用基本命令行和 Docker、但不了解 TrendRadar 的维护者。所有命令默认在 Windows 的 WSL2 终端中执行，项目通过 Docker Desktop 和 Docker Compose 运行。

## 阅读路径

- 首次接手：从“接手前准备”开始，按顺序完成首次 PDF 验收。
- 日常维护：直接查看“更新项目”“故障排查”和“常用命令速查”。

## 1. 项目概览

TrendRadar 按自然周采集农业育种新闻，筛选并生成一个企业微信 PDF 周报。正式周报包含三个模块：

1. 时事动态：水稻相关官方发布、产业消息和在线搜索结果，最多 20 条。
2. 科研进展：期刊、预印本和具有明确论文证据的机构报道，最多 20 条。
3. 全国农业气象周报：官方农业气象回顾、影响、展望和生产建议。

运行主线如下：

```text
在线采集新闻与农业气象
→ 持久化 RSS 和来源状态
→ 筛选上一完整自然周
→ 补全文、分类、AI 严格筛选
→ 新闻去重与模块 Top 20
→ 生成三模块研判和 A4 PDF
→ 企业微信上传并发送 PDF
→ 记录周期与账号投递状态
```

个别新闻源暂时不可访问时，其余可用来源继续参与周报，失败来源在 PDF 中说明。整段统计窗口没有任何可用来源证据时才停止生成。

## 2. 接手前准备

接手者需要获得以下权限或信息：

- GitHub 仓库 `zzzseeu/TrendRadar` 的读取与推送权限。
- OpenAI 兼容 AI 接口的 API Key、模型名和 API Base。
- 企业微信机器人 Webhook。
- Windows 本地代理地址和端口。
- Elsevier API Key；如机构提供访问令牌，同时取得 Institution Token。

真实密钥只能保存在 `docker/.env` 或组织批准的密钥管理系统中。不要通过聊天记录、Issue、Git Commit 或普通文档传递真实密钥。

## 3. 安装运行环境

### 3.1 Windows 组件

在 Windows 中安装并启用：

- WSL2。
- 一个 WSL2 Linux 发行版，例如 Ubuntu。
- Docker Desktop。
- Docker Desktop 的 WSL Integration。

在 Docker Desktop 中打开 WSL Integration，并允许当前 Linux 发行版访问 Docker。

### 3.2 WSL2 检查

在 WSL2 终端执行：

```bash
git --version
docker --version
docker compose version
```

成功标志：三个命令都输出版本号，且没有 `command not found` 或 Docker daemon 连接错误。

如果 `docker` 无法连接 daemon，先确认 Docker Desktop 已启动，并检查当前 WSL2 发行版是否启用了集成。

## 4. 下载项目

在 WSL2 中进入用于保存项目的目录：

```bash
cd /mnt/d/project
git clone https://github.com/zzzseeu/TrendRadar.git
cd TrendRadar
```

确认分支和文件：

```bash
git branch --show-current
git status --short
test -f docker/docker-compose-build.yml
```

成功标志：当前分支为 `main`，工作树没有输出，Compose 文件存在。

## 5. 目录说明

| 路径 | 用途 | 是否包含运行状态 |
|---|---|---|
| `config/` | 新闻源、AI Prompt、兴趣规则和调度规则 | 否，受 Git 管理 |
| `docker/` | Dockerfile、Compose 和环境变量模板 | `docker/.env` 含密钥 |
| `output/rss/` | RSS 日数据库、来源状态和首次发现账本 | 是 |
| `output/news/` | AI 结果、周期执行和投递账本 | 是 |
| `output/pdf/` | 正式周报 HTML 与 PDF | 是 |
| `trendradar/` | Python 业务代码 | 否，受 Git 管理 |
| `tests/` | 自动化测试 | 否，受 Git 管理 |

`output/` 通过目录挂载保存在宿主机。重新构建镜像或容器不会自动清除这些业务数据。

## 6. 配置 `docker/.env`

先从模板创建本地配置：

```bash
cp docker/.env.example docker/.env
chmod 600 docker/.env
```

`docker/.env` 包含密钥，禁止提交 Git。可以执行以下命令确认它没有被 Git 跟踪：

```bash
git check-ignore docker/.env
```

成功标志：命令输出 `docker/.env`。

### 6.1 必填配置

| 变量 | 用途 | 示例格式 | 要求 |
|---|---|---|---|
| `AI_ANALYSIS_ENABLED` | 启用周报 AI 分析 | `true` | 必须为 `true` |
| `AI_API_KEY` | AI 接口鉴权 | `<your-ai-api-key>` | 必填，不得提交 Git |
| `AI_MODEL` | LiteLLM 模型标识 | `openai/<model-name>` | 必须是 `provider/model` 格式 |
| `AI_API_BASE` | OpenAI 兼容接口地址 | `https://<your-api-host>/v1` | 与服务商文档一致 |
| `WEWORK_WEBHOOK_URL` | 企业微信机器人 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<your-key>` | 至少配置 1 个账号 |
| `RUN_MODE` | 容器运行模式 | `cron` | 定时部署使用 `cron` |
| `IMMEDIATE_RUN` | 启动后是否立即执行 | `false` | 正式环境推荐 `false` |

多个企业微信账号使用分号分隔：

```dotenv
WEWORK_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<account-a>;https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<account-b>
```

### 6.2 推荐配置

| 变量 | 用途 | 示例格式 | 要求 |
|---|---|---|---|
| `DOCKER_PROXY_URL` | 容器外网请求的代理回退 | `http://host.docker.internal:7892` | 端口与 Windows 代理一致 |
| `DOCKER_NO_PROXY` | AI 网关与企业微信直连 | `apigw.example.cn,qyapi.weixin.qq.com` | 使用逗号分隔域名 |
| `CRON_SCHEDULES` | 定时进程触发计划 | 见下方示例 | 保持引号和分号 |
| `ELSEVIER_API_KEY` | Elsevier 全文 API | `<your-elsevier-api-key>` | 推荐配置 |
| `ELSEVIER_INST_TOKEN` | Elsevier 机构访问令牌 | `<your-elsevier-inst-token>` | 机构提供时配置 |

Windows 代理监听在 `127.0.0.1:7892` 时，容器不能使用这个回环地址访问宿主机。`DOCKER_PROXY_URL` 必须使用 `host.docker.internal`：

```dotenv
DOCKER_PROXY_URL=http://host.docker.internal:7892
```

Elsevier 凭据用于尝试获取期刊正文。接口无法提供正文时，证据会按网页正文、摘要、标题逐级降级，并在 PDF 卡片中标明证据层级。

### 6.3 定时配置

当前定时触发配置为：

```dotenv
CRON_SCHEDULES="0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1"
RUN_MODE=cron
IMMEDIATE_RUN=false
```

`CRON_SCHEDULES` 负责启动任务，`config/timeline.yaml` 决定任务执行静默采集还是周报交付。`IMMEDIATE_RUN=false` 表示容器启动后只启动定时器，不立即执行新闻任务。

### 6.4 完整脱敏示例

```dotenv
WEBSERVER_PORT=8080
DOCKER_PROXY_URL=http://host.docker.internal:7892
DOCKER_NO_PROXY=apigw.example.cn,qyapi.weixin.qq.com

AI_ANALYSIS_ENABLED=true
AI_API_KEY=<your-ai-api-key>
AI_MODEL=openai/<model-name>
AI_API_BASE=https://<your-api-host>/v1

WEWORK_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<your-key>

ELSEVIER_API_KEY=<your-elsevier-api-key>
ELSEVIER_INST_TOKEN=<your-elsevier-inst-token>

CRON_SCHEDULES="0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1"
RUN_MODE=cron
IMMEDIATE_RUN=false
```

## 7. 配置新闻源、AI 和兴趣规则

以下文件受 Git 管理：

| 文件 | 用途 |
|---|---|
| `config/config.yaml` | RSS、网页来源、AI 参数和运行配置 |
| `config/ai_interests.txt` | 关注主题、物种范围和筛选边界 |
| `config/ai_filter/prompt.txt` | 结构化新闻筛选协议 |
| `config/ai_analysis_prompt.txt` | 三模块研判和正文总结要求 |
| `config/timeline.yaml` | 周二至周日采集与周一交付时序 |

新增或修改新闻源时，需要同时确认：

- 地址是来源的官方地址。
- 页面可访问，并能被当前 RSS 或网页解析策略处理。
- `id` 在全部来源中唯一且稳定。
- 日期字段能够解析为明确发布日期。

修改这些文件后执行：

```bash
git status --short
git diff --check
```

确认差异后提交并推送，避免正式服务器长期依赖未提交配置。

## 8. 首次构建并启动

### 8.1 验证 Compose

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  config --quiet
```

成功标志：退出码为 0，没有输出错误。

### 8.2 构建并启动

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  up -d --build --force-recreate
```

该命令构建并启动 `trendradar` 和 `trendradar-mcp`。由于 `IMMEDIATE_RUN=false`，启动后等待定时计划，不会立即推送周报。

### 8.3 检查服务

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  ps

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs --tail=100 trendradar
```

成功标志：

- 两个服务处于运行状态。
- 日志显示 Web 服务启动。
- 日志显示 `supercronic` 启动并作为 PID 1 运行。
- 没有“配置文件缺失”或 Cron 格式错误。

### 8.4 运行体检

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar python -m trendradar --doctor
```

体检失败时先处理配置、依赖或目录权限问题，再执行正式推送。

## 9. 定时采集与周报推送

当前调度规则：

- 周二至周日北京时间 10:00 静默采集，只保存候选与来源状态。
- 周一处理上一完整自然周，并生成、推送三模块 PDF。
- 周一 10:30、11:00、11:30、12:00 是普通幂等重试；本周期已成功时自动跳过。

查看当前调度结果：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar python -m trendradar --show-schedule
```

实时观察定时任务：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs -f trendradar
```

按 `Ctrl+C` 退出日志跟踪不会停止容器。

## 10. 人工强制重新推送

无论当天是星期几，以下命令都处理上一完整自然周：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar python -m trendradar --force-weekly
```

该命令会：

1. 忽略本周期已有分析和推送完成状态。
2. 重新在线采集上一完整自然周发布的候选。
3. 重建权威周快照、AI 研判和 PDF。
4. 向全部当前企业微信账号再次发送。

执行前不需要重建容器、删除缓存或修改 SQLite。周锁仍然有效；如果同一周任务正在运行，等待其结束后再执行。

成功标志：日志显示 PDF 生成、企业微信上传和文件消息发送成功，并记录本周期推送状态。

## 11. 查看日志和运行结果

### 11.1 查看最近日志

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs --tail=200 trendradar
```

### 11.2 查看实时日志

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs -f trendradar
```

### 11.3 查看生成文件

```bash
find output/rss -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort
find output/news -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort
find output/pdf -maxdepth 3 -type f -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort
```

正式 PDF 位于 `output/pdf/<周期结束日期>/`。

展示日志或发送诊断信息前，先检查并遮盖 API Key、Webhook、Token 和邮箱密码。

## 12. 更新项目

### 12.1 更新前检查

确认没有人工任务运行：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  ps

git status --short
git rev-parse HEAD
```

`git status --short` 应没有受 Git 管理文件的修改。新闻源、Prompt、兴趣规则或代码修改必须先提交并推送。

### 12.2 备份

为保证 SQLite 文件一致，先停止两个服务：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  stop trendradar trendradar-mcp
```

将备份保存在项目目录外：

```bash
backup_dir="../TrendRadar-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp docker/.env "$backup_dir/docker.env"
cp -a output "$backup_dir/output"
git rev-parse HEAD > "$backup_dir/git-commit.txt"
echo "$backup_dir"
```

确认备份目录中存在 `docker.env`、`output/` 和 `git-commit.txt`。

### 12.3 拉取并部署

```bash
git pull --ff-only origin main

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  up -d --build --force-recreate
```

更新后检查：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  ps

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs --tail=100 trendradar
```

如果 `docker/.env.example` 新增了变量，将其手工补入 `docker/.env`，不要用模板覆盖真实配置。

### 12.4 更新失败时恢复

停止服务，并使用更新前备份中的明确路径：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  stop trendradar trendradar-mcp

mv output "output.before-restore-$(date +%Y%m%d-%H%M%S)"
cp -a <backup-directory>/output output
cp <backup-directory>/docker.env docker/.env
```

读取 `<backup-directory>/git-commit.txt` 中的提交号，然后临时切换到该提交并重建：

```bash
git switch --detach <previous-commit>

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  up -d --build --force-recreate
```

确认服务恢复后记录故障原因。问题修复后执行 `git switch main` 返回正式分支。

不要使用 `docker compose down -v`。不要手工删除 SQLite 检查点，也不要通过清空 `output/` 解决普通运行问题。

## 13. 故障排查

### 13.1 容器启动失败

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  config --quiet

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  ps -a

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs --tail=200 trendradar
```

重点检查 Compose 解析错误、配置文件缺失、端口占用和 Docker daemon 状态。

### 13.2 容器无法使用 Windows 代理

检查 `docker/.env`：

```dotenv
DOCKER_PROXY_URL=http://host.docker.internal:7892
```

确认 Windows 代理已启动、允许局域网连接，并且监听端口正确。容器不能通过 `127.0.0.1` 访问 Windows 宿主机代理。

如果日志显示“直连返回 HTTP 403，使用代理重试”，表示新闻正文直连被目标站点拒绝，程序正在使用代理回退；只有代理重试也失败时才需要处理代理配置。

### 13.3 个别新闻源不可访问或解析失败

日志会显示来源名称、HTTP 错误或页面结构错误。检查：

- 配置 URL 是否仍是官方发布地址。
- 浏览器能否访问该页面。
- 页面是否更改了列表结构。
- 代理重试是否成功。

个别来源失败不会阻止其他可用来源生成周报。不要把来源状态改成伪成功；应修正 URL 或解析策略。

### 13.4 AI 返回空内容或 JSON 校验失败

检查：

- `AI_MODEL` 是否为 `provider/model` 格式。
- `AI_API_BASE` 是否是正确的 OpenAI 兼容端点。
- API Key 是否有效且有额度。
- `config/config.yaml` 中的 `timeout` 和 `max_tokens` 是否被服务支持。
- 日志是否显示空响应重试、超时或严格 JSON 契约失败。

周报使用严格 JSON 契约。输入 ID 缺失、重复、未知或字段不完整时会拒绝该批结果，不应改成宽松解析。

### 13.5 Elsevier 文献只有摘要

确认凭据已经进入容器：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar sh -lc \
  'test -n "$ELSEVIER_API_KEY" && echo ELSEVIER_API_KEY=SET || echo ELSEVIER_API_KEY=UNSET; test -n "$ELSEVIER_INST_TOKEN" && echo ELSEVIER_INST_TOKEN=SET || echo ELSEVIER_INST_TOKEN=UNSET'
```

该命令只显示 `SET` 或 `UNSET`，不会打印真实密钥。即使凭据已设置，出版商权限也可能只返回元数据或摘要，此时程序按证据层级降级。

### 13.6 PDF 生成失败

检查日志中的 Chromium、`pdfinfo` 和 `pdftotext` 错误。Docker 镜像已包含 PDF 工具。正式 PDF 必须满足：

- 文件头有效。
- A4 页面可解析。
- 能提取中文文本。
- 文件不超过企业微信 20 MB 限制。

生成失败时保留已有正式件，不要手工覆盖半成品。

### 13.7 企业微信发送失败

检查：

- `WEWORK_WEBHOOK_URL` 是否有效。
- 机器人是否仍在目标群中。
- PDF 是否超过 20 MB。
- 企业微信 API 是否能直连。
- 日志中上传和 file 消息哪个阶段失败。

测试通知命令：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar python -m trendradar --test-notification
```

该命令会实际发送测试通知，执行前应告知群内成员。

### 13.8 定时任务没有触发

确认：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar env | grep -E '^(RUN_MODE|CRON_SCHEDULES|TZ)='

docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  logs --tail=200 trendradar
```

`RUN_MODE` 应为 `cron`，时区应为 `Asia/Shanghai`，日志应显示 `supercronic` 已启动。

### 13.9 人工任务被周锁阻止

如果日志显示同一自然周已有任务执行，先确认当前容器内是否仍有任务：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar ps -ef
```

等待正在运行的任务自然结束，再执行 `--force-weekly`。不要删除锁文件绕过并发保护。

## 14. 首次接手验收清单

- [ ] Docker Desktop 已启动并启用当前 WSL2 发行版。
- [ ] 项目已从 `zzzseeu/TrendRadar` 的 `main` 分支克隆。
- [ ] `docker/.env` 已创建、权限已收紧且被 Git 忽略。
- [ ] AI 模型、API Key 和 API Base 已配置。
- [ ] 企业微信 Webhook 已配置。
- [ ] Windows 代理能通过 `host.docker.internal` 访问。
- [ ] Elsevier 凭据已配置，或已接受证据降级。
- [ ] Compose 配置校验通过。
- [ ] `trendradar` 与 `trendradar-mcp` 正常运行。
- [ ] Web 服务和 `supercronic` 启动日志正常。
- [ ] `--doctor` 体检完成。
- [ ] 已执行一次 `--force-weekly` 并收到 PDF。
- [ ] `output/rss/`、`output/news/` 和 `output/pdf/` 产生业务文件。

## 15. 日常维护清单

- [ ] 每周确认企业微信收到 PDF。
- [ ] 查看 PDF 中的来源失败状态。
- [ ] 定期检查容器日志和磁盘空间。
- [ ] 密钥失效时只更新 `docker/.env`，然后重新创建容器。
- [ ] 新闻源、Prompt 和兴趣规则修改后提交并推送 Git。
- [ ] 拉取更新前备份 `docker/.env` 和 `output/`。
- [ ] 确认日志、数据库、PDF 和密钥没有进入 Git。

## 16. 常用命令速查

以下命令均在项目根目录执行。

### 构建并启动

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml up -d --build --force-recreate
```

### 查看服务

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml ps
```

### 查看最近日志

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml logs --tail=200 trendradar
```

### 跟踪实时日志

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml logs -f trendradar
```

### 停止服务

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml stop
```

### 启动已创建的服务

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml start
```

### 重新启动服务

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml restart
```

### 强制重新生成并推送上一自然周

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml exec trendradar python -m trendradar --force-weekly
```

### 查看调度状态

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml exec trendradar python -m trendradar --show-schedule
```

### 进入主容器

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml exec trendradar bash
```

### 更新并重新部署

```bash
git pull --ff-only origin main
docker compose --env-file docker/.env -f docker/docker-compose-build.yml up -d --build --force-recreate
```

### 验证 Compose

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml config --quiet
```
