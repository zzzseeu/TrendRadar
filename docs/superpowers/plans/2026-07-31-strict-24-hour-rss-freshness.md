# RSS 严格 24 小时新鲜度实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 所有 RSS 来源仅允许原始发布时间处于运行时刻前 24 小时内的新闻进入数据库新增结果、AI 筛选、报告和推送。

**架构：** 在 `trendradar.utils.time` 中统一严格时间判断语义；抓取器用它拦截新抓取的无日期、异常日期、未来日期和过期条目；主流程及 AI 管道再次应用相同规则，清理历史数据库遗留条目的影响。`max_age_days <= 0` 仍表示显式关闭过滤。

**技术栈：** Python、`unittest`、Docker Compose、项目容器内 `.venv`

---

## 文件结构

- 修改：`trendradar/utils/time.py`，提供严格的过去时间窗口判断。
- 修改：`trendradar/crawler/rss/fetcher.py`，在条目转换入库前执行来源级时间过滤。
- 修改：`trendradar/__main__.py`，对关键词统计和原始 RSS 展示执行严格过滤。
- 修改：`trendradar/ai/filter_pipeline.py`，对待分析、筛选结果及报告执行严格过滤。
- 修改：`tests/test_sciencedirect_rss_dates.py`，将无日期历史记录测试扩展到普通 RSS。
- 创建：`tests/test_rss_strict_freshness.py`，覆盖严格时间边界、抓取器和主流程转换。

### 任务 1：统一严格时间判断

**文件：**
- 修改：`trendradar/utils/time.py:175-238`
- 创建：`tests/test_rss_strict_freshness.py`

- [ ] **步骤 1：编写失败的时间边界测试**

```python
@patch("trendradar.utils.time.get_configured_time")
def test_strict_window_rejects_missing_invalid_future_and_old(mock_now):
    mock_now.return_value = pytz.timezone("Asia/Shanghai").localize(
        datetime(2026, 7, 31, 15, 0)
    )
    self.assertFalse(is_within_days("", 1, "Asia/Shanghai"))
    self.assertFalse(is_within_days("not-a-date", 1, "Asia/Shanghai"))
    self.assertFalse(is_within_days("2026-07-31T15:01:00+08:00", 1, "Asia/Shanghai"))
    self.assertFalse(is_within_days("2026-07-30T14:59:59+08:00", 1, "Asia/Shanghai"))
    self.assertTrue(is_within_days("2026-07-30T15:00:00+08:00", 1, "Asia/Shanghai"))
```

- [ ] **步骤 2：在容器内运行测试并确认失败**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m unittest tests.test_rss_strict_freshness -v
```

预期：无日期、异常日期或未来日期断言失败，因为现有实现会保留它们。

- [ ] **步骤 3：实现最小严格语义**

```python
if not iso_time:
    return False
if max_days <= 0:
    return True
# 解析失败返回 False
diff_seconds = (now - dt).total_seconds()
return 0 <= diff_seconds <= max_days * 24 * 60 * 60
```

- [ ] **步骤 4：重建测试容器并确认时间测试通过**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m unittest tests.test_rss_strict_freshness -v
```

预期：严格时间测试全部通过。

### 任务 2：将严格规则覆盖全部 RSS 出口

**文件：**
- 修改：`trendradar/crawler/rss/fetcher.py:100-165`
- 修改：`trendradar/__main__.py:1267-1330`
- 修改：`trendradar/ai/filter_pipeline.py:45-95`
- 修改：`tests/test_sciencedirect_rss_dates.py:95-135`
- 修改：`tests/test_rss_strict_freshness.py`

- [ ] **步骤 1：编写失败的全来源回归测试**

```python
def test_other_rss_item_without_date_is_not_pending(self):
    pending = _pipeline()._collect_pending_news("ai_interests.txt")
    self.assertEqual(pending[1], [])

def test_fetcher_rejects_undated_item_when_freshness_enabled(self):
    fetcher = RSSFetcher([], freshness_enabled=True, default_max_age_days=1)
    self.assertFalse(fetcher._is_item_fresh(RSSFeedConfig("x", "X", "https://x"), ""))

def test_raw_conversion_rejects_undated_item_when_freshness_enabled(self):
    result = analyzer._convert_rss_items_to_list(
        {"example-rss": [RSSItem(title="old", feed_id="example-rss", url="https://x")]},
        {"example-rss": "Example"},
    )
    self.assertEqual(result, [])
```

- [ ] **步骤 2：运行目标测试并确认因普通 RSS 仍被保留而失败**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m unittest \
  tests.test_sciencedirect_rss_dates tests.test_rss_strict_freshness -v
```

预期：普通 RSS 无日期条目仍进入待分析或转换结果，测试失败。

- [ ] **步骤 3：实现抓取器和双出口严格过滤**

```python
def _is_item_fresh(self, feed, published_at):
    max_days = (
        feed.max_age_days
        if feed.max_age_days is not None
        else self.default_max_age_days
    )
    if not self.freshness_enabled or max_days <= 0:
        return True
    return is_within_days(published_at, max_days, self.timezone)
```

在抓取器构造 `RSSItem` 前跳过不新鲜条目；在 `_convert_rss_items_to_list` 中不再只对非空日期执行判断；在 `AIFilterPipeline._is_rss_item_fresh` 中删除 ScienceDirect 特例，所有来源统一调用 `is_within_days`。

- [ ] **步骤 4：运行目标测试并确认通过**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m unittest \
  tests.test_sciencedirect_rss_dates tests.test_rss_strict_freshness -v
```

预期：目标测试全部通过。

### 任务 3：完整验证与实际补跑

**文件：**
- 验证：`tests/`
- 验证：`output/html/latest/current.html`

- [ ] **步骤 1：运行完整测试套件**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m unittest discover -s tests -v
```

预期：全部测试通过且失败数为 0。

- [ ] **步骤 2：检查补丁质量**

运行：

```bash
git diff --check
git diff -- trendradar tests
```

预期：`git diff --check` 退出码为 0，无空白错误。

- [ ] **步骤 3：重建容器并立即补跑**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m trendradar
```

预期：任务退出码为 0；无日期、异常日期、未来日期或超过 24 小时的条目不会进入最终结果。

- [ ] **步骤 4：审计最终报告**

读取最新报告和当天 RSS 数据库，逐条确认最终结果的 `published_at` 满足：

```text
0 <= 运行时间 - published_at <= 24 小时
```

预期：若没有合格新闻则不发送推送；若有合格新闻，报告中的每一条均通过严格时间审计。
