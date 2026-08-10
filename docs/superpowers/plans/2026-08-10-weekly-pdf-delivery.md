# 每周农业新闻 PDF 推送实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 每天静默采集农业新闻，每周一从上一自然周候选中严格筛选最多 20 条，并加入当期中央气象台农业气象周报，最终通过企业微信只发送一份专用 PDF。

**架构：** 运行入口冻结一次 `run_at`，所有普通新闻由唯一的 `NaturalWeekWindow` 判定；新闻搜索只使用从该窗口派生的召回边界。周一先校验当期官方农业气象周报，再执行周聚合、严格 AI、专用打印 HTML/PDF 和企业微信文件投递。成功检查点只负责同周幂等，任何必需阶段失败都不写检查点。

**技术栈：** Python 3.12、`unittest`、SQLite、`requests`、`feedparser`、`pytz`、Chromium Headless、Docker Compose、企业微信群机器人文件 API

---

## 文件结构

- 修改 `trendradar/core/weekly.py`：唯一上一自然周窗口、日期精度判断、双边日库聚合和最多 20 条主题均衡选择。
- 修改 `trendradar/crawler/rss/parser.py`：保留纯日期精度，标准 RSS 时间输出明确 UTC offset。
- 修改 `trendradar/crawler/rss/fetcher.py`：删除 RSS 滚动 freshness 和 `max_age_days`。
- 修改 `trendradar/crawler/news_search.py`：自然周召回边界、删除滚动 24/48 小时规则。
- 创建 `trendradar/crawler/agro_weather.py`：中央气象台农业气象周报抓取、解析和本期校验。
- 修改 `trendradar/core/loader.py`：删除旧 freshness/PDF 可选配置，加载农业气象源。
- 修改 `trendradar/__main__.py`：每天静默采集、周一气象门控、周聚合、PDF-only 交付和人工补跑。
- 创建 `trendradar/report/weekly_pdf.py`：专用 A4 HTML 模板和 PDF 构建器。
- 修改 `trendradar/report/pdf.py`：输出文件名、PDF 完整性和大小校验。
- 修改 `trendradar/notification/wework_pdf.py`：仅上传并发送 PDF 文件。
- 修改 `trendradar/notification/dispatcher.py`：新增严格的企业微信 PDF-only 调度入口。
- 修改 `trendradar/notification/senders.py`：普通模式保持兼容，周报不再进入文字分批路径。
- 修改 `config/config.yaml`、`config/config.en.yaml`：删除旧滚动时间字段，增加农业气象官方源。
- 修改 `config/timeline.yaml`、`config/timeline.en.yaml`：周一周报、周二至周日静默采集。
- 修改 `docker/entrypoint.sh`、`docker/.env.example`、两个 Compose 文件：支持多条短 Cron 触发。
- 修改 `docs/index.html`、`docs/assets/script.js`、`docs/assets/i18n.js`、`docs/news-push-technical-implementation.md`：删除失效配置并说明周报 PDF。
- 创建 `tests/test_weekly_time_rule.py`、`tests/test_agro_weather.py`、`tests/test_weekly_pdf_report.py`、`tests/test_weekly_pdf_delivery.py`。
- 修改 `tests/test_weekly_digest.py`、`tests/test_weekly_schedule.py`、`tests/test_news_search.py`、`tests/test_news_search_pipeline.py`、`tests/test_sciencedirect_rss_dates.py`、`tests/test_wework_pdf.py`、`tests/test_portable_deployment.sh`。
- 删除 `tests/test_rss_strict_freshness.py`：被删除的滚动 freshness 功能不再保留测试。

所有 Python 测试使用镜像中的 `/app/.venv/bin/python`；不修改本地 `.venv`，不运行系统 Python。

### 任务 1：删除滚动时效规则并统一发布日期语义

**文件：**
- 创建：`tests/test_weekly_time_rule.py`
- 修改：`trendradar/crawler/rss/parser.py`
- 修改：`trendradar/crawler/rss/fetcher.py`
- 修改：`trendradar/core/loader.py`
- 修改：`trendradar/ai/filter_pipeline.py`
- 修改：`trendradar/context.py`
- 修改：`trendradar/utils/time.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 删除：`tests/test_rss_strict_freshness.py`
- 测试：`tests/test_sciencedirect_rss_dates.py`
- 测试：`tests/test_weekly_configuration.py`

- [ ] **步骤 1：编写旧规则清理和日期精度失败测试**

创建 `tests/test_weekly_time_rule.py`，覆盖运行配置、数据类和生产源码：

```python
import inspect
import unittest
from pathlib import Path

import yaml

from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.loader import _load_rss_config
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher


ROOT = Path(__file__).resolve().parents[1]


class WeeklyTimeRuleRemovalTests(unittest.TestCase):
    def test_runtime_rss_config_has_no_freshness_contract(self):
        loaded = _load_rss_config({"rss": {"enabled": True, "feeds": []}})
        self.assertNotIn("FRESHNESS_FILTER", loaded)
        self.assertNotIn("DEFAULT_MAX_AGE_DAYS", loaded)

    def test_feed_and_fetcher_have_no_age_options(self):
        self.assertNotIn("max_age_days", RSSFeedConfig.__dataclass_fields__)
        fetcher = RSSFetcher([])
        self.assertFalse(hasattr(fetcher, "freshness_enabled"))
        self.assertFalse(hasattr(fetcher, "default_max_age_days"))

    def test_active_yaml_has_no_removed_time_keys(self):
        for relative in ("config/config.yaml", "config/config.en.yaml"):
            raw = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
            self.assertNotIn("freshness_filter", raw["rss"])
            self.assertTrue(all(
                "max_age_days" not in feed
                for feed in raw["rss"].get("feeds", [])
            ))

    def test_ai_pipeline_has_no_freshness_filter(self):
        source = inspect.getsource(AIFilterPipeline)
        self.assertNotIn("_is_rss_item_fresh", source)
        self.assertNotIn("freshness_filtered_rss", source)
```

在 `tests/test_sciencedirect_rss_dates.py` 增加：

```python
def test_sciencedirect_fallback_preserves_date_only(self):
    items = self.parser.parse(
        self._feed("Available online 9 August 2026"),
        "https://rss.sciencedirect.com/publication/science/22145141",
    )
    self.assertEqual(items[0].published_at, "2026-08-09")

def test_standard_rss_struct_time_is_explicit_utc(self):
    value = self.parser._parse_date({
        "published_parsed": (2026, 8, 9, 1, 2, 3, 0, 0, 0),
    })
    self.assertEqual(value, "2026-08-09T01:02:03+00:00")
```

- [ ] **步骤 2：运行测试并确认旧实现失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_time_rule tests.test_sciencedirect_rss_dates tests.test_weekly_configuration -v
```

预期：`freshness_filter`、`max_age_days`、fetcher 属性和 ScienceDirect 日期断言失败。

- [ ] **步骤 3：删除 RSS 抓取层滚动过滤**

将 `RSSFeedConfig` 收敛为：

```python
@dataclass
class RSSFeedConfig:
    id: str
    name: str
    url: str
    max_items: int = 0
    enabled: bool = True
    source_type: str = "rss"
    fetch_url: str = ""
```

从 `RSSFetcher.__init__()`、`from_config()` 和 `fetch_feed()` 删除 `freshness_enabled`、`default_max_age_days`、`_is_item_fresh()` 及年龄列表过滤。`max_items` 截断后直接转换所有解析成功的条目。

从 `_load_rss_config()` 删除 `FRESHNESS_FILTER` 映射，从中英文 YAML 删除 `freshness_filter` 和每源 `max_age_days`。

- [ ] **步骤 4：统一 RSS 日期输出**

在 `trendradar/crawler/rss/parser.py` 中使用以下规则：

```python
from datetime import datetime, timezone


def _format_struct_time_utc(value) -> str:
    parsed = datetime(*value[:6], tzinfo=timezone.utc)
    return parsed.isoformat()


def _normalize_iso_source_date(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()
```

