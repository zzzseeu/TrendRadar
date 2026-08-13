# 项目接手与运维指南设计

## 1. 目标

为新的项目维护者提供一份可直接执行的中文交接手册，使其在不了解 TrendRadar 内部实现的情况下，能够完成以下工作：

- 在 Windows + WSL2 + Docker Desktop 环境中部署项目。
- 安全配置 AI、企业微信、代理、Elsevier API 和定时任务。
- 启动服务、确认定时调度、人工强制生成并发送周报。
- 拉取 `main` 分支更新并重新部署。
- 备份关键数据，定位常见运行故障。

目标读者会使用基本命令行和 Docker，但不了解本项目。

## 2. 文档范围

新增主文档 `docs/project-handover-guide.md`，并在 `README.md` 的配置与运行章节增加入口。

文档只描述当前有效主线：

- Windows + WSL2 + Docker Desktop。
- `docker/docker-compose-build.yml` 本地构建部署。
- `docker/.env` 环境变量配置。
- 三模块农业育种 PDF 周报。
- 企业微信 PDF 文件投递。
- 当前定时调度和 `--force-weekly` 完整重跑语义。

文档仅陈述当前部署、配置和运行方式，每个操作只保留一个正式入口。

## 3. 文档入口与组织方式

### 3.1 入口

- 主文档：`docs/project-handover-guide.md`。
- README 入口：在“配置与运行”章节链接到主文档。

### 3.2 阅读路径

文档开头提供两条阅读路径：

- 首次接手：按章节从环境准备执行到首次 PDF 推送验收。
- 日常维护：直接进入项目更新、故障排查和命令速查章节。

每个操作章节统一使用以下结构：

1. 操作目的。
2. 前置条件。
3. 可复制命令。
4. 成功标志。
5. 失败处理。

所有命令默认在 WSL2 的项目根目录执行。

## 4. 文档章节

主文档按以下顺序编写：

1. 项目概览。
2. 接手前准备。
3. 安装运行环境。
4. 下载项目。
5. 配置 `docker/.env`。
6. 配置新闻源、AI 和兴趣规则。
7. 首次构建并启动。
8. 定时采集与周报推送。
9. 人工强制重新推送。
10. 查看日志和运行结果。
11. 更新项目。
12. 备份与恢复。
13. 故障排查。
14. 日常维护清单。
15. 常用命令速查。

## 5. 配置设计

配置章节将变量分成三级，避免接手者面对完整 `.env` 时无法判断优先级。

### 5.1 必填配置

- `AI_ANALYSIS_ENABLED`
- `AI_API_KEY`
- `AI_MODEL`
- `AI_API_BASE`
- `WEWORK_WEBHOOK_URL`
- `RUN_MODE=cron`
- `IMMEDIATE_RUN=false`

### 5.2 推荐配置

- `DOCKER_PROXY_URL=http://host.docker.internal:7892`
- `DOCKER_NO_PROXY`
- `CRON_SCHEDULES`
- `ELSEVIER_API_KEY`
- `ELSEVIER_INST_TOKEN`

### 5.3 按需配置

- Web 服务端口。
- MCP 端口。
- 其他通知渠道。
- S3 兼容远程存储。

文档要求先复制模板：

```bash
cp docker/.env.example docker/.env
```

每个变量说明用途、示例格式、是否必填和验证方法。示例只使用占位符，不写入任何真实密钥。

配置章节必须明确：

- `docker/.env` 含密钥，禁止提交 Git。
- WSL2 中的容器访问 Windows 代理必须使用 `host.docker.internal`，不能使用 `127.0.0.1`。
- AI 接口按 OpenAI 兼容格式配置。
- 多个企业微信 Webhook 使用分号分隔。
- Elsevier 凭据用于尝试获取期刊正文；缺少凭据时内容证据会按网页正文、摘要、标题逐级降级。
- `CRON_SCHEDULES` 负责触发进程，项目内部调度负责决定静默采集或周报推送。
- `IMMEDIATE_RUN=false` 表示容器启动后等待定时计划，不立即执行新闻任务。

## 6. 运行主线

### 6.1 首次构建与启动

唯一部署命令为：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  up -d --build --force-recreate
```

启动后使用 `docker compose ps` 和 `docker compose logs` 验证：

- `trendradar` 与 `trendradar-mcp` 正常运行。
- Web 服务启动。
- `supercronic` 作为定时调度器启动。
- 没有配置文件缺失或 Compose 解析错误。

### 6.2 定时运行

文档说明当前时序：

- 周二至周日北京时间 10:00 静默采集。
- 周一汇总上一完整自然周并生成、推送 PDF。
- 周一后续触发用于普通幂等重试；本周期成功后自动跳过。

### 6.3 人工强制运行

人工命令为：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose-build.yml \
  exec trendradar python -m trendradar --force-weekly
```

文档明确该命令会重新在线采集上一完整自然周、重新分析、重建 PDF，并向全部当前企业微信账号再次发送。执行前不需要删除缓存或重建容器。

## 7. 项目更新

更新流程固定为：

```text
确认没有人工任务运行
→ 备份 docker/.env 和 output
→ 检查 git status
→ 拉取 main
→ 重新构建容器
→ 检查容器和日志
```

拉取命令为：

```bash
git pull --ff-only origin main
```

更新章节必须说明：

- 拉取更新前工作树应保持干净。
- 新闻源、Prompt、兴趣规则和代码修改应提交并推送，不能长期保留为本地未提交修改。
- `docker/.env` 和 `output/` 不随 Git 更新。
- 重建容器不会清空 `output/`。
- 不使用 `docker compose down -v`。
- 不通过手工删除 SQLite 检查点触发重跑。

## 8. 数据、备份与恢复

文档说明以下目录的职责：

- `docker/.env`：密钥与部署变量。
- `config/`：新闻源、AI Prompt、兴趣和调度规则。
- `output/rss/`：RSS 日数据库和首次发现账本。
- `output/news/`：AI 结果、周期执行和投递账本。
- `output/weekly/`：正式周报 HTML 与 PDF。

备份应在停止人工任务后进行，至少包含 `docker/.env` 和完整 `output/`。恢复时先停止服务、恢复文件、检查权限，再启动服务。文档不得给出宽泛删除命令。

## 9. 故障排查范围

故障章节只覆盖当前主线：

- 容器无法启动。
- Windows 代理无法从容器访问。
- 个别新闻源不可访问或解析失败。
- AI 返回空内容或 JSON 校验失败。
- Elsevier 只能获得摘要。
- PDF 生成失败或超过企业微信 20 MB 限制。
- 企业微信上传或发送失败。
- 定时任务没有触发。
- 人工任务被同周运行锁阻止。

每个故障条目给出诊断命令、关键日志、判断标准和安全处理方法。

## 10. 安全要求

- 不在文档中写入真实 API Key、Webhook、Token 或密码。
- 不提交 `docker/.env`、日志、数据库、PDF 和运行缓存。
- 展示日志时对密钥和 Webhook 脱敏。
- 不建议删除 `output/`、数据库或检查点作为常规修复方法。
- 任何清理或恢复操作都先明确目标文件并完成备份。

## 11. 验收标准

文档完成后应满足：

- 新接手者只阅读该文档即可完成首次部署。
- 所有命令与当前 `main` 分支文件名、Compose 服务名和 CLI 参数一致。
- 文档只出现当前有效入口，不包含历史实现说明。
- 密钥示例全部为占位符。
- README 能直接跳转到交接文档。
- Markdown 链接有效，命令块格式统一。
- 文档包含首次接手清单、日常维护清单和命令速查表。
