# 自然周新闻推送实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 每天 10:00 静默采集新闻，并在每周一 10:00 仅分析和推送上一个自然周内的去重内容。

**架构：** 新增自然周窗口与 RSS 周聚合器，读取上周一至本周一共八个日库，严格按发布时间过滤、跨日去重后把幂等快照写入本周一数据库。主流程在采集前解析调度，静默日采集后直接结束；周一把周窗口和快照条目 ID 传给 AI 筛选、报告与通知链路，防止本周一内容或数据库内重复项进入周报。

**技术栈：** Python 3.12、SQLite、PyYAML、`unittest`、Docker Compose、supercronic

---

## 文件职责

- 创建 `trendradar/core/weekly.py`：自然周窗口计算、八日 RSS 聚合、跨日去重和快照 ID 解析。
- 创建 `tests/test_weekly_digest.py`：自然周边界、八日聚合、缺库、去重和 AI 周范围测试。
- 创建 `tests/test_weekly_schedule.py`：周一/非周一调度、静默日 AI 短路和周报编排测试。
- 创建 `tests/test_weekly_report_output.py`：周报类型与自然周范围的通知、HTML 展示测试。
- 创建 `tests/test_weekly_configuration.py`：运行配置、时间线和示例 Cron 测试。
- 修改 `trendradar/utils/time.py`：抽取可复用 ISO 时间解析函数。
- 修改 `trendradar/ai/filter_pipeline.py`：支持自然周窗口和允许的 RSS 条目 ID。
- 修改 `trendradar/context.py`：把周报范围传给 AI 筛选和结果转换。
- 修改 `trendradar/__main__.py`：前置调度、静默短路、周快照接入和 `weekly` 模式。
- 修改 `trendradar/core/scheduler.py`：补充 `weekly` 模式说明。
- 修改 `trendradar/crawler/news_search.py`：新闻搜索回看窗口改为 48 小时。
- 修改 `tests/test_news_search.py`：验证两个搜索提供商的 48 小时窗口。
- 修改 `config/config.yaml`：启用自定义调度，将当前启用 RSS 源的回看改为 2 天。
- 修改 `config/timeline.yaml`：周一生成周报，其他日期静默采集。
- 修改 `docker/.env.example`：示例 Cron 改为每天 10:00。
- 本机修改 `docker/.env`：实际 Cron 改为每天 10:00；保持 Git 忽略。
- 修改 `trendradar/notification/splitter.py`、`renderer.py`：显示周报类型与周期。
- 修改 `trendradar/report/generator.py`：允许 `period_label` 进入 HTML 报告数据。
- 修改 `trendradar/report/html.py`：HTML 显示周报模式与周期。

### 任务 1：实现自然周时间窗口

**文件：**
- 修改：`trendradar/utils/time.py`
- 创建：`trendradar/core/weekly.py`
- 创建：`tests/test_weekly_digest.py`

- [ ] **步骤 1：编写自然周边界和日期解析失败测试**

```python
import unittest
from datetime import datetime

import pytz

from trendradar.core.weekly import previous_natural_week
from trendradar.utils.time import parse_iso_datetime


class NaturalWeekWindowTests(unittest.TestCase):
    def test_previous_week_is_monday_to_monday_in_shanghai(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )

        self.assertEqual(window.start.isoformat(), "2026-08-03T00:00:00+08:00")
        self.assertEqual(window.end.isoformat(), "2026-08-10T00:00:00+08:00")
        self.assertEqual(window.label, "2026-08-03—2026-08-09")
        self.assertEqual(
            window.storage_dates,
            [
                "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
                "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10",
            ],
        )

    def test_window_is_half_open_across_year_boundary(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2027, 1, 4, 10, 0)),
            "Asia/Shanghai",
        )

        self.assertTrue(window.contains("2026-12-28T00:00:00+08:00"))
        self.assertTrue(window.contains("2027-01-03T23:59:59+08:00"))
        self.assertFalse(window.contains("2027-01-04T00:00:00+08:00"))
        self.assertFalse(window.contains(""))
        self.assertFalse(window.contains("invalid"))

    def test_naive_iso_time_keeps_existing_utc_assumption(self):
        parsed = parse_iso_datetime("2026-08-02T16:00:00", "Asia/Shanghai")
        self.assertEqual(parsed.isoformat(), "2026-08-03T00:00:00+08:00")
```