`published_parsed`/`updated_parsed` 调 `_format_struct_time_utc()`；ScienceDirect 和 JSON Feed 的纯日期保持 `YYYY-MM-DD`，完整无时区时间明确按 UTC 补 `+00:00`。

- [ ] **步骤 5：删除 AI 和报告转换中的二次 freshness**

`AIFilterPipeline._is_rss_item_in_scope()` 只保留以下三路：

```python
def _is_rss_item_in_scope(self, item: dict) -> bool:
    if self.allowed_rss_ids is not None:
        return item.get("id") in self.allowed_rss_ids
    if self.rss_window is not None:
        return self.rss_window.contains(str(item.get("published_at") or ""))
    return True
```

把 `freshness_filtered_rss` 重命名为 `scope_filtered_rss`。从 `__main__._convert_rss_items_to_list()` 删除 `apply_freshness` 参数和年龄过滤；从 `context.py`、`utils/time.py` 删除无生产调用的 `is_within_days()`、`calculate_days_old()`。

- [ ] **步骤 6：重跑任务测试并提交**

运行步骤 2 命令，预期全部 `OK`。随后提交：

```bash
git add config/config.yaml config/config.en.yaml trendradar/crawler/rss/parser.py trendradar/crawler/rss/fetcher.py trendradar/core/loader.py trendradar/ai/filter_pipeline.py trendradar/context.py trendradar/utils/time.py trendradar/__main__.py tests/test_weekly_time_rule.py tests/test_sciencedirect_rss_dates.py tests/test_weekly_configuration.py tests/test_rss_strict_freshness.py
git commit -m "refactor: 删除新闻滚动时效规则"
```

### 任务 2：建立自然周召回边界和唯一周聚合

**文件：**
- 修改：`trendradar/core/weekly.py`
- 修改：`trendradar/crawler/news_search.py`
- 修改：`trendradar/__main__.py`
- 测试：`tests/test_weekly_digest.py`
- 测试：`tests/test_news_search.py`
- 测试：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写自然周边界和搜索参数失败测试**

在 `tests/test_weekly_digest.py` 增加：

```python
SHANGHAI = pytz.timezone("Asia/Shanghai")


def test_previous_week_is_local_half_open_and_date_only_safe(self):
    now = SHANGHAI.localize(datetime(2026, 8, 10, 10, 0))
    window = previous_natural_week(now, "Asia/Shanghai")
    self.assertEqual(window.start.isoformat(), "2026-08-03T00:00:00+08:00")
    self.assertEqual(window.end.isoformat(), "2026-08-10T00:00:00+08:00")
    self.assertTrue(window.contains("2026-08-03"))
    self.assertTrue(window.contains("2026-08-09T23:59:59+08:00"))
    self.assertFalse(window.contains("2026-08-10"))
    self.assertFalse(window.contains(""))

def test_weekly_aggregator_reads_previous_seven_days_and_run_day(self):
    window = previous_natural_week(
        SHANGHAI.localize(datetime(2026, 8, 10, 10, 0)),
        "Asia/Shanghai",
    )
    self.assertEqual(window.storage_dates[0], "2026-08-03")
    self.assertEqual(window.storage_dates[-1], "2026-08-10")
    self.assertEqual(len(window.storage_dates), 8)
```

在 `tests/test_news_search.py` 增加上海窗口到供应商参数的断言：

```python
def test_provider_bounds_are_safe_superset_of_shanghai_week(self):
    window = NewsSearchBounds(
        start=SHANGHAI.localize(datetime(2026, 8, 3, 0, 0)),
        end=SHANGHAI.localize(datetime(2026, 8, 10, 0, 0)),
    )
    gdelt = GDELTClient().build_params("rice breeding", 20, window)
    self.assertEqual(gdelt["startdatetime"], "20260802155959")
    self.assertEqual(gdelt["enddatetime"], "20260809160000")
    self.assertNotIn("timespan", gdelt)
    google = GoogleNewsRSSClient().build_params("水稻育种", "zh", window)
    self.assertIn("after:2026-08-02", google["q"])
    self.assertIn("before:2026-08-10", google["q"])
    self.assertNotIn("when:2d", google["q"])
```

- [ ] **步骤 2：运行测试并确认当前 24/48 小时实现失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_digest tests.test_news_search tests.test_news_search_pipeline -v
```

预期：GDELT `timespan=48h`、Google `when:2d`、搜索聚合 24 小时和纯日期窗口断言失败。

- [ ] **步骤 3：实现自然周安全解析**

在 `trendradar/core/weekly.py` 增加日期精度分支：

```python
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_week_published_at(value: str, timezone_name: str) -> datetime | None:
    text = str(value or "").strip()
    tz = pytz.timezone(timezone_name)
    if DATE_ONLY.fullmatch(text):
        try:
            return tz.localize(datetime.strptime(text, "%Y-%m-%d"))
        except ValueError:
            return None
    return parse_iso_datetime(text, timezone_name)


def current_natural_week(now: datetime, timezone_name: str) -> NaturalWeekWindow:
    tz = pytz.timezone(timezone_name)
    local_now = now.astimezone(tz)
    monday = (local_now - timedelta(days=local_now.weekday())).date()
    start = tz.localize(datetime.combine(monday, datetime.min.time()))
    return NaturalWeekWindow(start, start + timedelta(days=7), timezone_name)
