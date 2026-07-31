# 临时停用 ScienceDirect RSS 源实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 暂停 4 个 ScienceDirect RSS 源，保留其他来源，并让 Docker 定时服务应用新配置。

**架构：** 只使用现有 RSS 源级 `enabled` 开关，不修改抓取器或删除历史数据。先以容器内项目虚拟环境验证目标状态尚未满足，再修改 `config/config.yaml` 并重复验证；最后重启服务并检查运行时配置和调度状态。

**技术栈：** YAML、TrendRadar RSS 配置、Docker Compose、容器内 `/app/.venv`

---

## 文件结构

- 修改：`config/config.yaml`  
  为 `molecular-plant`、`plant-communications`、`rice-science`、`crop-journal` 增加 `enabled: false`。
- 不创建测试文件  
  本次是纯配置变更，使用容器内项目虚拟环境执行可重复的配置断言。

### 任务 1：禁用 4 个 ScienceDirect RSS 源

**文件：**

- 修改：`config/config.yaml:187-211`

- [ ] **步骤 1：运行配置断言，验证修改前失败**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps \
  --entrypoint /app/.venv/bin/python trendradar -c \
  "import pathlib,yaml; p=pathlib.Path('/app/config/config.yaml'); c=yaml.safe_load(p.read_text(encoding='utf-8')); feeds={f['id']:f for f in c['rss']['feeds']}; ids={'molecular-plant','plant-communications','rice-science','crop-journal'}; assert ids <= feeds.keys(); assert all(feeds[i].get('enabled', True) is False for i in ids), 'ScienceDirect feeds are still enabled'"
```

预期：命令以非零状态退出，错误包含 `ScienceDirect feeds are still enabled`。

- [ ] **步骤 2：添加最小配置变更**

在 4 个目标源的 `url` 后增加相同的开关：

```yaml
    - id: "molecular-plant"
      name: "Molecular Plant"
      url: "https://rss.sciencedirect.com/publication/science/16742052"
      enabled: false                  # 临时停用，等待 Elsevier API 权限确认
      max_items: 30
      max_age_days: 1
```

`plant-communications`、`rice-science`、`crop-journal` 使用相同配置。

- [ ] **步骤 3：运行配置断言，验证修改后通过**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps \
  --entrypoint /app/.venv/bin/python trendradar -c \
  "import pathlib,urllib.parse,yaml; p=pathlib.Path('/app/config/config.yaml'); c=yaml.safe_load(p.read_text(encoding='utf-8')); fs=c['rss']['feeds']; feeds={f['id']:f for f in fs}; ids={'molecular-plant','plant-communications','rice-science','crop-journal'}; assert ids <= feeds.keys(); assert all(feeds[i].get('enabled', True) is False for i in ids); offenders=[f['id'] for f in fs if f.get('enabled', True) and urllib.parse.urlparse(f['url']).hostname=='rss.sciencedirect.com']; assert not offenders, offenders; remaining=[f['id'] for f in fs if f.get('enabled', True)]; assert remaining; print('disabled=',','.join(sorted(ids))); print('enabled_non_sciencedirect=',len(remaining))"
```

预期：命令退出状态为 0，输出列出 4 个禁用源，且 `enabled_non_sciencedirect` 大于 0。

- [ ] **步骤 4：检查变更范围和格式**

运行：

```bash
git diff --check -- config/config.yaml
git diff -- config/config.yaml
```

预期：`git diff --check` 退出状态为 0；差异只包含 4 个 `enabled: false` 配置行。

- [ ] **步骤 5：提交配置变更**

```bash
git add config/config.yaml
git commit -m "chore(监控): 临时停用 ScienceDirect 来源"
```

预期：提交只包含 `config/config.yaml`。

### 任务 2：重启服务并验证运行状态

**文件：**

- 不修改文件

- [ ] **步骤 1：确认重启不会立即推送**

运行：

```bash
rg -n '^IMMEDIATE_RUN=false$' docker/.env
```

预期：匹配到 `IMMEDIATE_RUN=false`。

- [ ] **步骤 2：重启 TrendRadar 服务**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml restart trendradar
```

预期：`trendradar` 服务完成重启。

- [ ] **步骤 3：检查服务状态**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml ps trendradar
```

预期：`trendradar` 状态为 `Up`。

- [ ] **步骤 4：从运行中容器验证生效配置**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -c \
  "import pathlib,urllib.parse,yaml; c=yaml.safe_load(pathlib.Path('/app/config/config.yaml').read_text(encoding='utf-8')); fs=c['rss']['feeds']; disabled=[f['id'] for f in fs if not f.get('enabled', True)]; offenders=[f['id'] for f in fs if f.get('enabled', True) and urllib.parse.urlparse(f['url']).hostname=='rss.sciencedirect.com']; remaining=[f['id'] for f in fs if f.get('enabled', True)]; assert {'molecular-plant','plant-communications','rice-science','crop-journal'} <= set(disabled); assert not offenders, offenders; assert remaining; print('runtime_config_ok'); print('enabled_non_sciencedirect=',len(remaining))"
```

预期：退出状态为 0，输出 `runtime_config_ok`，且启用的非 ScienceDirect 来源数量大于 0。

- [ ] **步骤 5：检查启动日志**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml logs --since 2m trendradar
```

预期：

- 日志包含 Web 服务和 supercronic 启动信息。
- 日志不包含 `立即执行一次`。
- 没有启动失败或配置解析错误。

- [ ] **步骤 6：最终核对工作区**

运行：

```bash
git status --short
git log -2 --oneline
```

预期：最新提交为 ScienceDirect 临时停用；用户原有的 `index.html`、`output/` 和其他未跟踪文件保持不变。