- [ ] **步骤 2：运行测试并确认接口不存在**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_digest.NaturalWeekWindowTests -v
```

预期：FAIL，缺少 `trendradar.core.weekly` 或 `parse_iso_datetime`。

- [ ] **步骤 3：实现时间解析和自然周值对象**

在 `trendradar/utils/time.py` 新增 `parse_iso_datetime()`；无时区值按现有约定视为 UTC，最后转换到配置时区。让 `is_within_days()` 调用该函数，保持缺失、未来和精确边界行为。

```python
def parse_iso_datetime(iso_time: str, timezone: str = DEFAULT_TIMEZONE):
    if not iso_time:
        return None
    try:
        parsed = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = pytz.UTC.localize(parsed)
    try:
        target_tz = pytz.timezone(timezone)
    except pytz.UnknownTimeZoneError:
        target_tz = pytz.timezone(DEFAULT_TIMEZONE)
    return parsed.astimezone(target_tz)
```

在 `trendradar/core/weekly.py` 新增：

```python
@dataclass(frozen=True)
class NaturalWeekWindow:
    start: datetime
    end: datetime
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d}—{self.end - timedelta(days=1):%Y-%m-%d}"

    @property
    def storage_dates(self) -> list[str]:
        return [
            (self.start + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(8)
        ]

    def contains(self, published_at: str) -> bool:
        parsed = parse_iso_datetime(published_at, self.timezone)
        return parsed is not None and self.start <= parsed < self.end


def previous_natural_week(now: datetime, timezone: str) -> NaturalWeekWindow:
    tz = pytz.timezone(timezone)
    local_now = now.astimezone(tz)
    monday_date = (local_now - timedelta(days=local_now.weekday())).date()
    end = tz.localize(datetime.combine(monday_date, datetime.min.time()))
    return NaturalWeekWindow(end - timedelta(days=7), end, timezone)
```

- [ ] **步骤 4：运行自然周和原新鲜度测试**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest \
  tests.test_weekly_digest.NaturalWeekWindowTests \
  tests.test_rss_strict_freshness.StrictFreshnessTimeTests -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交时间窗口**

```bash
git add trendradar/utils/time.py trendradar/core/weekly.py tests/test_weekly_digest.py
git commit -m "feat(周报): 添加自然周时间窗口"
```

### 任务 2：实现八日 RSS 聚合与幂等快照

**文件：**
- 修改：`trendradar/core/weekly.py`
- 修改：`tests/test_weekly_digest.py`

- [ ] **步骤 1：编写八日读取、边界过滤、去重和缺库测试**

```python
from unittest.mock import MagicMock

from trendradar.core.weekly import WeeklyRSSAggregator
from trendradar.storage.base import RSSData, RSSItem


def rss_data(date, *items):
    grouped = {}
    names = {}
    for item in items:
        grouped.setdefault(item.feed_id, []).append(item)
        names[item.feed_id] = item.feed_name or item.feed_id
    return RSSData(date=date, crawl_time="10-00", items=grouped, id_to_name=names)


class WeeklyRSSAggregatorTests(unittest.TestCase):
    def test_reads_eight_dates_and_uses_half_open_window(self):
        tz = pytz.timezone("Asia/Shanghai")
        storage = MagicMock()
        by_date = {
            "2026-08-03": rss_data("2026-08-03", RSSItem(
                title="Week start", feed_id="journal",
                url="https://example.org/start",
                published_at="2026-08-03T00:00:00+08:00",
            )),
            "2026-08-10": rss_data("2026-08-10",
                RSSItem(title="Sunday night", feed_id="journal",
                        url="https://example.org/sunday",
                        published_at="2026-08-09T23:59:59+08:00"),
                RSSItem(title="This Monday", feed_id="journal",
                        url="https://example.org/monday",
                        published_at="2026-08-10T00:00:00+08:00"),
            ),
        }
        storage.get_rss_data.side_effect = lambda date: by_date.get(date)
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [
            {"id": 11, "source_id": "journal", "title": "Week start",
             "url": "https://example.org/start"},
            {"id": 12, "source_id": "journal", "title": "Sunday night",
             "url": "https://example.org/sunday"},
            {"id": 13, "source_id": "journal", "title": "This Monday",
             "url": "https://example.org/monday"},
        ]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            tz.localize(datetime(2026, 8, 10, 10, 0))
        )

        self.assertEqual(storage.get_rss_data.call_count, 8)
        self.assertEqual(
            [item.title for item in result.iter_items()],
            ["Week start", "Sunday night"],
        )
        self.assertEqual(result.allowed_rss_ids, {11, 12})
        self.assertEqual(result.filtered_out, 1)
        self.assertEqual(len(result.missing_dates), 6)

    def test_canonical_url_dedup_keeps_richer_search_record(self):
        storage = MagicMock()
        first = RSSItem(
            title="Breeding result", feed_id="agri-news-search",
            url="https://example.org/story?utm_source=google", summary="short",
            published_at="2026-08-05T08:00:00Z", source_count=1,
            search_providers="google_news",
        )
        second = RSSItem(
            title="Breeding result", feed_id="agri-news-search",
            url="https://example.org/story", summary="a much richer summary",
            published_at="2026-08-05T08:00:00Z", source_count=3,
            pre_hot_score=0.8, search_providers="gdelt",
        )
        storage.get_rss_data.side_effect = lambda date: {
            "2026-08-05": rss_data(date, first),
            "2026-08-06": rss_data(date, second),
        }.get(date)
        storage.save_rss_data.return_value = True
        storage.get_all_rss_ids.return_value = [{
            "id": 21, "source_id": "agri-news-search",
            "title": "Breeding result", "url": "https://example.org/story",
        }]

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )
        merged = list(result.iter_items())[0]

        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(merged.summary, "a much richer summary")
        self.assertEqual(merged.source_count, 3)
        self.assertEqual(merged.search_providers, "gdelt,google_news")

    def test_empty_week_does_not_write_snapshot(self):
        storage = MagicMock()
        storage.get_rss_data.return_value = None

        result = WeeklyRSSAggregator(storage, "Asia/Shanghai").build(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )

        self.assertIsNone(result.data)
        storage.save_rss_data.assert_not_called()

    def test_rebuilding_same_week_is_idempotent_in_sqlite(self):
        from tempfile import TemporaryDirectory
        from trendradar.storage.local import LocalStorageBackend

        tz = pytz.timezone("Asia/Shanghai")
        now = tz.localize(datetime(2026, 8, 10, 10, 0))
        with TemporaryDirectory() as data_dir:
            storage = LocalStorageBackend(
                data_dir=data_dir,
                enable_txt=False,
                enable_html=False,
                timezone="Asia/Shanghai",
            )
            storage.save_rss_data(rss_data("2026-08-05", RSSItem(
                title="Stable item", feed_id="journal",
                url="https://example.org/stable",
                published_at="2026-08-05T08:00:00+08:00",
            )))

            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)
            WeeklyRSSAggregator(storage, "Asia/Shanghai").build(now)

            monday = storage.get_rss_data("2026-08-10")
            urls = [
                item.url for items in monday.items.values() for item in items
            ]
            self.assertEqual(urls.count("https://example.org/stable"), 1)
            storage.cleanup()
