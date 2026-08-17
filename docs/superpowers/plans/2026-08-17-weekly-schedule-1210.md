# 周报 12:10 定时实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将周一周报触发调整为 12:10、12:30、13:00、13:30，并保持周二至周日 10:00 静默采集。

**架构：** Docker Cron 负责精确唤醒进程，时间线负责判定周报执行窗口。所有部署入口、示例和文档使用同一时间表，避免只改一层造成任务被调度器拒绝。

**技术栈：** Bash/Supercronic、YAML、Python unittest、Docker Compose。

---

### 任务 1：锁定新时间契约

**文件：**
- 修改：`tests/test_weekly_configuration.py`
- 修改：`tests/test_portable_deployment.sh`

- [ ] **步骤 1：编写失败的测试**

将周一时间线期望改为 `start: "12:10"`、`end: "13:31"`，并断言默认 Cron 为：

```text
0 10 * * 2-7
10 12 * * 1
30 12 * * 1
0 13 * * 1
30 13 * * 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
.venv/bin/python -m unittest tests.test_weekly_configuration -v
bash tests/test_portable_deployment.sh
```

预期：旧 `10:00–12:01` 窗口和旧 Cron 导致断言失败。

### 任务 2：同步生产时间表

**文件：**
- 修改：`docker/entrypoint.sh`
- 修改：`docker/.env`
- 修改：`docker/.env.example`
- 修改：`config/daily.crontab`
- 修改：`config/timeline.yaml`
- 修改：`config/timeline.en.yaml`

- [ ] **步骤 1：实现最少配置改动**

统一设置：

```text
CRON_SCHEDULES="0 10 * * 2-7;10,30 12 * * 1;0,30 13 * * 1"
monday_weekly.start="12:10"
monday_weekly.end="13:31"
```

- [ ] **步骤 2：运行聚焦测试验证通过**

运行：

```bash
.venv/bin/python -m unittest tests.test_weekly_configuration -v
bash tests/test_portable_deployment.sh
```

预期：全部通过。

### 任务 3：同步用户文档并完成验证

**文件：**
- 修改：`README.md`
- 修改：`README-EN.md`
- 修改：`docs/news-push-technical-implementation.md`
- 修改：`docs/project-handover-guide.md`

- [ ] **步骤 1：更新所有活动主线时间说明**

将旧的周一 `10:30—12:00` 和旧 Cron 示例替换为四次新触发，并保持周二至周日 `10:00` 静默采集描述。

- [ ] **步骤 2：运行最终验证**

运行：

```bash
.venv/bin/python -m unittest tests.test_weekly_configuration tests.test_project_handover_guide -v
bash tests/test_portable_deployment.sh
git diff --check
```

预期：测试全部通过，`git diff --check` 无输出。

- [ ] **步骤 3：重启服务并核对生效的 crontab**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose-build.yml up -d --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose-build.yml logs --tail 30 trendradar
```

预期：日志显示五条活动 Cron，分别为周二至周日 10:00，以及周一 12:10、12:30、13:00、13:30。