```

`NaturalWeekWindow.contains()` 调该函数，并继续使用 `start <= parsed < end`。`storage_dates` 保留 8 个日期，以读取周一运行库中新抓到的上周条目。

- [ ] **步骤 4：实现搜索召回边界并删除滚动资格判断**

在 `trendradar/crawler/news_search.py` 增加：

```python
@dataclass(frozen=True)
class NewsSearchBounds:
    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("news search bounds must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("news search bounds must be increasing")

    @property
    def gdelt_start(self) -> datetime:
        return self.start.astimezone(timezone.utc) - timedelta(seconds=1)

    @property
    def gdelt_end(self) -> datetime:
        return self.end.astimezone(timezone.utc)

    @property
    def google_after(self) -> str:
        return (self.start.date() - timedelta(days=1)).isoformat()

    @property
    def google_before(self) -> str:
        return self.end.date().isoformat()
```

将 `GDELTClient.build_params/fetch()`、`GoogleNewsRSSClient.build_params/fetch()`、`AgriculturalNewsSearch.search()` 改为显式接收 `bounds`。删除 `aggregate()` 的未来/24 小时年龄拒绝和基于 `/24.0` 的 recency 分数；排名只使用来源覆盖和权威度：

```python
pre_hot_score = round(0.6 * coverage + 0.4 * authority, 4)
```

GDELT 使用 `startdatetime/enddatetime`，Google 查询附加 `after/before`；二者都是召回超集，最终仍由 `WeeklyRSSAggregator` 过滤。

- [ ] **步骤 5：让每日静默采集和周一汇总使用自然周边界**

在 `__main__.py` 中建立：

```python
def _news_search_bounds(self, report_mode: str) -> NewsSearchBounds:
    run_at = self._operation_run_at()
    if report_mode == "weekly":
        window = previous_natural_week(run_at, self.ctx.timezone)
    else:
        window = current_natural_week(run_at, self.ctx.timezone)
    return NewsSearchBounds(window.start, window.end)
```

每日静默采集使用当前自然周召回边界；周一周报使用上一自然周边界。新闻搜索调用必须传入该对象，不允许无界 active 调用。

- [ ] **步骤 6：实现最多 20 条的稳定主题均衡选择**

在 `trendradar/core/weekly.py` 增加 `select_weekly_news()`。输入必须是 AI 已判定合格的去重条目；先按 `highlight_rank`、AI score、发布时间、来源、标题形成稳定顺序，固定前 5 条，再对其余条目按主主题轮转补足至 20 条：

```python
def report_item_identity(item: dict) -> tuple[str, str]:
    url = canonicalize_url(str(item.get("url") or ""))
    if url:
        return ("url", url)
    return ("title", normalize_title(str(item.get("title") or "")))


def primary_weekly_topic(item: dict) -> str:
    topics = item.get("weekly_topics") or []
    if isinstance(topics, str):
        topics = [topics]
    normalized = sorted({str(topic).strip() for topic in topics if str(topic).strip()})
    return normalized[0] if normalized else "其他"


def weekly_value_sort_key(item: dict) -> tuple:
    try:
        highlight = int(item.get("highlight_rank") or 10**9)
    except (TypeError, ValueError):
        highlight = 10**9
    try:
        score = float(item.get("ai_score") or item.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    published = parse_week_published_at(
        str(item.get("published_at") or ""), "Asia/Shanghai"
    )
    published_epoch = published.timestamp() if published else 0.0
    return (
        highlight,
        -score,
        -published_epoch,
        str(item.get("source_name") or ""),
        str(item.get("title") or ""),
    )


def deduplicate_report_items(items: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for raw in items:
        item = dict(raw)
        key = report_item_identity(item)
        if not key[1]:
            continue
        existing = merged.get(key)
        if existing is None or weekly_value_sort_key(item) < weekly_value_sort_key(existing):
            merged[key] = item
    return list(merged.values())


def select_weekly_news(
    items: list[dict],
    *,
    limit: int = 20,
    highlight_count: int = 5,
) -> list[dict]:
    unique = deduplicate_report_items(items)
    ranked = sorted(unique, key=weekly_value_sort_key)
    selected = ranked[:min(highlight_count, limit)]
    selected_keys = {report_item_identity(item) for item in selected}
    buckets: dict[str, deque[dict]] = {}
    for item in ranked:
        if report_item_identity(item) in selected_keys:
            continue
        topic = primary_weekly_topic(item) or "其他"
        buckets.setdefault(topic, deque()).append(item)
    topics = sorted(buckets)
    while len(selected) < limit and topics:
        next_topics = []
        for topic in topics:
            bucket = buckets[topic]
            if bucket and len(selected) < limit:
                item = bucket.popleft()
                selected.append(item)
                selected_keys.add(report_item_identity(item))
            if bucket:
                next_topics.append(topic)
        topics = next_topics
    for rank, item in enumerate(selected[:highlight_count], start=1):
        item["highlight_rank"] = rank
    return selected
```

主流程把 AI 结果各分组的 `word` 写入条目的 `weekly_topics` 后再调用该函数，PDF builder 只接收返回值。

测试至少覆盖：30 条同主题+多主题候选只返回 20 条；TOP 5 不因轮转丢失；其余名额包含多个主题；重复 URL 只出现一次；输入少于 20 条时全部返回；相同输入重复运行顺序一致。《全国农业气象周报》不传入该函数，因此不占名额。

- [ ] **步骤 7：重跑并提交**

运行步骤 2 命令，预期全部 `OK`，提交：

```bash
git add trendradar/core/weekly.py trendradar/crawler/news_search.py trendradar/__main__.py tests/test_weekly_digest.py tests/test_news_search.py tests/test_news_search_pipeline.py
git commit -m "feat: 统一自然周新闻范围"
```

### 任务 3：新增中央气象台农业气象周报监控

**文件：**
- 创建：`trendradar/crawler/agro_weather.py`
- 修改：`trendradar/crawler/__init__.py`
- 修改：`trendradar/core/loader.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 创建：`tests/test_agro_weather.py`

- [ ] **步骤 1：编写官方页面解析和本期校验失败测试**

创建 `tests/test_agro_weather.py`，使用本地 HTML，不访问网络：

```python
import unittest
from datetime import datetime
from unittest.mock import MagicMock

import pytz

from trendradar.crawler.agro_weather import AgroWeatherClient


HTML = """
<html><body>
<h1>全国农业气象周报</h1>
<p>预报：李轩　签发：郑昌玲　2026 年 08 月 10 日</p>
<h2>本周西北地区东部阴雨寡照</h2>
<h3>一、本周天气特点及农业影响分析</h3>
<p>本周（2026年8月2日-2026年8月8日），东北农区光温适宜。</p>
<h3>二、未来天气对农业生产影响预估及建议</h3>
<p>未来10天，黄淮等地有强降雨，低洼农田渍涝风险高。</p>
<p>建议：及时排涝散墒，做好病虫害监测。</p>
</body></html>
"""


class AgroWeatherClientTests(unittest.TestCase):
    def test_parses_current_cycle_report(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.text = HTML
        session.get.return_value.raise_for_status.return_value = None
        client = AgroWeatherClient(session=session)
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )
        report = client.fetch_latest(run_at)
        self.assertEqual(report.report_date.isoformat(), "2026-08-10")
        self.assertEqual(report.reviewed_start.isoformat(), "2026-08-02")
        self.assertEqual(report.reviewed_end.isoformat(), "2026-08-08")
        self.assertIn("未来10天", report.outlook)
        self.assertIn("排涝", report.recommendations)

    def test_rejects_stale_cycle(self):
        stale = HTML.replace("08 月 10 日", "08 月 03 日").replace(
            "8月2日-2026年8月8日", "7月26日-2026年8月1日"
        )
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.text = stale
        session.get.return_value.raise_for_status.return_value = None
        client = AgroWeatherClient(session=session)
        run_at = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )
        self.assertIsNone(client.fetch_latest(run_at))
```

再增加周日提前发布、本期正文缺少未来展望、HTTP 403/500、空正文和每次调用重新请求的测试。

- [ ] **步骤 2：运行测试并确认模块不存在**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_agro_weather -v
```

预期：导入 `trendradar.crawler.agro_weather` 失败。

- [ ] **步骤 3：实现数据模型、HTML 提取和周期校验**

创建 `trendradar/crawler/agro_weather.py`：

```python
@dataclass(frozen=True)
class AgroWeatherReport:
    title: str
    report_date: date
    reviewed_start: date
    reviewed_end: date
    impact: str
    outlook: str
    recommendations: str
    source_url: str

    def belongs_to_run(self, run_at: datetime, timezone_name: str) -> bool:
        local_date = run_at.astimezone(pytz.timezone(timezone_name)).date()
        valid_report_dates = {local_date, local_date - timedelta(days=1)}
        valid_review_ends = {
            local_date - timedelta(days=1),
            local_date - timedelta(days=2),
        }
        return (
            self.report_date in valid_report_dates
            and self.reviewed_end in valid_review_ends
            and (self.reviewed_end - self.reviewed_start).days == 6
            and bool(self.impact.strip())
            and bool(self.outlook.strip())
            and bool(self.recommendations.strip())
        )
```

使用标准库 `html.parser.HTMLParser` 提取可见文本，定位“全国农业气象周报”后紧邻的签发日期、`本周（起始-结束）`、第一部分影响、第二部分未来天气和“建议：”段落。不要引入新的 HTML 依赖。

`AgroWeatherClient.fetch_latest(run_at)` 每次执行 `session.get()`，调用 `raise_for_status()`，解析后只在 `belongs_to_run()` 为真时返回报告；旧报告返回 `None`，网络/结构错误抛出带来源上下文的异常。

- [ ] **步骤 4：增加配置并重跑测试**

在中英文配置加入：

```yaml
agro_weather:
  enabled: true
  url: "https://www.nmc.cn/publish/agro/ten-week/index.html"
  timeout: 30
```

`_load_agro_weather_config()` 映射为 `AGRO_WEATHER`，保留 URL、timeout 和 enabled；周报气象专栏固定为必须项。运行步骤 2，预期全部 `OK`。

- [ ] **步骤 5：提交农业气象监控**

```bash
git add trendradar/crawler/agro_weather.py trendradar/crawler/__init__.py trendradar/core/loader.py config/config.yaml config/config.en.yaml tests/test_agro_weather.py
git commit -m "feat: 监控全国农业气象周报"
```

### 任务 4：接入周一重试、每日静默采集和人工补跑

**文件：**
- 修改：`trendradar/__main__.py`
- 修改：`trendradar/core/scheduler.py`
- 修改：`config/timeline.yaml`
- 修改：`config/timeline.en.yaml`
- 修改：`docker/entrypoint.sh`
- 修改：`docker/.env.example`
- 修改：`docker/docker-compose.yml`
- 修改：`docker/docker-compose-build.yml`
- 测试：`tests/test_weekly_schedule.py`
- 测试：`tests/test_portable_deployment.sh`

- [ ] **步骤 1：编写调度和失败关闭测试**

在 `tests/test_weekly_schedule.py` 增加：

```python
CURRENT_WEATHER = SimpleNamespace(
    report_date="2026-08-10",
    review_start="2026-08-02",
    review_end="2026-08-08",
)


def make_analyzer(self, run_at=RUN_AT):
    scheduler = MagicMock()
    scheduler.already_executed.return_value = False
    scheduler.record_execution.return_value = True
    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    analyzer.ctx = SimpleNamespace(
        cleanup=MagicMock(),
        config={"DEBUG": False},
        get_time=MagicMock(return_value=run_at),
        create_scheduler=MagicMock(return_value=scheduler),
    )
    analyzer.report_mode = "weekly"
    analyzer._initialize_and_check_config = MagicMock(return_value=True)
    analyzer._resolve_and_apply_schedule = MagicMock(return_value=schedule())
    analyzer._fetch_agro_weather = MagicMock(return_value=CURRENT_WEATHER)
    analyzer._crawl_data = MagicMock(return_value=({}, {}, []))
    analyzer._crawl_rss_data = MagicMock(
        return_value=(None, None, [], set())
    )
    analyzer._execute_mode_strategy = MagicMock(return_value=True)
    return analyzer


def test_monday_attempt_window_and_other_days_silent_collect(self):
    monday = self.resolve(at(2026, 8, 10, 10, 30))
    self.assertEqual(monday.report_mode, "weekly")
    self.assertTrue(monday.collect and monday.analyze and monday.push)
    tuesday = self.resolve(at(2026, 8, 11, 10, 0))
    self.assertTrue(tuesday.collect)
    self.assertFalse(tuesday.analyze)
    self.assertFalse(tuesday.push)
    tuesday_late = self.resolve(at(2026, 8, 11, 10, 30))
    self.assertFalse(tuesday_late.collect)

def test_missing_current_weather_aborts_before_ordinary_crawl(self):
    analyzer = self.make_analyzer(run_at=RUN_AT)
    analyzer._fetch_agro_weather = MagicMock(return_value=None)
    analyzer._crawl_data = MagicMock()
    analyzer._crawl_rss_data = MagicMock()
    self.assertFalse(analyzer.run())
    analyzer._crawl_data.assert_not_called()
    analyzer._crawl_rss_data.assert_not_called()

def test_success_checkpoint_skips_retry_before_network(self):
    analyzer = self.make_analyzer(run_at=RUN_AT)
    analyzer.ctx.create_scheduler().already_executed.return_value = True
    analyzer._fetch_agro_weather = MagicMock()
    self.assertTrue(analyzer.run())
    analyzer._fetch_agro_weather.assert_not_called()
```

在测试文件顶部增加明确的时间工厂，并把上面的 `make_analyzer()` 作为 `WeeklyScheduleTests` 方法（首参数为 `self`），沿用该文件现有的 `NewsAnalyzer.__new__` 夹具模式：

```python
def at(year: int, month: int, day: int, hour: int, minute: int):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute)
    )
```

缺失气象、已成功检查点和爬取异常测试只覆盖上述默认 mock；不得让单元测试访问真实网络或磁盘。

增加 10:00、10:30、11:00、11:30、12:00 均进入周报，12:30 不采集；周日报告可接受；过旧报告失败；`--force-weekly` 在时间窗外进入周报但仍尊重同周检查点的测试。

- [ ] **步骤 2：运行测试并确认现有每日时间线失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_schedule -v
```

预期：当前 `daily_delivery` 全天时间线、无气象门控和无强制周报入口导致失败。

- [ ] **步骤 3：配置每日静默采集与周一周报**

把 `config/timeline.yaml` 和英文版本的 custom 收敛为：

```yaml
custom:
  name: "每周农业新闻 PDF"
  description: "每天静默采集，周一生成上一自然周 PDF。"
  default:
    collect: false
    analyze: false
    push: false
    report_mode: "current"
    ai_mode: "follow_report"
    once: {analyze: false, push: false}
  periods:
    daily_collect:
      name: "每日静默采集"
      start: "10:00"
      end: "10:01"
      collect: true
      analyze: false
      push: false
      report_mode: "current"
    monday_weekly:
      name: "每周农业新闻 PDF"
      start: "10:00"
      end: "12:01"
      collect: true
      analyze: true
      push: true
      report_mode: "weekly"
      ai_mode: "weekly"
      once: {analyze: true, push: true}
  day_plans:
    monday: {periods: ["monday_weekly"]}
    collect_only: {periods: ["daily_collect"]}
  week_map:
    1: "monday"
    2: "collect_only"
    3: "collect_only"
    4: "collect_only"
    5: "collect_only"
    6: "collect_only"
    7: "collect_only"
```

- [ ] **步骤 4：支持多条短 Cron，不在进程内等待两小时**

在 `docker/entrypoint.sh` 中兼容旧 `CRON_SCHEDULE`，新增分号分隔的 `CRON_SCHEDULES`：

```bash
CRON_LIST="${CRON_SCHEDULES:-${CRON_SCHEDULE:-0 10 * * *}}"
: > /tmp/crontab
IFS=';' read -ra EXPRESSIONS <<< "$CRON_LIST"
for CRON_EXPR in "${EXPRESSIONS[@]}"; do
    CRON_EXPR="$(echo "$CRON_EXPR" | xargs)"
    if ! echo "$CRON_EXPR" | grep -qE '^[0-9*/,[:space:]-]+$'; then
        echo "❌ CRON_SCHEDULES 格式非法: $CRON_EXPR"
        exit 1
    fi
    echo "$CRON_EXPR cd /app && python -m trendradar" >> /tmp/crontab
done
```

示例和 Compose 默认值使用：

```text
0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1
```

这会每天 10:00 采集，并在周一额外触发 10:30、11:00、11:30、12:00。应用时间线保证其他时刻不推送。

- [ ] **步骤 5：在主流程前置幂等与气象门控**

在 `NewsAnalyzer.run()` 冻结 `run_at` 后、普通抓取前执行：

```python
if schedule.report_mode == "weekly":
    window = previous_natural_week(self._operation_run_at(), self.ctx.timezone)
    checkpoint_date = window.end.strftime("%Y-%m-%d")
    scheduler = self.ctx.create_scheduler()
    if schedule.once_push and scheduler.already_executed(
        schedule.period_key, "push", checkpoint_date
    ):
        print("[周报] 本周 PDF 已成功发送，跳过重试")
        return True
    self._agro_weather_report = self._fetch_agro_weather()
    if self._agro_weather_report is None:
        raise RuntimeError("本期全国农业气象周报尚未发布")
```

周报检查点的日期统一使用 `window.end`，分析和推送都不得使用当前抓取完成时间。`_record_delivery_checkpoint()` 同步使用该键。

为 argparse 增加 `--force-weekly`；它只绕过时间线窗口，不绕过气象校验、严格 AI、PDF 投递和同周成功检查点。人工补跑可以在 12:00 后执行：

```bash
/app/.venv/bin/python -m trendradar --force-weekly
```

- [ ] **步骤 6：运行调度与 shell 验证**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_schedule -v
bash -n docker/entrypoint.sh
bash tests/test_portable_deployment.sh
```

预期：测试 `OK`，两个 shell 命令退出码 0。

- [ ] **步骤 7：提交调度实现**

```bash
git add trendradar/__main__.py trendradar/core/scheduler.py config/timeline.yaml config/timeline.en.yaml docker/entrypoint.sh docker/.env.example docker/docker-compose.yml docker/docker-compose-build.yml tests/test_weekly_schedule.py tests/test_portable_deployment.sh
git commit -m "feat: 每周一重试生成农业周报"
```

### 任务 5：新增专用 A4 周报 PDF 模板

**文件：**
- 创建：`trendradar/report/weekly_pdf.py`
- 修改：`trendradar/report/pdf.py`
- 修改：`trendradar/report/__init__.py`
- 修改：`trendradar/__main__.py`
- 创建：`tests/test_weekly_pdf_report.py`

- [ ] **步骤 1：编写模板结构、20 条上限和 PDF 生成失败测试**

创建 `tests/test_weekly_pdf_report.py`：

```python
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytz

from trendradar.report.weekly_pdf import render_weekly_pdf_html


class WeeklyPdfReportTests(unittest.TestCase):
    def test_template_contains_reference_sections_and_all_news(self):
        items = [{
            "title": f"新闻 {index}",
            "url": f"https://example.com/{index}",
            "reader_url": f"https://search.example.com/{index}",
            "source_name": "测试来源",
            "published_at": "2026-08-09",
            "ai_summary": f"摘要 {index}",
            "highlight_rank": index if index <= 5 else None,
            "tags": ["育种技术"],
        } for index in range(1, 8)]
        weather = SimpleNamespace(
            title="全国农业气象周报",
            impact="上周农业气象影响",
            outlook="未来10天风险",
            recommendations="及时排涝散墒",
            source_url="https://www.nmc.cn/publish/agro/ten-week/index.html",
        )
        html = render_weekly_pdf_html(
            news_items=items,
            ai_analysis=SimpleNamespace(
                success=True,
                core_trends="本周核心趋势",
                sentiment_controversy="风险分类",
                signals="关注信号",
                outlook_strategy="后续建议",
            ),
            agro_weather=weather,
            period_label="2026-08-03 00:00—2026-08-10 00:00",
            generated_at=pytz.timezone("Asia/Shanghai").localize(
                datetime(2026, 8, 10, 10, 0)
            ),
        )
        for heading in (
            "核心观点摘要", "重点新闻", "入选新闻",
            "农业气象与灾害风险", "趋势与指标", "数据与方法说明",
        ):
            self.assertIn(heading, html)
        for index in range(1, 8):
            self.assertIn(f"新闻 {index}", html)
        self.assertEqual(html.count("重点标记"), 5)
        self.assertIn('href="https://example.com/1"', html)
        self.assertIn("@page", html)
        self.assertIn("size: A4", html)
```

增加 HTML 转 PDF 后文件头为 `%PDF`、中文内容非空、输出文件名正确、超过 20MB 拒绝、Chromium 缺失失败的测试。

- [ ] **步骤 2：运行测试并确认模板模块不存在**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_pdf_report -v
```

预期：导入 `trendradar.report.weekly_pdf` 失败。

- [ ] **步骤 3：实现专用打印 HTML**

创建 `trendradar/report/weekly_pdf.py`，提供：

```python
def flatten_unique_news(groups: list[dict]) -> list[dict]:
    ordered = []
    seen = set()
    for group in groups:
        for item in group.get("titles", []):
            key = canonicalize_url(item.get("url", ""))
            if not key:
                key = normalize_title(item.get("title", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(item)
    return sorted(
        ordered,
        key=lambda item: (
            int(item.get("highlight_rank") or 10**9),
            str(item.get("published_at") or ""),
            str(item.get("title") or ""),
        ),
    )
```

`render_weekly_pdf_html()` 对所有外部文本执行 HTML escape，对 HTTP(S) 链接执行安全校验。它只接收 `select_weekly_news()` 的最多 20 条结果；TOP 5 单独列出，“入选新闻”区展示同一批全部入选条目。CSS 至少包含：

```css
@page {
  size: A4;
  margin: 16mm 14mm 18mm;
  @bottom-center { content: "第 " counter(page) " 页"; }
}
body { font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
.news-card { break-inside: avoid-page; }
a { color: #155e75; text-decoration: none; word-break: break-all; }
```

不加载远程字体或图片。农业气象专栏直接使用已验证的官方结构字段，链接指向中央气象台原页。

- [ ] **步骤 4：构建文件并调用现有 Chromium 引擎**

提供：

```python
def build_weekly_pdf(
    output_dir: str,
    period_start: date,
    period_end: date,
    html: str,
) -> str:
    folder = Path(output_dir) / "pdf" / period_end.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"农业育种新闻周报_{period_start:%Y-%m-%d}至{period_end - timedelta(days=1):%Y-%m-%d}"
    html_path = folder / f"{stem}.html"
    pdf_path = folder / f"{stem}.pdf"
    html_path.write_text(html, encoding="utf-8")
    return generate_pdf_from_html(str(html_path), str(pdf_path))
```

`report/pdf.py` 在 Chromium 完成后验证文件存在、以 `%PDF` 开头、大小大于最小阈值且不超过 `20 * 1024 * 1024`；失败删除不完整输出并抛异常。

- [ ] **步骤 5：在周报分析后生成专用 PDF**

`NewsAnalyzer` 在严格 AI 成功后调用 `render_weekly_pdf_html()` 和 `build_weekly_pdf()`，保存为 `self._weekly_pdf_path`。现有网页 HTML 可继续生成用于本地查看，但不得作为 PDF 模板输入。

气象报告存在时，即使普通新闻为零，也生成包含气象专栏的 PDF；普通新闻和气象报告都为空才是失败。

- [ ] **步骤 6：运行测试并提交**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_pdf_report tests.test_weekly_report_output -v
```

预期：全部 `OK`。提交：

```bash
git add trendradar/report/weekly_pdf.py trendradar/report/pdf.py trendradar/report/__init__.py trendradar/__main__.py tests/test_weekly_pdf_report.py tests/test_weekly_report_output.py
git commit -m "feat: 生成专用农业周报PDF"
```

### 任务 6：将企业微信改为严格 PDF-only 投递

**文件：**
- 修改：`trendradar/notification/wework_pdf.py`
- 修改：`trendradar/notification/dispatcher.py`
- 修改：`trendradar/notification/senders.py`
- 修改：`trendradar/core/loader.py`
- 修改：`trendradar/__main__.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 修改：`docker/.env.example`
- 修改：`docker/docker-compose.yml`
- 修改：`docker/docker-compose-build.yml`
- 修改：`tests/test_wework_pdf.py`
- 创建：`tests/test_weekly_pdf_delivery.py`

- [ ] **步骤 1：编写“只有文件请求”失败测试**

在 `tests/test_weekly_pdf_delivery.py` 增加：

```python
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"


def response(payload, status_code=200):
    result = MagicMock(status_code=status_code)
    result.json.return_value = payload
    return result


def test_weekly_delivery_uploads_and_sends_only_one_file_message(self):
    pdf_path = Path(self.tempdir.name) / "weekly.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
    responses = [
        response({"errcode": 0, "media_id": "media-1"}),
        response({"errcode": 0}),
    ]
    with patch(
        "trendradar.notification.wework_pdf.requests.post",
        side_effect=responses,
    ) as post:
        ok = send_wework_pdf_file(WEBHOOK, str(pdf_path))
    self.assertTrue(ok)
    self.assertEqual(post.call_count, 2)
    self.assertIn("upload_media", post.call_args_list[0].args[0])
    self.assertEqual(
        post.call_args_list[1].kwargs["json"],
        {"msgtype": "file", "file": {"media_id": "media-1"}},
    )
    payloads = [call.kwargs.get("json") for call in post.call_args_list]
    self.assertFalse(any(
        payload and payload.get("msgtype") in {"text", "markdown"}
        for payload in payloads
    ))

def test_pdf_failure_never_calls_text_sender_or_records_checkpoint(self):
    scheduler = MagicMock()
    dispatcher = MagicMock()
    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    analyzer.ctx = SimpleNamespace(
        create_notification_dispatcher=MagicMock(return_value=dispatcher),
        create_scheduler=MagicMock(return_value=scheduler),
    )
    analyzer._weekly_pdf_path = None
    self.assertFalse(analyzer._deliver_weekly_pdf(self.schedule))
    dispatcher.dispatch_weekly_pdf.assert_not_called()
    scheduler.record_execution.assert_not_called()
```

`setUp()` 创建 `self.tempdir = tempfile.TemporaryDirectory()` 和 `self.schedule = schedule()`；`tearDown()` 调 `self.tempdir.cleanup()`。测试文件显式导入 `Path`、`SimpleNamespace`、`MagicMock`、`patch`、`tempfile`、`NewsAnalyzer`、`send_wework_pdf_file` 和任务 4 的 `schedule` 测试工厂；若不跨测试模块复用，则在本文件复制该十余行纯数据工厂，禁止导入测试模块产生隐式耦合。

再覆盖上传失败、file 发送失败、多个企业微信账号中任一失败、没有企业微信 Webhook、PDF 超限和同周成功后不重复发送。

- [ ] **步骤 2：运行测试并确认当前预览与文字兜底导致失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_wework_pdf tests.test_weekly_pdf_delivery -v
```

预期：当前实现发送 Markdown 预览，且失败会进入文字分批路径，因此测试失败。

- [ ] **步骤 3：收敛企业微信文件发送函数**

删除 `build_wework_pdf_preview()`、`collect_highlights()` 和 sender 内生成 PDF 的职责。保留上传与文件发送，新增：

```python
def send_wework_pdf_file(
    webhook_url: str,
    pdf_file_path: str,
    *,
    proxies: dict[str, str] | None = None,
) -> bool:
    media_id = upload_wework_file(
        webhook_url, pdf_file_path, proxies=proxies
    )
    if not send_wework_file(
        webhook_url, media_id, proxies=proxies
    ):
        return False
    print(f"企业微信 PDF 文件发送完成: {pdf_file_path}")
    return True
```

函数不捕获异常并回退文字；dispatcher 统一捕获、记录失败并返回 `False`。

- [ ] **步骤 4：新增严格 PDF-only dispatcher**

`NotificationDispatcher.dispatch_weekly_pdf()` 只解析企业微信 Webhook，多账号逐个调用 `send_wework_pdf_file()`，并要求全部成功：

```python
def dispatch_weekly_pdf(self, pdf_file_path: str, proxy_url: str = "") -> bool:
    urls = limit_accounts(
        parse_multi_account_config(self.config.get("WEWORK_WEBHOOK_URL", "")),
        self.max_accounts,
        "企业微信",
    )
    if not urls:
        print("[周报] 未配置企业微信 Webhook")
        return False
    proxies = (
        {"http": proxy_url, "https": proxy_url}
        if proxy_url else None
    )
    results = []
    for url in urls:
        try:
            results.append(send_wework_pdf_file(
                url, pdf_file_path, proxies=proxies
            ))
        except Exception as exc:
            print(f"[周报] 企业微信 PDF 发送失败: {type(exc).__name__}")
            results.append(False)
    return bool(results) and all(results)
```

周报主链只调用该入口，不调用 `dispatch_all()`，因此飞书、钉钉、邮件、普通 Webhook 和企业微信文字 sender 都不会收到周报。

- [ ] **步骤 5：成功后再写周检查点**

`NewsAnalyzer._deliver_weekly_pdf()` 使用 `window.end` 作为检查点日期：

```python
def _deliver_weekly_pdf(self, schedule) -> bool:
    if not self._weekly_pdf_path:
        return False
    dispatcher = self.ctx.create_notification_dispatcher(
        operation_at=self._operation_run_at()
    )
    if not dispatcher.dispatch_weekly_pdf(
        self._weekly_pdf_path, self.proxy_url
    ):
        return False
    checkpoint_date = self._rss_window.end.strftime("%Y-%m-%d")
    return self.ctx.create_scheduler().record_execution(
        schedule.period_key, "push", checkpoint_date
    )
```

PDF 上传或 file 消息失败时不得调用 `record_execution()`。

- [ ] **步骤 6：删除周报的失效 PDF 可选配置**

从 loader、YAML、Compose 和 `.env.example` 删除 `WEWORK_PDF_ENABLED`、`WEWORK_PDF_TOP_N`、`pdf_enabled`、`pdf_top_n`。`WEWORK_MSG_TYPE` 可为其他手工模式保留，但周报完全忽略它。

更新 `tests/test_wework_pdf.py`：删除 Markdown 预览和 TOP N 配置测试，保留 PDF 生成、上传和 file 消息 API 测试。

- [ ] **步骤 7：运行测试并提交**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_wework_pdf tests.test_weekly_pdf_delivery tests.test_weekly_schedule -v
```

预期：全部 `OK`。提交：

```bash
git add trendradar/notification/wework_pdf.py trendradar/notification/dispatcher.py trendradar/notification/senders.py trendradar/core/loader.py trendradar/__main__.py config/config.yaml config/config.en.yaml docker/.env.example docker/docker-compose.yml docker/docker-compose-build.yml tests/test_wework_pdf.py tests/test_weekly_pdf_delivery.py tests/test_weekly_schedule.py
git commit -m "feat: 企业微信仅发送周报PDF"
```

### 任务 7：清理配置器和说明文档

**文件：**
- 修改：`docs/index.html`
- 修改：`docs/assets/script.js`
- 修改：`docs/assets/i18n.js`
- 修改：`docs/news-push-technical-implementation.md`
- 修改：`config/daily.crontab`
- 修改：`tests/test_weekly_time_rule.py`
- 修改：`tests/test_portable_deployment.sh`

- [ ] **步骤 1：扩大静态清理测试**

在 `tests/test_weekly_time_rule.py` 增加：

```python
def test_runtime_and_user_docs_have_no_obsolete_delivery_terms(self):
    paths = (
        "trendradar", "config", "docker", "docs/index.html",
        "docs/assets/script.js", "docs/assets/i18n.js",
        "docs/news-push-technical-implementation.md",
    )
    forbidden = (
        "freshness_filter", "max_age_days", "when:2d",
        '"timespan": "48h"', "每日新增推送",
        "WEWORK_PDF_TOP_N", "WEWORK_PDF_ENABLED",
    )
    for relative in paths:
        path = ROOT / relative
        files = [path] if path.is_file() else list(path.rglob("*"))
        text = "\n".join(
            file.read_text(encoding="utf-8", errors="ignore")
            for file in files if file.is_file()
        )
        for token in forbidden:
            self.assertNotIn(token, text, f"{relative}: {token}")
```

该测试不扫描当前规格和计划，因为文档需要明确说明被删除的旧规则。

- [ ] **步骤 2：运行测试并确认网页配置器和说明仍有旧字段**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_time_rule -v
```

预期：网页配置器、技术文档或环境示例中的旧字段导致失败。

- [ ] **步骤 3：更新用户可见配置与文档**

删除网页配置器中的 freshness、per-feed max age、PDF enabled 和 PDF top N 控件、序列化字段与 i18n 文案。技术实现文档改为以下准确流程：

```text
每天 10:00 静默采集 → 周一验证当期全国农业气象周报 →
上一自然周 published_at 唯一过滤 → 严格 AI → 专用 A4 PDF →
企业微信 upload_media → 企业微信 file 消息 → 周成功检查点
```

`config/daily.crontab` 注释改为“每天静默采集，周一生成上一自然周 PDF；周一 10:30—12:00 为气象周报重试”。同步更新 `tests/test_portable_deployment.sh` 的精确文案和 `CRON_SCHEDULES` 断言。

- [ ] **步骤 4：运行静态与可移植测试并提交**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_time_rule -v
bash tests/test_portable_deployment.sh
git diff --check
```

预期：全部退出码 0。提交：

```bash
git add docs/index.html docs/assets/script.js docs/assets/i18n.js docs/news-push-technical-implementation.md config/daily.crontab tests/test_weekly_time_rule.py tests/test_portable_deployment.sh
git commit -m "docs: 更新每周PDF推送配置说明"
```

### 任务 8：分层回归、缓存迁移和一次真实补跑

**文件：**
- 修改：`docs/superpowers/plans/2026-08-10-weekly-pdf-delivery.md`（只勾选完成项和记录验证结果）
- 运行时迁移：`output` 整目录备份
- 运行时配置：`docker/.env` 只更新 `CRON_SCHEDULES`

- [x] **步骤 1：运行聚焦回归**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest \
    tests.test_weekly_time_rule \
    tests.test_sciencedirect_rss_dates \
    tests.test_news_search \
    tests.test_news_search_pipeline \
    tests.test_weekly_digest \
    tests.test_agro_weather \
    tests.test_weekly_schedule \
    tests.test_weekly_pdf_report \
    tests.test_wework_pdf \
    tests.test_weekly_pdf_delivery -v
```

预期：全部 `OK`，退出码 0。

- [x] **步骤 2：运行兼容和全量回归**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_elsevier_fulltext tests.test_direct_first_proxy tests.test_email_multi_recipient -v

docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest discover -s /workspace/tests -v
```

预期：两个容器明确退出码 0，无 `FAIL` 或 `ERROR`。

- [x] **步骤 3：运行静态与 PDF 实际生成验证**

```bash
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
git diff --check

docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_pdf_report.PdfChromiumIntegrationTests -v
```

预期：全部退出码 0，集成测试生成有效 `%PDF` 文件。

- [x] **步骤 4：提交最终测试修正和计划记录**

```bash
git add tests docs/superpowers/plans/2026-08-10-weekly-pdf-delivery.md
git commit -m "test: 验证每周PDF交付链路"
```

验证记录（2026-08-10，专用镜像
`trendradar-task8-verify:7b97a5d0`，只读工作树，`--network none`）：

- 聚焦回归：240 项通过，`OK`，退出码 0（修正后复验 287.498 秒）。
- 兼容回归：计划中的 `tests.test_elsevier_fulltext` 和
  `tests.test_email_multi_recipient` 并不存在；使用仓库实际模块
  `tests.test_elsevier_full_text`、`tests.test_direct_first_proxy`、
  `tests.test_email_delivery` 后 36 项通过，`OK`，退出码 0。
- 全量回归：首轮 603 项发现一条仍断言旧每日推送时间线的测试，且
  多进程锁子进程发生一次性 `SemLock` 启动错误；更新旧时间线期望后，
  定向 2 项、聚焦 240 项均通过，第二轮全量 603 项通过，`OK`，
  退出码 0（1018.092 秒）。
- 静态验证：两项 `bash -n`、可移植部署检查和 `git diff --check`
  均退出码 0。
- PDF 实际生成：计划中的 `PdfChromiumIntegrationTests` 并不存在；
  运行实际多页用例
  `WeeklyPdfGenerationValidationTests.test_actual_chromium_output_is_a4_multipage_with_repeated_chinese_furniture`
  后 1 项通过，`OK`，退出码 0。用例校验了 `%PDF`、A4、多页、中文
  页眉页码以及 `pdfinfo`/`pdftotext` 输出。

- [ ] **步骤 5：合并前只读审查**

审查从规格提交到当前 HEAD 的完整差异，重点确认：

- 普通新闻只有上一自然周一套资格规则；
- 气象周报是唯一显式产品周期例外；
- 周一重试不会重复抓取或重复发送成功周报；
- PDF 模板最多收录 20 条严格相关普通新闻，TOP 5 固定，其余名额主题均衡；
- 周报只进入企业微信文件 API；
- 任一失败不会写成功检查点；
- 没有 `.env`、API Key、Webhook、输出缓存或无关用户文件进入提交。

发现 Critical/Important 问题时先补 RED 测试、最小修复、重跑聚焦与全量，再提交；没有这两级问题才允许合并。

- [ ] **步骤 6：停止服务并可恢复地清空旧缓存**

在主工作树合并通过后执行：

```bash
docker compose -f docker/docker-compose.yml stop trendradar trendradar-mcp
```

确认两个容器均已停止，确认 `output.backup-20260810-weekly-pdf` 不存在，然后把 `output` 同盘移动到该明确备份名并创建空 `output`。不得使用 `rm`，不得运行 `docker compose down -v`，不得修改本地 `.venv`。

备份后核对原文件数量和大小仍可读取；回滚时停止服务，把新 `output` 移为 `output.failed-20260810-weekly-pdf`，再把备份恢复为 `output`。

- [ ] **步骤 7：只更新调度变量并重建镜像**

使用 `apply_patch` 只把 `docker/.env` 中的调度行更新为：

```text
CRON_SCHEDULES=0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1
```

不得打印 `.env` 完整内容；只用返回 `SET/UNSET` 的检查验证 API Key、企业微信 Webhook、Elsevier Key/Insttoken 和代理变量仍存在。

```bash
docker compose -f docker/docker-compose.yml up -d --build --force-recreate
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs --tail=120 trendradar
```

预期：服务健康，supercronic 显示五类触发点，Web 服务可访问，未自动发送文字消息。

- [ ] **步骤 8：执行一次真实周报补跑并验收**

```bash
docker compose -f docker/docker-compose.yml run --rm \
  --entrypoint /app/.venv/bin/python trendradar \
  -m trendradar --force-weekly
```

持续观察到进程明确退出。验收：

- 官方农业气象周报的报告日期和回顾周期通过校验；
- 普通新闻发布时间全部属于上一自然周；
- PDF 文件位于 `output/pdf/<本周一>/`，文件名、大小和 `%PDF` 正确；
- PDF 包含最多 20 条入选新闻、TOP 5、气象专栏、方法说明和可点击链接；
- 企业微信日志只有 `upload_media` 与 `file` 成功，没有 Markdown/text；
- `monday_weekly/push/<本周一>` 成功检查点存在；
- 立即再次运行 `--force-weekly` 会在任何外部请求前幂等跳过。

若真实发送失败，保留新 `output` 供诊断且不写检查点；修复后再次补跑。只有无法在当前授权范围修复时才执行步骤 6 的缓存回滚。

### 任务 9：执行 2026-08-03 至 2026-08-09 初始化补采

**文件：**
- 临时创建：`/tmp/trendradar-initial-week-backfill.py`（不进入仓库）
- 临时创建：`/tmp/test-trendradar-initial-week-backfill.py`（不进入仓库）
- 修改：`docs/superpowers/plans/2026-08-10-weekly-pdf-delivery.md`（只记录执行结果）
- 运行时备份：`output.pre-initial-backfill-20260810/`

该任务不修改正式周报生产代码。临时入口只在当前 Python 进程中替换
`WeeklyRSSAggregator`：它读取本次运行写入的 `2026-08-10` 日库，按正式
`NaturalWeekWindow`、`item_identity` 和 `item_richness` 构造尽力恢复快照，
并复用正式严格 AI、气象校验、PDF 和逐账号投递账本。正常
`--force-weekly` 仍要求八个完整日库。

- [ ] **步骤 1：编写并运行临时入口的失败测试**

测试必须覆盖固定操作令牌、窗口外排除、canonical 去重、富信息条目优先、
允许 ID 只来自保留条目，以及 PDF 周期标签包含“初始化补采”。先只创建
`/tmp/test-trendradar-initial-week-backfill.py`，内容为：

```python
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

import pytz

from trendradar.storage.base import RSSData, RSSItem


MODULE_PATH = Path("/operator/trendradar-initial-week-backfill.py")
SPEC = importlib.util.spec_from_file_location("initial_backfill", MODULE_PATH)
BACKFILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKFILL)


class FakeStorage:
    def __init__(self):
        self.data = RSSData(
            date="2026-08-10",
            crawl_time="2026-08-10 10:00:00",
            items={
                "feed-a": [
                    RSSItem(
                        title="A short",
                        feed_id="feed-a",
                        url="https://example.com/a?utm_source=x",
                        published_at="2026-08-04",
                    ),
                    RSSItem(
                        title="A richer",
                        feed_id="feed-a",
                        url="https://example.com/a",
                        published_at="2026-08-04T08:00:00+08:00",
                        summary="完整摘要",
                    ),
                    RSSItem(
                        title="Outside",
                        feed_id="feed-a",
                        url="https://example.com/outside",
                        published_at="2026-08-02",
                    ),
                ],
            },
            id_to_name={"feed-a": "来源 A"},
            failed_ids=[],
        )

    def get_rss_data_strict(self, date):
        return self.data if date == "2026-08-10" else None

    def get_all_rss_ids_strict(self, date):
        return [{
            "id": 42,
            "source_id": "feed-a",
            "source_name": "来源 A",
            "url": "https://example.com/a",
            "title": "A richer",
        }]


class InitialBackfillTests(unittest.TestCase):
    def test_guard_rejects_any_other_operation(self):
        with self.assertRaises(ValueError):
            BACKFILL.validate_ack("2026-08-02_2026-08-08")

    def test_builds_one_deduplicated_window_snapshot(self):
        now = pytz.timezone("Asia/Shanghai").localize(
            datetime(2026, 8, 10, 10, 0)
        )
        snapshot = BACKFILL.build_initial_snapshot(
            FakeStorage(), now, "Asia/Shanghai"
        )
        retained = list(snapshot.iter_items())
        self.assertEqual([item.title for item in retained], ["A richer"])
        self.assertEqual(snapshot.allowed_rss_ids, {42})
        self.assertEqual(snapshot.total_read, 3)
        self.assertEqual(snapshot.filtered_out, 1)
        self.assertEqual(snapshot.duplicate_count, 1)
        self.assertEqual(
            BACKFILL.initial_period_label(snapshot.window.label),
            "初始化补采｜2026-08-03—2026-08-09",
        )


if __name__ == "__main__":
    unittest.main()
```

再运行：

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v /tmp:/operator:ro \
  --entrypoint /app/.venv/bin/python trendradar \
  /operator/test-trendradar-initial-week-backfill.py
```

预期：因 `/operator/trendradar-initial-week-backfill.py` 尚不存在而失败，退出码
非 0；不得发起网络请求。

- [ ] **步骤 2：实现隔离的一次性聚合入口**

创建 `/tmp/trendradar-initial-week-backfill.py`，核心接口固定如下：

```python
BACKFILL_TOKEN = "2026-08-03_2026-08-09"
SOURCE_DATE = "2026-08-10"


def validate_ack(value):
    if value != BACKFILL_TOKEN:
        raise ValueError("初始化补采操作令牌不匹配")


def initial_period_label(label):
    return f"初始化补采｜{label}"


def build_initial_snapshot(storage, now, timezone_name):
    window = previous_natural_week(now, timezone_name)
    if window.label != "2026-08-03—2026-08-09":
        raise RuntimeError("初始化补采窗口不匹配")
    source = storage.get_rss_data_strict(SOURCE_DATE)
    if source is None or source.failed_ids:
        raise RuntimeError("初始化补采源快照不存在或来源失败")

    deduplicated = {}
    total_read = filtered_out = duplicate_count = 0
    for items in source.items.values():
        for item in items:
            total_read += 1
            if not window.contains(item.published_at):
                filtered_out += 1
                continue
            identity = item_identity(item)
            if not identity:
                filtered_out += 1
                continue
            current = deduplicated.get(identity)
            if current is None:
                deduplicated[identity] = replace(item)
            else:
                duplicate_count += 1
                if item_richness(item) > item_richness(current):
                    deduplicated[identity] = replace(item)

    grouped = {}
    id_to_name = {}
    for item in sorted(
        deduplicated.values(),
        key=lambda value: (value.published_at, value.feed_id, value.title),
    ):
        grouped.setdefault(item.feed_id, []).append(item)
        id_to_name[item.feed_id] = item.feed_name or item.feed_id
    data = RSSData(
        date=SOURCE_DATE,
        crawl_time=now.isoformat(),
        items=grouped,
        id_to_name=id_to_name,
        failed_ids=[],
    )
    resolver = WeeklyRSSAggregator(storage, timezone_name)
    return WeeklyRSSSnapshot(
        window=window,
        data=data if deduplicated else None,
        allowed_rss_ids=resolver._resolve_allowed_ids(data) if deduplicated else set(),
        total_read=total_read,
        filtered_out=filtered_out,
        duplicate_count=duplicate_count,
    )
```

入口还必须：

1. 只接受命令行 `--ack 2026-08-03_2026-08-09`；
2. 在进程内把 `trendradar.__main__.WeeklyRSSAggregator` 替换成仅调用
   `build_initial_snapshot()` 的适配器；
3. 包装 `trendradar.__main__.render_weekly_pdf_html()`，把传入的
   `period_label` 改为 `初始化补采｜2026-08-03—2026-08-09`；
4. 调用 `NewsAnalyzer(config=load_config(), force_weekly=True).run()`；
5. 只有 `run()` 返回真才以退出码 0 结束，任何异常或假值均退出 1。

- [ ] **步骤 3：运行临时入口单元测试并做无外呼预检**

```bash
docker compose -f docker/docker-compose.yml run --rm --network none \
  -v /tmp:/operator:ro \
  --entrypoint /app/.venv/bin/python trendradar \
  /operator/test-trendradar-initial-week-backfill.py
```

预期：全部 `OK`、退出码 0。随后只读检查正式源码没有新增 backfill 分支，
临时脚本没有读取或输出 Webhook、API Key 等秘密。

- [ ] **步骤 4：停止常驻任务并创建可恢复运行时备份**

```bash
docker compose -f docker/docker-compose.yml stop trendradar trendradar-mcp
```

确认没有正在运行的 TrendRadar Python 任务、目标备份目录不存在，再把
`output` 同盘重命名为 `output.pre-initial-backfill-20260810`，复制其内容到新建
`output`。不删除任何目录，不修改 `docker/.env`。运行前记录两个目录的文件
数量和总大小；若外呼尚未成功而任务失败，停止任务后恢复原备份。

- [ ] **步骤 5：执行唯一一次真实 PDF 补推**

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v /tmp:/operator:ro \
  --entrypoint /app/.venv/bin/python trendradar \
  /operator/trendradar-initial-week-backfill.py \
  --ack 2026-08-03_2026-08-09
```

持续观察到进程明确退出。不得同时启动第二个补推进程。

- [ ] **步骤 6：验证实际交付并恢复常驻服务**

验证必须同时满足：

- 日志显示固定 RSS 与所有启用的新闻搜索供应商成功；
- 所有 allowed RSS 条目的 `published_at` 均属于
  `[2026-08-03 00:00, 2026-08-10 00:00)`；
- 严格 AI 成功，普通新闻不超过 20 条；
- 官方气象周报校验成功；
- PDF 文件头、A4 页数、中文文本和文件大小有效，且正文出现“初始化补采”；
- 企业微信只执行 PDF 上传与 `file` 发送；所有账号成功；
- `monday_weekly/push/2026-08-10` 和逐账号 PDF 摘要账本存在；
- 用相同命令再次做只读幂等验证时，在气象、新闻和企业微信外呼前跳过。

成功后删除 `/tmp` 中两个临时脚本，保留运行时备份直至用户确认效果；然后：

```bash
docker compose -f docker/docker-compose.yml up -d trendradar trendradar-mcp
docker compose -f docker/docker-compose.yml ps
```

若发送前失败，恢复步骤 4 的备份后启动服务；若企业微信已经成功但本地账本
写入失败，禁止自动重试，保留现场并人工核对，避免重复发送。