```

- [ ] **步骤 2：运行并确认聚合器不存在**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_digest.WeeklyRSSAggregatorTests -v
```

预期：FAIL，`WeeklyRSSAggregator` 不存在。

- [ ] **步骤 3：实现稳定身份、字段合并和快照 ID**

新增 `WeeklyRSSSnapshot`，字段为 `window`、`data`、`allowed_rss_ids`、`missing_dates`、`failed_sources`、`total_read`、`filtered_out`、`duplicate_count`。聚合器必须：

1. 读取 `window.storage_dates` 的八个日库；
2. 丢弃 `window.contains()` 为假的条目；
3. 优先按 `canonicalize_url(url)` 去重，否则按 `feed_id + normalize_title(title)`；
4. 用 `(len(summary), source_count, pre_hot_score, bool(author))` 选择更完整记录；
5. 合并并排序 `search_providers`，取最大的 `source_count` 与 `pre_hot_score`；
6. 固定按 `published_at`、`feed_id`、`title` 排序；
7. 以 `date=window.end.strftime("%Y-%m-%d")`、`crawl_time="weekly"` 保存；
8. 保存失败时抛出 `RuntimeError("周快照保存失败")`；
9. 合并八日的 `failed_ids` 写入快照，并把逐日失败映射保存在 `failed_sources`；
10. 用 `storage.get_all_rss_ids(snapshot.date)` 查询实际 ID，仅保留与快照的 `(feed_id, canonical_url, normalized_title)` 一致的 ID；
11. 非空快照没有解析出任何 ID 时抛出 `RuntimeError("周快照 ID 解析失败")`，禁止 AI 在无范围约束下继续。

关键身份函数：

```python
def _item_identity(item: RSSItem):
    canonical = canonicalize_url(item.url)
    if canonical:
        return ("url", canonical)
    normalized = normalize_title(item.title)
    return ("title", item.feed_id, normalized) if normalized else ()
```

快照写入复用 SQLite 现有 `feed_id + URL/GUID` 更新逻辑；重复运行不得新增重复行。

- [ ] **步骤 4：运行聚合测试并提交**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_digest.WeeklyRSSAggregatorTests -v
git add trendradar/core/weekly.py tests/test_weekly_digest.py
git commit -m "feat(周报): 聚合并去重上周 RSS 数据"
```

预期：测试全部 PASS；提交只包含本任务文件。

### 任务 3：限制 AI 只处理周快照范围

**文件：**
- 修改：`trendradar/ai/filter_pipeline.py`
- 修改：`trendradar/context.py`
- 修改：`tests/test_weekly_digest.py`

- [ ] **步骤 1：编写周范围和允许 ID 测试**

```python
from trendradar.ai.filter import AIFilterResult
from trendradar.ai.filter_pipeline import AIFilterPipeline


class _WeeklyAIStorage:
    def get_all_news_ids(self):
        return []

    def get_analyzed_news_ids(self, source_type, interests_file):
        return set()

    def get_all_rss_ids(self):
        return [
            {"id": 1, "source_id": "journal",
             "published_at": "2026-08-03T00:00:00+08:00"},
            {"id": 2, "source_id": "journal",
             "published_at": "2026-08-09T23:59:59+08:00"},
            {"id": 3, "source_id": "journal",
             "published_at": "2026-08-10T00:00:00+08:00"},
        ]


class WeeklyAIFilterScopeTests(unittest.TestCase):
    def setUp(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )
        self.pipeline = AIFilterPipeline(
            {
                "TIMEZONE": "Asia/Shanghai",
                "RSS": {"ENABLED": True, "FRESHNESS_FILTER": {
                    "ENABLED": True, "MAX_AGE_DAYS": 1,
                }},
                "AI_FILTER": {"MIN_SCORE": 0},
            },
            _WeeklyAIStorage(),
            lambda: None,
            rss_window=window,
            allowed_rss_ids={1, 2},
        )

    def test_week_start_survives_but_current_monday_is_excluded(self):
        pending = self.pipeline._collect_pending_news("ai_interests.txt")
        self.assertEqual([item["id"] for item in pending[1]], [1, 2])
        self.assertEqual(pending[-1], 1)

    def test_report_conversion_rejects_unapproved_duplicate_id(self):
        result = AIFilterResult(success=True, tags=[{
            "tag": "育种", "count": 2, "items": [
                {"news_item_id": 1, "title": "Allowed", "source_type": "rss",
                 "source_id": "journal",
                 "first_time": "2026-08-03T00:00:00+08:00",
                 "relevance_score": 0.9},
                {"news_item_id": 9, "title": "Duplicate", "source_type": "rss",
                 "source_id": "journal",
                 "first_time": "2026-08-04T00:00:00+08:00",
                 "relevance_score": 0.9},
            ],
        }])

        _, rss_stats, _ = self.pipeline.convert_to_report_data(
            result, mode="weekly"
        )

        self.assertEqual(
            [item["title"] for item in rss_stats[0]["titles"]],
            ["Allowed"],
        )
```

- [ ] **步骤 2：运行并确认构造参数不存在**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_digest.WeeklyAIFilterScopeTests -v
```

预期：FAIL，`AIFilterPipeline.__init__()` 不接受周范围参数。

- [ ] **步骤 3：实现统一 RSS 范围判断**

为 `AIFilterPipeline.__init__()` 新增：

```python
rss_window: Optional[NaturalWeekWindow] = None,
allowed_rss_ids: Optional[set[int]] = None,
```

保存不可变副本并新增：

```python
def _is_rss_item_in_scope(
    self,
    feed_id: str,
    published_at: str,
    news_item_id: Optional[int] = None,
) -> bool:
    if (
        self._allowed_rss_ids is not None
        and news_item_id not in self._allowed_rss_ids
    ):
        return False
    if self._rss_window is not None:
        return self._rss_window.contains(published_at)
    return self._is_rss_item_fresh(feed_id, published_at)
```

以下位置统一使用该函数并传入 `id` 或 `news_item_id`：

- `_collect_pending_news()`；
- `run()` 获取 active 结果后；
- `_build_filter_result()` 的 RSS 重点排序；
- `_limit_search_hotspots()`；
- `convert_to_report_data()`。

`AppContext._get_ai_filter_pipeline()`、`run_ai_filter()` 和 `convert_ai_filter_to_report_data()` 显式接收并传递同一 `rss_window` 与 `allowed_rss_ids`。普通模式传 `None`，保持原有滚动新鲜度行为。

- [ ] **步骤 4：运行周范围和既有新鲜度测试**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest \
  tests.test_weekly_digest.WeeklyAIFilterScopeTests \
  tests.test_sciencedirect_rss_dates \
  tests.test_rss_strict_freshness -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交 AI 周范围**

```bash
git add trendradar/ai/filter_pipeline.py trendradar/context.py \
  tests/test_weekly_digest.py
git commit -m "feat(AI筛选): 限制周报自然周数据范围"
```

### 任务 4：前置调度并接入周一编排

**文件：**
- 修改：`trendradar/__main__.py`
- 修改：`trendradar/core/scheduler.py`
- 创建：`tests/test_weekly_schedule.py`

- [ ] **步骤 1：编写周一、静默日和主流程短路测试**

```python
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytz

from trendradar.__main__ import NewsAnalyzer
from trendradar.core.scheduler import Scheduler


TIMELINE = {"custom": {
    "default": {
        "collect": True, "analyze": False, "push": False,
        "report_mode": "current", "ai_mode": "follow_report",
        "once": {"analyze": False, "push": False},
    },
    "periods": {"monday_weekly": {
        "name": "自然周周报", "start": "00:00", "end": "23:59",
        "analyze": True, "push": True, "report_mode": "weekly",
        "ai_mode": "follow_report",
        "once": {"analyze": True, "push": True},
    }},
    "day_plans": {
        "monday": {"periods": ["monday_weekly"]},
        "silent": {"periods": []},
    },
    "week_map": {
        1: "monday", 2: "silent", 3: "silent", 4: "silent",
        5: "silent", 6: "silent", 7: "silent",
    },
}}


class WeeklyScheduleTests(unittest.TestCase):
    def resolve(self, when):
        return Scheduler(
            {"enabled": True, "preset": "custom"},
            TIMELINE, MagicMock(), lambda: when,
        ).resolve()

    def test_monday_collects_analyzes_and_pushes_weekly_once(self):
        schedule = self.resolve(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            )
        )
        self.assertTrue(schedule.collect)
        self.assertTrue(schedule.analyze)
        self.assertTrue(schedule.push)
        self.assertEqual(schedule.report_mode, "weekly")
        self.assertEqual(schedule.ai_mode, "weekly")
        self.assertTrue(schedule.once_analyze)
        self.assertTrue(schedule.once_push)

    def test_tuesday_only_collects(self):
        schedule = self.resolve(
            pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 11, 10, 0)
            )
        )
        self.assertTrue(schedule.collect)
        self.assertFalse(schedule.analyze)
        self.assertFalse(schedule.push)

    def test_silent_run_never_enters_analysis_pipeline(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(cleanup=MagicMock())
        analyzer._initialize_and_check_config = MagicMock(return_value=True)
        analyzer._resolve_and_apply_schedule = MagicMock(
            return_value=SimpleNamespace(
                collect=True, analyze=False, push=False,
            )
        )
        analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
        analyzer._crawl_rss_data = MagicMock(
            return_value=(None, None, [], set())
        )
        analyzer._execute_mode_strategy = MagicMock()

        analyzer.run()

        analyzer._crawl_data.assert_called_once()
        analyzer._crawl_rss_data.assert_called_once()
        analyzer._execute_mode_strategy.assert_not_called()

    def test_weekly_failures_do_not_fall_back_or_mark_partial_delivery(self):
        self.assertFalse(
            NewsAnalyzer._should_fallback_ai_filter("weekly")
        )
        self.assertTrue(
            NewsAnalyzer._should_fallback_ai_filter("daily")
        )
        self.assertFalse(
            NewsAnalyzer._notification_delivery_succeeded(
                "weekly", {"wework": True, "email": False}
            )
        )
        self.assertTrue(
            NewsAnalyzer._notification_delivery_succeeded(
                "daily", {"wework": True, "email": False}
            )
        )
```

- [ ] **步骤 2：运行并确认前置调度不存在**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_schedule -v
```

预期：FAIL，缺少 `_resolve_and_apply_schedule()`，或静默日仍进入模式策略。

- [ ] **步骤 3：采集前只解析一次调度**

新增：

```python
def _resolve_and_apply_schedule(self) -> ResolvedSchedule:
    schedule = self.ctx.create_scheduler().resolve()
    self.report_mode = schedule.report_mode
    self.frequency_file = schedule.frequency_file
    self.filter_method = schedule.filter_method or self.ctx.filter_method
    self.interests_file = schedule.interests_file
    return schedule
```

调整 `run()`，在 `_initialize_and_check_config()` 之前应用调度，使启动日志也显示本次真实报告模式：

```python
schedule = self._resolve_and_apply_schedule()
if not self._initialize_and_check_config():
    return
if not schedule.collect:
    print("[调度] 当前不执行采集")
    return
results, id_to_name, failed_ids = self._crawl_data()
rss_items, rss_new_items, raw_rss_items, rss_new_urls = (
    self._crawl_rss_data()
)
if not schedule.analyze and not schedule.push:
    print("[调度] 静默采集完成，本次不执行 AI 和推送")
    return
self._execute_mode_strategy(..., schedule=schedule)
```

给 `_execute_mode_strategy()` 增加必传 `schedule`，删除内部再次解析调度。这样 `_process_rss_data_by_mode()` 从抓取阶段就看到正确的 `weekly` 模式。

- [ ] **步骤 4：把周快照接入 RSS 处理和 AI**

在初始化中设置：

```python
self._rss_window = None
self._allowed_rss_ids = None
self._report_period_label = ""
```

给 `_convert_rss_items_to_list()` 增加 `apply_freshness: bool = True`。在 `_process_rss_data_by_mode()` 增加 `weekly` 分支：

```python
snapshot = WeeklyRSSAggregator(
    self.storage_manager,
    self.ctx.timezone,
).build(self.ctx.get_time())
if not snapshot.data:
    print("[周报] 上一自然周没有可用数据，不生成空周报")
    return None, None, None, set()

self._rss_window = snapshot.window
self._allowed_rss_ids = snapshot.allowed_rss_ids
self._report_period_label = snapshot.window.label
self._rss_total_count = sum(
    len(items) for items in snapshot.data.items.values()
)
raw_rss_items = self._convert_rss_items_to_list(
    snapshot.data.items,
    snapshot.data.id_to_name,
    apply_freshness=False,
)
```

周报分支用 `count_rss_frequency()` 统计快照，不调用 `detect_new_rss_items()`；`rss_new_items=None`、`rss_new_urls=set()`。在 `_run_analysis_pipeline()` 调用 `run_ai_filter()` 和 `convert_ai_filter_to_report_data()` 时传入 `_rss_window`、`_allowed_rss_ids`。

聚合器在构建结束时打印 `total_read`、`filtered_out`、`duplicate_count`、保留数量、`missing_dates` 和 `failed_sources`；日志不得包含文章正文或任何密钥。

`_execute_mode_strategy()` 增加 `weekly` 分支，使用当前热榜数据和聚合后的 RSS 进入统一分析流水线，不调用当日 `_load_analysis_data()`。快照为空或 AI 匹配为零时沿用 `_send_notification_if_needed()` 的空内容短路。

新增两个无副作用策略函数并接入现有分支：

```python
@staticmethod
def _should_fallback_ai_filter(mode: str) -> bool:
    return mode != "weekly"

@staticmethod
def _notification_delivery_succeeded(
    mode: str,
    results: Dict[str, bool],
) -> bool:
    return bool(results) and (
        all(results.values()) if mode == "weekly" else any(results.values())
    )
```

AI 筛选失败时，普通模式继续现有关键词回退；`weekly` 模式抛出 `RuntimeError("周报 AI 筛选失败")`。周报所有已配置通知渠道都成功后才记录 `once_push`；任一失败则返回失败且不记录，允许人工补跑。

把 `run()` 改为返回 `bool`：静默采集、空周和完整成功返回 `True`，捕获异常后返回 `False`。`main()` 在 `analyzer.run()` 为 `False` 时执行 `raise SystemExit(1)`，让 supercronic 能看到失败退出码；调试模式仍保留原异常。

- [ ] **步骤 5：运行调度、聚合和 AI 范围测试**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_schedule tests.test_weekly_digest -v
```

预期：全部 PASS。

- [ ] **步骤 6：提交周一编排**

```bash
git add trendradar/__main__.py trendradar/core/scheduler.py \
  tests/test_weekly_schedule.py
git commit -m "feat(调度): 每日静默采集并在周一生成周报"
```

### 任务 5：显示周报类型和自然周范围

**文件：**
- 修改：`trendradar/__main__.py`
- 修改：`trendradar/notification/splitter.py`
- 修改：`trendradar/notification/renderer.py`
- 修改：`trendradar/report/generator.py`
- 修改：`trendradar/report/html.py`
- 创建：`tests/test_weekly_report_output.py`

- [ ] **步骤 1：编写模式和周期展示测试**

```python
import unittest

from trendradar.__main__ import NewsAnalyzer
from trendradar.notification.splitter import split_content_into_batches
from trendradar.report.generator import generate_html_report
from trendradar.report.html import render_html_content


REPORT_DATA = {
    "stats": [], "new_titles": [], "failed_ids": [],
    "total_new_count": 0, "rss_matched_count": 1,
    "rss_total_count": 1, "rss_source_total": 1,
    "rss_source_failed": 0,
    "period_label": "2026-08-03—2026-08-09",
}
RSS_STATS = [{"word": "育种", "count": 1, "titles": [{
    "title": "Weekly item", "source_name": "Journal",
    "url": "https://example.org/item", "time": "08-05 10:00",
}]}]


class WeeklyReportOutputTests(unittest.TestCase):
    def test_weekly_strategy_has_explicit_type(self):
        self.assertEqual(
            NewsAnalyzer.MODE_STRATEGIES["weekly"]["report_type"],
            "上周周报",
        )

    def test_notification_header_contains_period(self):
        content = "\n".join(split_content_into_batches(
            REPORT_DATA,
            "wework",
            mode="weekly",
            report_type="上周周报",
            rss_items=RSS_STATS,
        ))
        self.assertIn("上周周报", content)
        self.assertIn("2026-08-03—2026-08-09", content)

    def test_html_renderer_contains_weekly_mode_and_period(self):
        html = render_html_content(
            report_data=REPORT_DATA,
            total_titles=1,
            mode="weekly",
            rss_items=RSS_STATS,
        )
        self.assertIn("上周周报", html)
        self.assertIn("2026-08-03—2026-08-09", html)

    def test_html_generator_forwards_period_metadata(self):
        from tempfile import TemporaryDirectory

        captured = {}

        def render(report_data, total_titles, mode, update_info):
            captured.update(report_data)
            return "<html></html>"

        with TemporaryDirectory() as output_dir:
            generate_html_report(
                stats=[], total_titles=0, mode="weekly",
                output_dir=output_dir, date_folder="2026-08-10",
                time_filename="10-00", render_html_func=render,
                report_metadata={
                    "period_label": "2026-08-03—2026-08-09"
                },
            )

        self.assertEqual(
            captured["period_label"], "2026-08-03—2026-08-09"
        )
```

- [ ] **步骤 2：运行并确认 weekly 被当作 daily 或缺少周期**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_report_output -v
```

预期：FAIL，`weekly` 策略不存在或输出缺少周期。

- [ ] **步骤 3：增加 weekly 策略和报告元数据**

在 `MODE_STRATEGIES` 添加：

```python
"weekly": {
    "mode_name": "自然周周报模式",
    "description": "自然周周报模式（上一周一至周日）",
    "report_type": "上周周报",
    "should_send_notification": True,
},
```

HTML 的 `report_metadata` 与通知的 `report_data` 都注入：

```python
"period_label": self._report_period_label,
```

把 `trendradar/report/generator.py` 的 `_METADATA_KEYS` 增加 `"period_label"`，确保 `AppContext.generate_html()` 传入的范围不会被白名单丢弃。

`split_content_into_batches()` 在“类型”后增加：

```python
period_label = report_data.get("period_label", "")
if period_label:
    base_header += f"{b_s}周期：{b_e} {period_label}\n"
```

通知 AI 模式映射、`renderer.py` 空状态文案和 HTML `mode_display` 均显式识别 `weekly`。主流程仍不得发送空消息。

- [ ] **步骤 4：运行报告和通知回归测试并提交**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD/trendradar:/app/trendradar:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_weekly_report_output tests.test_wework_pdf -v
git add trendradar/__main__.py trendradar/notification/splitter.py \
  trendradar/notification/renderer.py trendradar/report/generator.py \
  trendradar/report/html.py \
  tests/test_weekly_report_output.py
git commit -m "feat(周报): 展示自然周日期范围"
```

预期：全部 PASS；提交只包含列出的文件。

### 任务 6：配置每日 10 点和 48 小时重叠采集

**文件：**
- 修改：`trendradar/crawler/news_search.py`
- 修改：`tests/test_news_search.py`
- 修改：`config/config.yaml`
- 修改：`config/timeline.yaml`
- 修改：`docker/.env.example`
- 创建：`tests/test_weekly_configuration.py`

- [ ] **步骤 1：编写搜索和运行配置测试**

把 `tests/test_news_search.py` 的请求测试改为：

```python
def test_gdelt_builds_48_hour_article_list_params(self):
    self.assertEqual(
        GDELTClient().build_params("wheat breeding", 25)["timespan"],
        "48h",
    )

def test_google_rss_requests_two_day_window(self):
    self.assertEqual(
        GoogleNewsRSSClient().build_params("rice breeding", "en")["q"],
        "rice breeding when:2d",
    )
```

创建配置测试：

```python
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WeeklyConfigurationTests(unittest.TestCase):
    def test_runtime_uses_custom_weekly_schedule(self):
        config = yaml.safe_load(
            (ROOT / "config/config.yaml").read_text(encoding="utf-8")
        )
        self.assertTrue(config["schedule"]["enabled"])
        self.assertEqual(config["schedule"]["preset"], "custom")
        self.assertEqual(
            config["rss"]["freshness_filter"]["max_age_days"], 2
        )
        active = [
            feed for feed in config["rss"]["feeds"]
            if feed.get("enabled", True)
        ]
        self.assertTrue(all(feed.get("max_age_days") == 2 for feed in active))

    def test_custom_timeline_pushes_only_on_monday(self):
        timeline = yaml.safe_load(
            (ROOT / "config/timeline.yaml").read_text(encoding="utf-8")
        )["custom"]
        self.assertEqual(timeline["week_map"][1], "monday")
        self.assertEqual(
            timeline["periods"]["monday_weekly"]["report_mode"],
            "weekly",
        )
        for weekday in range(2, 8):
            self.assertEqual(timeline["week_map"][weekday], "silent")

    def test_example_cron_runs_daily_at_ten(self):
        text = (ROOT / "docker/.env.example").read_text(encoding="utf-8")
        self.assertIn('CRON_SCHEDULE="0 10 * * *"', text)
```

- [ ] **步骤 2：运行并确认旧参数导致失败**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD:/app:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_news_search.ProviderRequestTests \
  tests.test_weekly_configuration -v
```

预期：FAIL，仍为 `24h`、`when:1d`、关闭调度和旧 Cron。

- [ ] **步骤 3：修改搜索窗口**

在 `GDELTClient.build_params()` 使用 `"timespan": "48h"`；在 `GoogleNewsRSSClient.build_params()` 使用：

```python
return {"q": f"{query} when:2d", **locale}
```

- [ ] **步骤 4：配置自然周时间线**

`config/config.yaml`：

```yaml
schedule:
  enabled: true
  preset: "custom"
```

把全局及所有当前启用 feed 的 `max_age_days` 改为 `2`；三个明确 `enabled: false` 的示例源保持原值。

`config/timeline.yaml` 的 `custom` 改为：

```yaml
custom:
  name: "自然周周报"
  description: "每日静默采集，每周一汇总推送上一自然周。"
  default:
    collect: true
    analyze: false
    ai_mode: "follow_report"
    push: false
    report_mode: "current"
    once:
      analyze: false
      push: false
  periods:
    monday_weekly:
      name: "自然周周报"
      start: "00:00"
      end: "23:59"
      collect: true
      analyze: true
      ai_mode: "follow_report"
      push: true
      report_mode: "weekly"
      once:
        analyze: true
        push: true
  day_plans:
    monday:
      periods: ["monday_weekly"]
    silent:
      periods: []
  week_map:
    1: "monday"
    2: "silent"
    3: "silent"
    4: "silent"
    5: "silent"
    6: "silent"
    7: "silent"
  overlap:
    policy: "error_on_overlap"
```

`docker/.env.example` 设置 `CRON_SCHEDULE="0 10 * * *"`。

- [ ] **步骤 5：运行配置测试并提交**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD:/app:ro" \
  -w /app docker-trendradar \
  -m unittest tests.test_news_search.ProviderRequestTests \
  tests.test_weekly_configuration -v
git add trendradar/crawler/news_search.py tests/test_news_search.py \
  config/config.yaml config/timeline.yaml docker/.env.example \
  tests/test_weekly_configuration.py
git commit -m "feat(配置): 每日十点采集并周一推送"
```

预期：全部 PASS；真实 `docker/.env` 不在提交中。

### 任务 7：完整回归、合并和部署

**文件：**
- 修改：`docker/.env`（本机忽略文件，不提交）

- [ ] **步骤 1：运行全部 Python 测试**

```bash
docker run --rm --network none --entrypoint .venv/bin/python \
  -v "$PWD:/app:ro" \
  -w /app docker-trendradar \
  -m unittest discover -s tests -v
```

预期：全部 PASS；既有 `daily`、`current`、`incremental`、Elsevier 全文和 AI JSON 韧性测试无回归。

- [ ] **步骤 2：运行部署测试与静态检查**

```bash
bash tests/test_portable_deployment.sh
git diff --check
git status --short
```

预期：部署测试 PASS，`git diff --check` 无输出，状态只包含本任务预期文件。

- [ ] **步骤 3：修正回归时先加失败测试并单独提交**

如果步骤 1 或 2 发现问题，先在对应 `tests/test_weekly_*.py` 或既有回归测试中复现，再修正实现。只暂存实际修正文件：

```bash
git add tests/test_weekly_digest.py trendradar/core/weekly.py
git commit -m "fix(周报): 修正自然周推送回归问题"
```

若修正涉及其他文件，用精确路径替换上例；不得使用 `git add .`。

- [ ] **步骤 4：快进合并功能分支**

在主工作区执行：

```bash
git merge --ff-only agent/natural-week-push
```

预期：合并成功，主工作区原有 `index.html`、旧文档和 `output/` 改动保持不变。

- [ ] **步骤 5：修改实际 Cron 并验证 Compose**

在主工作区忽略文件 `docker/.env` 设置：

```dotenv
CRON_SCHEDULE="0 10 * * *"
```

运行：

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose.yml config
```

预期：配置解析成功，`CRON_SCHEDULE` 为 `0 10 * * *`。不得输出 API Key、机器人 Key 或 Elsevier institutional token 的值。

- [ ] **步骤 6：重建并启动服务**

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose.yml up -d --build
docker compose --env-file docker/.env \
  -f docker/docker-compose.yml ps
```

预期：`trendradar` 服务状态为 `Up`。

- [ ] **步骤 7：验证 Cron 与服务日志**

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose.yml logs --tail=120 trendradar
```

预期日志包含：

```text
0 10 * * * cd /app && python -m trendradar
启动supercronic: 0 10 * * *
```

非周一只验证容器健康和自动化调度测试，不发送真实通知。若当前为周一且需要补跑，先取得用户对真实推送的单独确认。

- [ ] **步骤 8：最终敏感信息与工作区检查**

```bash
git status --short
git log --oneline -8
git grep -n "ELSEVIER_INST_TOKEN=.*[^}]" -- ':!docker/.env'
```

预期：功能提交完整；真实凭据仅存在于 Git 忽略的 `docker/.env`，用户原有未提交文件未被覆盖。
