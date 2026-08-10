# 前一自然日新闻推送实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 当前启用的每日推送链每天北京时间 10:00 只推送前一自然日 `[昨日 00:00, 今日 00:00)` 发布的农业育种新闻，删除该链路全部旧滚动时间规则，并在清空旧运行缓存后立即补跑 2026-08-09。

**架构：** 运行入口冻结一次 `run_at` 并生成唯一 `DailyDeliveryWindow`；同一个窗口用于新闻搜索查询边界和最终 RSS 快照筛选。RSS 抓取、搜索聚合、AI 和报告转换不再执行 `24h/48h/max_age_days` 二次裁剪；checkpoint 只负责同日幂等，first-seen 只作为存储账本保留。部署时把整个 `output` 原子移动到可恢复备份，再用原 `.env` 重建并显式补跑。

**技术栈：** Python 3.12、`unittest`、SQLite、`pytz`、Docker Compose、容器内锁定的 `uv` 环境

---

## 文件结构

- 修改 `trendradar/core/daily_delivery.py`：前一自然日窗口、发布时间解析、昨日库与今日库聚合。
- 修改 `trendradar/crawler/rss/parser.py`：ScienceDirect 与 JSON Feed 纯日期保持日精度，标准 RSS 时间明确为 UTC。
- 修改 `trendradar/utils/time.py`：纯日期显示不再漂移为 UTC 午夜后的 08:00。
- 修改 `trendradar/crawler/rss/fetcher.py`：删除 `freshness_enabled`、`max_age_days` 及抓取前滚动过滤。
- 修改 `trendradar/crawler/news_search.py`：删除聚合器滚动 24 小时和 recency 打分；搜索客户端接收唯一自然日窗口。
- 修改 `trendradar/ai/filter_pipeline.py`：删除 RSS freshness 配置和二次判断，只保留权威 ID 或报告窗口范围。
- 修改 `trendradar/__main__.py`：删除 feed/报告 freshness 路径，接入唯一窗口，解除内容范围与 latest checkpoint 耦合。
- 修改 `trendradar/core/loader.py`、`trendradar/context.py`、`trendradar/utils/time.py`：删除旧 freshness 配置加载与无调用时间工具。
- 修改 `config/config.yaml`、`config/config.en.yaml`：删除 `freshness_filter` 和所有 `max_age_days`。
- 修改 `docs/index.html`、`docs/assets/script.js`、`docs/assets/i18n.js`、`docs/news-push-technical-implementation.md`：删除会生成或说明旧 freshness 字段的网页配置器与文档内容。
- 修改 `config/timeline.yaml`、`trendradar/notification/splitter.py`、`trendradar/notification/renderer.py`、`trendradar/notification/senders.py`、`trendradar/report/html.py`：显示“昨日新闻”。
- 创建 `tests/test_previous_day_time_rule.py`：集中证明生产代码和配置仅保留自然日交付规则。
- 修改 `tests/test_sciencedirect_rss_dates.py`、`tests/test_daily_delivery.py`、`tests/test_daily_delivery_schedule.py`：自然日边界、双日库、checkpoint 幂等。
- 修改 `tests/test_news_search.py`、`tests/test_news_search_pipeline.py`：搜索查询复用自然日窗口，不再有独立 24/48 小时。
- 修改 `tests/test_daily_delivery_review3.py`、`tests/test_daily_delivery_review5.py`、`tests/test_daily_delivery_review6.py`：更新聚合器签名及跨午夜契约。
- 修改 `tests/test_daily_delivery_report.py`、`tests/test_weekly_configuration.py`：更新显示与配置断言。
- 删除 `tests/test_rss_strict_freshness.py`：被删除功能的旧测试不再保留。
- 保留 `docs/superpowers/specs/2026-08-10-previous-calendar-day-delivery-design.md` 与本计划；`docs/superpowers` 下更早文档全部删除。

### 任务 1：删除旧 RSS freshness 规则

**文件：**
- 创建：`tests/test_previous_day_time_rule.py`
- 修改：`trendradar/crawler/rss/fetcher.py:18-109,145-160,272-338`
- 修改：`trendradar/core/loader.py:287-310`
- 修改：`trendradar/context.py:12-21`
- 修改：`trendradar/utils/time.py:223-308`
- 修改：`config/config.yaml:106-367`
- 修改：`config/config.en.yaml:107-151`
- 修改：`docs/index.html:245-270`
- 修改：`docs/assets/script.js:918-928,3034-3130`
- 修改：`docs/assets/i18n.js:80-90,280-290`
- 修改：`docs/news-push-technical-implementation.md:40-70`
- 删除：`tests/test_rss_strict_freshness.py`
- 测试：`tests/test_weekly_configuration.py`
- 修改：其余测试夹具中仅为旧配置兼容而存在的 `FRESHNESS_FILTER/freshness_filter/max_age_days` 字段

- [ ] **步骤 1：编写失败的旧规则清理测试**

创建 `tests/test_previous_day_time_rule.py`：

```python
import unittest
from pathlib import Path

import yaml

from trendradar.core.loader import _load_rss_config
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher


ROOT = Path(__file__).resolve().parents[1]


class LegacyTimeRuleRemovalTests(unittest.TestCase):
    def test_runtime_rss_config_has_no_freshness_filter(self):
        loaded = _load_rss_config({"rss": {"enabled": True, "feeds": []}})

        self.assertNotIn("FRESHNESS_FILTER", loaded)

    def test_yaml_has_no_freshness_or_per_feed_age_rules(self):
        for relative_path in ("config/config.yaml", "config/config.en.yaml"):
            with self.subTest(path=relative_path):
                raw = yaml.safe_load(
                    (ROOT / relative_path).read_text(encoding="utf-8")
                )
                rss = raw["rss"]
                self.assertNotIn("freshness_filter", rss)
                self.assertTrue(all(
                    "max_age_days" not in feed
                    for feed in rss.get("feeds", [])
                ))

    def test_fetcher_api_has_no_rolling_age_options(self):
        feed_fields = RSSFeedConfig.__dataclass_fields__
        self.assertNotIn("max_age_days", feed_fields)
        fetcher = RSSFetcher([])
        self.assertFalse(hasattr(fetcher, "freshness_enabled"))
        self.assertFalse(hasattr(fetcher, "default_max_age_days"))

    def test_docs_and_configurator_do_not_emit_removed_age_fields(self):
        for relative_path in (
            "docs/index.html",
            "docs/assets/script.js",
            "docs/assets/i18n.js",
            "docs/news-push-technical-implementation.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("freshness_filter", text)
                self.assertNotIn("max_age_days", text)
```

把 `tests/test_weekly_configuration.py` 中 freshness/max-age 断言改为上述“键不存在”契约。

- [ ] **步骤 2：运行测试并确认旧实现失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_previous_day_time_rule tests.test_weekly_configuration -v
```

预期：`FRESHNESS_FILTER`、`freshness_filter`、`max_age_days` 和 fetcher 属性断言失败。

- [ ] **步骤 3：删除抓取层滚动过滤**

把 `RSSFeedConfig` 收敛为：

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

`RSSFetcher.__init__()` 删除 `freshness_enabled` 和 `default_max_age_days` 参数及属性；删除 `_is_item_fresh()`。`fetch_feed()` 在 `max_items` 截断后直接转换全部 `parsed_items`，不再执行以下旧阶段：

```python
before_freshness_count = len(parsed_items)
parsed_items = [
    parsed for parsed in parsed_items
    if self._is_item_fresh(feed, parsed.published_at, now)
]
```

`RSSFetcher.from_config()` 不读取 `freshness_filter/max_age_days`，只构造上述字段并向构造器传网络、时区和 feed 参数。

- [ ] **步骤 4：删除加载器、配置和时间工具中的旧规则**

`_load_rss_config()` 返回值改为：

```python
return {
    "ENABLED": rss.get("enabled", False),
    "REQUEST_INTERVAL": advanced_rss.get("request_interval", 2000),
    "TIMEOUT": advanced_rss.get("timeout", 15),
    "USE_PROXY": advanced_rss.get("use_proxy", False),
    "PROXY_URL": rss_proxy_url,
    "FEEDS": feeds,
    "NEWS_SEARCH": runtime_news_search,
}
```

从中英文配置删除整个 `freshness_filter` 区块、说明和每个 feed 的 `max_age_days`。从
`utils/time.py` 删除仅用于旧 freshness 的 `is_within_days()`、`calculate_days_old()`；从
`context.py` 删除对应 import。网页配置器同时删除全局 freshness 控件、RSS 弹窗中的最大年龄
输入、feed YAML 序列化/反序列化字段及对应 i18n；技术实现文档的新增源示例只保留
`max_items`，不再指导用户写入无效时间字段。
同步清理每日、weekly、ScienceDirect、Rice Science 和新闻搜索测试夹具中的旧字段；只有
`test_previous_day_time_rule.py` 可保留这些字面量用于断言仓库已删除对应配置。

- [ ] **步骤 5：删除旧测试并运行新契约**

用补丁删除 `tests/test_rss_strict_freshness.py`，运行任务 1 步骤 2 的命令。

预期：全部 `OK`，退出码 0。

- [ ] **步骤 6：提交旧规则清理**

```bash
git add config/config.yaml config/config.en.yaml docs/index.html docs/assets/script.js docs/assets/i18n.js docs/news-push-technical-implementation.md trendradar/core/loader.py trendradar/context.py trendradar/crawler/rss/fetcher.py trendradar/utils/time.py tests/test_previous_day_time_rule.py tests/test_weekly_configuration.py tests/test_rss_strict_freshness.py
git commit -m "refactor: 删除旧RSS滚动时间规则"
```

### 任务 2：保留发布日期精度并建立唯一自然日窗口

**文件：**
- 修改：`trendradar/crawler/rss/parser.py:12-14,189-203,278-300,318-346`
- 修改：`trendradar/core/daily_delivery.py:1-99`
- 修改：`trendradar/utils/time.py:144-185`
- 测试：`tests/test_sciencedirect_rss_dates.py`
- 测试：`tests/test_daily_delivery.py:70-170`

- [ ] **步骤 1：编写失败的日期与窗口边界测试**

在 `tests/test_sciencedirect_rss_dates.py` 使用：

```python
def test_sciencedirect_fallback_preserves_date_precision(self):
    items = RSSParser().parse(
        _rss_item(
            "<p>Publication date: Available online 9 August 2026</p>"
        ),
        "https://rss.sciencedirect.com/publication/science/22145141",
    )
    self.assertEqual(items[0].published_at, "2026-08-09")


def test_standard_rss_struct_time_is_explicit_utc(self):
    value = RSSParser()._parse_date({
        "published_parsed": (2026, 8, 9, 1, 2, 3, 0, 0, 0),
    })
    self.assertEqual(value, "2026-08-09T01:02:03+00:00")


def test_json_feed_date_only_preserves_date_precision(self):
    self.assertEqual(
        RSSParser()._parse_iso_date("2026-08-09"),
        "2026-08-09",
    )


def test_naive_full_timestamps_are_normalized_to_explicit_utc(self):
    parser = RSSParser()
    self.assertEqual(
        parser._parse_iso_date("2026-08-09T01:02:03"),
        "2026-08-09T01:02:03+00:00",
    )


def test_date_only_display_does_not_invent_a_time(self):
    self.assertEqual(
        format_iso_time_friendly("2026-08-09", "Asia/Shanghai", True),
        "08-09",
    )
    self.assertEqual(
        parser._parse_date({"published": "Sun, 09 Aug 2026 01:02:03"}),
        "2026-08-09T01:02:03+00:00",
    )
```

在 `DailyDeliveryWindowTests` 中替换滚动/checkpoint 用例：

```python
def test_previous_day_uses_local_midnight_left_closed_right_open(self):
    window = daily_delivery_window(
        shanghai(2026, 8, 10, 10, 0), "Asia/Shanghai"
    )
    self.assertEqual(window.start, shanghai(2026, 8, 9, 0, 0))
    self.assertEqual(window.end, shanghai(2026, 8, 10, 0, 0))
    self.assertEqual(window.storage_dates, ["2026-08-09", "2026-08-10"])
    self.assertTrue(window.contains(shanghai(2026, 8, 9, 0, 0)))
    self.assertTrue(window.contains(shanghai(2026, 8, 9, 23, 59, 59)))
    self.assertFalse(window.contains(shanghai(2026, 8, 10, 0, 0)))


def test_publication_precision_and_timezone_boundaries(self):
    window = daily_delivery_window(
        shanghai(2026, 8, 10, 10, 0), "Asia/Shanghai"
    )
    expected = {
        "2026-08-09": True,
        "2026-08-08": False,
        "2026-08-08T16:00:00Z": True,
        "2026-08-08T15:59:59Z": False,
        "2026-08-09T15:59:59Z": True,
        "2026-08-09T16:00:00Z": False,
        "": False,
        "not-a-date": False,
        "2026-08-10": False,
    }
    for value, included in expected.items():
        with self.subTest(value=value):
            self.assertIs(window.contains_published(value), included)
```

- [ ] **步骤 2：运行测试并确认失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_sciencedirect_rss_dates tests.test_daily_delivery.DailyDeliveryWindowTests -v
```

预期：日期精度、新函数签名和左闭右开断言失败。

- [ ] **步骤 3：实现日期精度**

`parser.py` 使用：

```python
from datetime import datetime, timezone as datetime_timezone

# feedparser 的 *_parsed 是 UTC struct。
dt = datetime(*date_struct[:6], tzinfo=datetime_timezone.utc)
return dt.isoformat()
```

ScienceDirect 描述回退使用：

```python
parsed = datetime.strptime(match.group(1), date_format)
return parsed.date().isoformat()
```

`_parse_iso_date()` 先识别严格的 `YYYY-MM-DD` 并原样返回；只有包含时间的 ISO 值才通过
`datetime.fromisoformat()` 规范化，避免 JSON Feed 的日期精度被扩成午夜时间。ISO 或 RFC
完整时间若没有 offset，明确按 UTC 补 `tzinfo=datetime_timezone.utc`；所有非纯日期输出必须
带时区，不能把 naive 值交给下游自行猜测。

`format_iso_time_friendly()` 对严格 `YYYY-MM-DD` 且 `include_date=True` 时直接输出 `MM-DD`，
不把日期补成 UTC 午夜后再转换出虚假的 `08:00`。

- [ ] **步骤 4：实现唯一自然日窗口**

`daily_delivery.py` 使用：

```python
_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def publication_local_date(value: str, timezone: str) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    if _DATE_ONLY_PATTERN.fullmatch(text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None
    parsed = parse_iso_datetime(text, timezone)
    return parsed.date() if parsed is not None else None


def daily_delivery_window(now: datetime, timezone: str) -> DailyDeliveryWindow:
    local_now = normalize_delivery_datetime(now, timezone)
    tz = pytz.timezone(timezone)
    end_date = local_now.date()
    start_date = end_date - timedelta(days=1)
    return DailyDeliveryWindow(
        start=tz.localize(datetime.combine(start_date, time.min)),
        end=tz.localize(datetime.combine(end_date, time.min)),
        timezone=timezone,
    )
```

`DailyDeliveryWindow.contains()` 为 `start <= local_value < end`；`contains_published()` 只接受 `publication_local_date(value) == start.date()`；`storage_dates` 精确返回昨日和今日。删除 `parse_discovered_at()`、`contains_discovered()` 和 `checkpoint` 参数。

- [ ] **步骤 5：运行测试并提交**

运行任务 2 步骤 2 命令，预期全部 `OK`，然后：

```bash
git add trendradar/crawler/rss/parser.py trendradar/core/daily_delivery.py trendradar/utils/time.py tests/test_sciencedirect_rss_dates.py tests/test_daily_delivery.py tests/test_daily_delivery_report.py
git commit -m "feat: 定义前一自然日发布时间窗口"
```

### 任务 3：让新闻搜索复用同一个自然日窗口

**文件：**
- 修改：`trendradar/crawler/news_search.py:186-230,261-290,379-394,451-487,509-596`
- 修改：`trendradar/__main__.py:1361-1415`
- 测试：`tests/test_news_search.py`
- 测试：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写失败的搜索窗口与无滚动过滤测试**

在 `tests/test_news_search.py` 替换 48h/2d 请求断言：

```python
def test_gdelt_query_is_safe_superset_derived_from_delivery_window(self):
    start = datetime(2026, 8, 8, 16, tzinfo=timezone.utc)
    end = datetime(2026, 8, 9, 16, tzinfo=timezone.utc)
    params = GDELTClient().build_params("wheat breeding", 25, start, end)
    self.assertNotIn("timespan", params)
    self.assertEqual(params["startdatetime"], "20260808155959")
    self.assertEqual(params["enddatetime"], "20260809160000")


def test_google_query_is_safe_superset_derived_from_delivery_dates(self):
    shanghai = timezone(timedelta(hours=8))
    start = datetime(2026, 8, 9, 0, tzinfo=shanghai)
    end = datetime(2026, 8, 10, 0, tzinfo=shanghai)
    params = GoogleNewsRSSClient().build_params(
        "水稻 基因编辑", "zh", start, end
    )
    self.assertNotIn("when:2d", params["q"])
    self.assertIn("after:2026-08-08", params["q"])
    self.assertIn("before:2026-08-10", params["q"])


def test_aggregate_does_not_apply_any_date_eligibility_filter(self):
    coordinator = AgriculturalNewsSearch()
    result = coordinator.aggregate([
        article("Old candidate", "2026-07-01T00:00:00+00:00"),
        article("Future candidate", "2026-09-01T00:00:00+00:00"),
        article("Undated candidate", ""),
        article("Invalid-date candidate", "not-a-date"),
    ])
    self.assertEqual(
        [item.title for item in result],
        [
            "Old candidate",
            "Future candidate",
            "Undated candidate",
            "Invalid-date candidate",
        ],
    )
```

在 `tests/test_news_search_pipeline.py` 断言 daily 调用 `search(window.start, window.end)`，且搜索器构造参数不再有独立 recency 开关。

- [ ] **步骤 2：运行测试并确认失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_news_search tests.test_news_search_pipeline -v
```

预期：旧 `timespan: 48h`、`when:2d`、聚合器 24 小时过滤和旧 `search()` 签名导致失败。

- [ ] **步骤 3：删除搜索聚合器独立时间规则**

`aggregate()` 不再判断发布时间是否缺失、非法、未来或过旧；它只做 URL/标题结构校验和去重。
GDELT/Google provider parser 同样只要求安全 URL 与标题，无法解析的发布时间保存为空字符串，
由最终 `DailyDeliveryAggregator` 唯一排除并统计。聚合阶段不计算 age，也不以 24 小时 recency
打分：

```python
pre_hot_score = round(
    0.6 * coverage + 0.4 * authority,
    4,
)
```

删除 `now_func`、`_current_time()`、`aggregate(..., now=...)` 和 `age_hours/recency`。相应评分测试更新为 coverage+authority 的确定值。

- [ ] **步骤 4：把同一窗口传入搜索客户端**

搜索协调器允许显式窗口或完全不加上游时间参数，但不再提供滚动默认值：

```python
def search(
    self,
    start: datetime | None = None,
    end: datetime | None = None,
) -> NewsSearchResult: ...

def GDELTClient.build_params(
    self,
    query: str,
    max_results: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict: ...

def GoogleNewsRSSClient.build_params(
    self,
    query: str,
    language: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict: ...
```

`start/end` 必须同时提供或同时省略；省略时只是不加供应商时间参数，不能回退为
`48h/when:2d`。GDELT 的 `startdatetime` 使用 `start` 转 UTC 后减一秒，`enddatetime` 使用
`end` 转 UTC；这是因为供应商起点语义为严格 after，保护秒只扩大召回，不改变最终左闭边界。
Google 的 `after:` 使用 `start` 的本地日期减一天，`before:` 使用 `end` 的本地日期，形成
日粒度召回超集。`fetch()` 和 `run_queries()` 原样转发 start/end；只有每日聚合器拥有内容
资格判定权。

`__main__.py` 在搜索前构造一次：

```python
delivery_window = (
    daily_delivery_window(self._operation_run_at(), timezone)
    if self.report_mode == "daily_delivery"
    else None
)
```

当前已启用时间线只有 `daily_delivery`；该模式调用：

```python
search_result = news_search.search(
    delivery_window.start,
    delivery_window.end,
)
```

其他兼容调用若没有内容窗口，可省略 start/end；该路径只扩大召回，不自行判断内容资格，
也不得恢复任何 24/48 小时常量。当前启用的 `daily_delivery` 必须始终传入唯一自然日窗口。

- [ ] **步骤 5：运行测试并提交**

运行任务 3 步骤 2 命令，预期全部 `OK`，然后：

```bash
git add trendradar/crawler/news_search.py trendradar/__main__.py tests/test_news_search.py tests/test_news_search_pipeline.py
git commit -m "refactor: 统一新闻搜索自然日窗口"
```

### 任务 4：删除 AI 和报告转换的二次 freshness

**文件：**
- 修改：`trendradar/ai/filter_pipeline.py:18-22,64-142,615-682`
- 修改：`trendradar/__main__.py:1270-1333,1515-1520,1560-1610,1718-1795`
- 测试：`tests/test_previous_day_time_rule.py`
- 测试：`tests/test_sciencedirect_rss_dates.py`
- 测试：`tests/test_rice_science_links.py`

- [ ] **步骤 1：扩展失败测试以证明生产路径没有旧关键词**

在 `LegacyTimeRuleRemovalTests` 新增：

```python
def test_production_paths_have_no_legacy_rolling_filter_tokens(self):
    paths = (
        "trendradar/crawler/rss/fetcher.py",
        "trendradar/ai/filter_pipeline.py",
        "trendradar/__main__.py",
    )
    forbidden = ("FRESHNESS_FILTER", "max_age_days", "is_within_days", "calculate_days_old")
    for relative_path in paths:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for token in forbidden:
            with self.subTest(path=relative_path, token=token):
                self.assertNotIn(token, text)
```

- [ ] **步骤 2：运行测试并确认失败**

运行任务 1 步骤 2 命令。

预期：`filter_pipeline.py` 和 `__main__.py` 仍包含旧 token，测试失败。

- [ ] **步骤 3：删除 AI freshness 分支**

从 `AIFilterPipeline` 删除 freshness import、配置字段、feed age map 和 `_is_rss_item_fresh()`；范围函数变为：

```python
def _is_rss_item_in_scope(
    self,
    published_at: str,
    news_item_id: Optional[int] = None,
) -> bool:
    if self._allowed_rss_ids is not None:
        allowed = news_item_id in self._allowed_rss_ids
        if self._rss_ids_authoritative or not allowed:
            return allowed
    if self._rss_window is not None:
        return self._rss_window.contains(published_at)
    return True
```

把 `_collect_pending_news()` 的 `freshness_filtered_rss` 改名为 `scope_filtered_rss`，日志改为“范围过滤”。active daily 依赖权威 ID，weekly 依赖自身窗口，均不会产生第二个滚动规则。

- [ ] **步骤 4：删除 main 中抓取和转换 freshness**

构造 `RSSFeedConfig` 时不再解析 `max_age_days`；构造 `RSSFetcher` 时不再读取或传入 freshness 参数。

`_convert_rss_items_to_list()` 收敛为：

```python
def _convert_rss_items_to_list(
    self, items_dict: Dict, id_to_name: Dict,
) -> List[Dict]:
    return [
        {
            "title": item.title,
            "feed_id": feed_id,
            "feed_name": id_to_name.get(feed_id, feed_id),
            "url": item.url,
            "reader_url": build_reader_url(feed_id, item.url, item.title),
            "published_at": item.published_at,
            "summary": item.summary,
            "author": item.author,
        }
        for feed_id, items in items_dict.items()
        for item in items
    ]
```

删除所有 `apply_freshness=` 实参和旧 debug 过滤日志。更新 Rice Science/ScienceDirect 测试，使缺失发布时间只在自然日聚合器处被排除，不再依赖 AI freshness。

- [ ] **步骤 5：运行测试并提交**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_previous_day_time_rule tests.test_sciencedirect_rss_dates tests.test_rice_science_links -v
```

预期：全部 `OK`，然后：

```bash
git add trendradar/ai/filter_pipeline.py trendradar/__main__.py tests/test_previous_day_time_rule.py tests/test_sciencedirect_rss_dates.py tests/test_rice_science_links.py
git commit -m "refactor: 删除AI与报告滚动时间过滤"
```

### 任务 5：按发布时间聚合昨日库和今日库

**文件：**
- 修改：`trendradar/core/daily_delivery.py:123-322`
- 修改：`trendradar/__main__.py:1497-1523`
- 测试：`tests/test_daily_delivery.py:172-613,1578-1794`
- 测试：`tests/test_daily_delivery_schedule.py:228-300,1110-1270`
- 测试：`tests/test_daily_delivery_review3.py`
- 测试：`tests/test_daily_delivery_review5.py:480-543`
- 测试：`tests/test_daily_delivery_review6.py:84-195`

- [ ] **步骤 1：编写双日库、唯一范围和快照时钟测试**

聚合器测试 helper 只传运行时间：

```python
def build(self, now=None):
    return DailyDeliveryAggregator(
        self.backend, "Asia/Shanghai"
    ).build(now or shanghai(2026, 8, 10, 10, 0))
```

关键测试：

```python
def test_reads_yesterday_and_today_databases_by_publication_date(self):
    save_rss_day(self.backend, "2026-08-09", "23-00", [RSSItem(
        title="Yesterday DB", feed_id="journal",
        url="https://example.org/yesterday-db", published_at="2026-08-09",
    )])
    save_rss_day(self.backend, "2026-08-10", "10-00", [RSSItem(
        title="Today DB", feed_id="journal",
        url="https://example.org/today-db", published_at="2026-08-09",
    )])
    snapshot = self.build()
    self.assertEqual(
        [item.title for item in snapshot.iter_items()],
        ["Today DB", "Yesterday DB"],
    )
    self.assertEqual(snapshot.data.date, "2026-08-10")
    self.assertEqual(snapshot.data.crawl_time, "2026-08-10 10:00:00")


def test_first_seen_and_checkpoint_do_not_control_content(self):
    save_rss_day(self.backend, "2026-08-10", "10-00", [
        RSSItem(
            title="Old discovery, yesterday publication",
            feed_id="journal", url="https://example.org/in",
            published_at="2026-08-09", first_time="2026-07-01 09:00:00",
        ),
        RSSItem(
            title="New discovery, old publication",
            feed_id="journal", url="https://example.org/out",
            published_at="2026-08-08", first_time="2026-08-10 10:00:00",
        ),
    ])
    with patch.object(
        self.backend,
        "get_earliest_rss_discoveries_strict",
        side_effect=AssertionError("不得读取 first-seen 决定范围"),
    ):
        snapshot = self.build()
    self.assertEqual(
        [item.title for item in snapshot.iter_items()],
        ["Old discovery, yesterday publication"],
    )
```

删除或重写所有 checkpoint/first-seen 内容窗口测试；canonical 去重、稳定 GUID、失败源、合法空库、严格读取和权威 ID 测试继续保留，并给候选显式添加昨日 `published_at`。

- [ ] **步骤 2：运行聚合与主链测试并确认失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest \
  tests.test_daily_delivery \
  tests.test_daily_delivery_schedule \
  tests.test_daily_delivery_review3 \
  tests.test_daily_delivery_review5 \
  tests.test_daily_delivery_review6 -v
```

预期：旧 build 签名、first-seen 二次筛选和错误快照时钟导致失败。

- [ ] **步骤 3：实现唯一发布时间聚合**

`DailyDeliveryAggregator.build()` 改为：

```python
def build(self, now: datetime) -> DailyDeliverySnapshot:
    run_at = normalize_delivery_datetime(now, self.timezone)
    window = daily_delivery_window(run_at, self.timezone)
```

读取 `window.storage_dates` 的两库；每条只做：

```python
if not window.contains_published(item.published_at):
    filtered_out += 1
    continue
```

删除 `first_time/crawl_time` 范围判断和 `get_earliest_rss_discoveries_strict()` 整段。快照使用：

```python
data = RSSData(
    date=run_at.strftime("%Y-%m-%d"),
    crawl_time=run_at.strftime("%Y-%m-%d %H:%M:%S"),
    items=grouped_items,
    id_to_name=id_to_name,
    failed_ids=[],
)
```

- [ ] **步骤 4：解除 latest checkpoint 与内容范围耦合**

`__main__.py` 改为：

```python
snapshot = DailyDeliveryAggregator(
    self.storage_manager, self.ctx.timezone
).build(self._operation_run_at())
```

删除此处 `scheduler.latest_execution()`。保留 `run()` 中的 `already_executed()` 和成功后的 `record_execution()`，它们只负责同日幂等与失败重试。

- [ ] **步骤 5：运行测试并提交**

运行任务 5 步骤 2 命令，预期全部 `OK`，然后：

```bash
git add trendradar/core/daily_delivery.py trendradar/__main__.py tests/test_daily_delivery.py tests/test_daily_delivery_schedule.py tests/test_daily_delivery_review3.py tests/test_daily_delivery_review5.py tests/test_daily_delivery_review6.py
git commit -m "feat: 按发布日期构建昨日新闻快照"
```

### 任务 6：统一“昨日新闻”显示

**文件：**
- 修改：`trendradar/__main__.py:116-121`
- 修改：`config/timeline.yaml:461-473`
- 修改：`trendradar/notification/splitter.py:295-301,385-400`
- 修改：`trendradar/notification/renderer.py:121-132,263-274`
- 修改：`trendradar/notification/senders.py:847-855`
- 修改：`trendradar/report/html.py:1498-1507`
- 修改：`config/daily.crontab:4`
- 修改：`tests/test_portable_deployment.sh:42`
- 测试：`tests/test_daily_delivery_report.py`
- 测试：`tests/test_weekly_configuration.py`

- [ ] **步骤 1：编写失败的显示测试**

```python
def test_daily_delivery_header_contains_exact_previous_day_window(self):
    report_data = dict(REPORT_DATA)
    report_data["period_label"] = "2026-08-09 00:00—2026-08-10 00:00"
    content = split_content_into_batches(
        report_data=report_data,
        format_type="wework",
        mode="daily_delivery",
        report_type="昨日新闻",
    )[0]
    self.assertIn("类型： 昨日新闻", content)
    self.assertIn("周期： 2026-08-09 00:00—2026-08-10 00:00", content)
```

HTML、飞书、钉钉、企业微信和 ntfy payload 均断言“昨日新闻”；空态为“昨日新闻暂无匹配内容”；
时间线 `daily_delivery.name` 为“昨日新闻”。

- [ ] **步骤 2：运行显示测试并确认失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_daily_delivery_report tests.test_weekly_configuration tests.test_weekly_report_output -v
```

- [ ] **步骤 3：统一生产文案**

模式映射：

```python
"daily_delivery": {
    "mode_name": "昨日新闻模式",
    "description": "昨日新闻模式（按北京时间前一自然日发布时间筛选）",
    "report_type": "昨日新闻",
    "should_send_notification": True,
}
```

时间线把顶层名称、`report_mode` 注释和 period 显示名中的“每日新增”全部替换为“昨日新闻”，
但 `start/end/once/report_mode` 值不变；splitter/renderer/html 的 daily 显示与空态同步替换。
ntfy 的英文 header 映射增加 `"昨日新闻": "Previous Day News"`，不再回退通用标题。
把 `config/daily.crontab` 的旧“周一汇总上一自然周”注释改为“每天 10:00 推送前一自然日”，
Cron 表达式仍保持 `0 10 * * *`。
同步更新 `tests/test_portable_deployment.sh` 的注释断言，避免部署验证继续锁定旧周报文案。

- [ ] **步骤 4：运行测试并提交**

运行任务 6 步骤 2 命令，预期 `OK`，然后：

```bash
git add config/timeline.yaml config/daily.crontab trendradar/__main__.py trendradar/notification/splitter.py trendradar/notification/renderer.py trendradar/notification/senders.py trendradar/report/html.py tests/test_daily_delivery_report.py tests/test_weekly_configuration.py tests/test_portable_deployment.sh
git commit -m "feat: 统一昨日新闻报告文案"
```

### 任务 7：分层回归与合并前审查

**文件：**
- 验证：`tests/`
- 验证：`docker/entrypoint.sh`
- 验证：`config/daily.crontab`
- 验证：`tests/test_portable_deployment.sh`

- [ ] **步骤 1：运行聚焦回归**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_previous_day_time_rule tests.test_sciencedirect_rss_dates tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report tests.test_news_search tests.test_news_search_pipeline -v
```

预期：`OK`，退出码 0。

- [ ] **步骤 2：运行严格存储与通知回归**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_daily_delivery_review3 tests.test_daily_delivery_review4 tests.test_daily_delivery_review5 tests.test_daily_delivery_review6 tests.test_daily_delivery_review7 tests.test_daily_delivery_review8 -v
```

预期：`OK`，退出码 0。

- [ ] **步骤 3：运行 weekly、Elsevier、代理和邮件兼容回归**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_weekly_digest tests.test_weekly_schedule tests.test_weekly_report_output tests.test_elsevier_full_text tests.test_direct_first_proxy tests.test_email_delivery -v
```

预期：`OK`，退出码 0。weekly 自己的自然周窗口属于独立报告模式，不参与当前每日内容范围。

- [ ] **步骤 4：运行全量与静态检查**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest discover -s /workspace/tests -q
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
git diff --check
```

预期：全量 `OK`；其余命令退出码 0；LiteLLM 断网价格表警告使用本地备份，不算失败。

- [ ] **步骤 5：只读审查唯一时间规则**

审查必须确认：

- 搜索查询与最终聚合来自同一个 `DailyDeliveryWindow`；
- RSS、搜索聚合、AI、报告转换没有滚动 24/48 小时；
- `config*.yaml` 没有 `freshness_filter/max_age_days`；
- `published_at` 是唯一内容归属字段；
- first-seen/outbox 只维护存储一致性；
- checkpoint 只做幂等，失败时可重试，成功后不重复发；
- 权威 ID 阻止热榜、窗口外 RSS 或旧 AI 缓存混入；
- Cron 仍为每天 10:00。
- weekly 模块只作为未启用的独立显式模式保留，不得被 `daily_delivery` 调用；本次不把
  独立功能删除混入时间资格修复。

并执行生产代码/配置静态搜索：

```bash
rg -n "freshness_filter|max_age_days|when:2d|timespan.*48h|24 \* 60 \* 60|age_hours|recency" \
  trendradar config docs \
  --glob '!docs/superpowers/**'
rg -n "FRESHNESS_FILTER|freshness_filter|max_age_days|每日新增|周一汇总上一自然周" tests \
  --glob '!test_previous_day_time_rule.py'
```

预期：两条命令均无输出；`docs/superpowers` 最新规格/计划和专门的 removal test 中用于证明旧
规则已删除的文字不计入生产路径。另用
`rg -n "每日新增|周一汇总上一自然周" trendradar config docs tests/test_portable_deployment.sh --glob '!docs/superpowers/**'`
确认用户可见文案和部署断言中无旧 daily/weekly 描述。

审查修复必须先补 RED 测试，再重跑任务 7 步骤 1 至步骤 4。

- [ ] **步骤 6：确认分支干净**

```bash
git status --short
git diff --check
```

预期：工作树无未提交修改；不创建空提交。

### 任务 8：合并、备份缓存、重建并立即补跑

**运行位置：**
- 主工作区：`/mnt/d/project/trendradar`
- Compose：`docker/docker-compose.yml`
- 环境：`docker/.env`
- 可恢复备份：`/mnt/d/project/trendradar/output.backup-20260810-previous-day-window`

- [ ] **步骤 1：核对主工作区与备份目标**

```bash
git -C /mnt/d/project/trendradar status --short
git -C /mnt/d/project/trendradar log -1 --oneline
test ! -e /mnt/d/project/trendradar/output.backup-20260810-previous-day-window
```

预期：备份目标不存在；源代码没有与本分支重叠的用户修改。不得用 reset/checkout 清理 `index.html` 或 `output`。

- [ ] **步骤 2：快进合并实现分支**

```bash
git -C /mnt/d/project/trendradar merge --ff-only agent/previous-day-window
```

预期：快进成功，不改 `docker/.env`、本地 `.venv`、API key 或机器人 key。

- [ ] **步骤 3：停止两个 output 写入服务**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml stop trendradar trendradar-mcp
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
```

预期：两个服务均停止，没有任务持有 SQLite 文件。

- [ ] **步骤 4：原子备份旧缓存并创建空 output**

```bash
mv /mnt/d/project/trendradar/output /mnt/d/project/trendradar/output.backup-20260810-previous-day-window
mkdir /mnt/d/project/trendradar/output
find /mnt/d/project/trendradar/output.backup-20260810-previous-day-window -type f -print | sort
find /mnt/d/project/trendradar/output -mindepth 1 -print
```

预期：旧 RSS/news/HTML 全部仍在备份目录，新 `output` 为空；没有删除任何数据。

- [ ] **步骤 5：重建并启动**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build --force-recreate
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
```

预期：服务运行；沿用 `.env` 的 `IMMEDIATE_RUN=false`，启动不会抢跑。

- [ ] **步骤 6：只检查凭据 SET/UNSET**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar /app/.venv/bin/python -c "import os; print({k: ('SET' if os.getenv(k) else 'UNSET') for k in ('AI_API_KEY','WEWORK_WEBHOOK_URL','ELSEVIER_API_KEY','ELSEVIER_INST_TOKEN')})"
```

预期：AI 和企业微信所需项为 `SET`，不打印实际密钥。

- [ ] **步骤 7：立即补跑**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar /app/.venv/bin/python -m trendradar
```

预期：退出码 0；报告周期为 `2026-08-09 00:00—2026-08-10 00:00`；所有推送条目均为 8 月 9 日发布；企业微信所有批次显示“发送成功 [昨日新闻]”。

- [ ] **步骤 8：验证新检查点与新缓存**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar /app/.venv/bin/python -c "import sqlite3; p='/app/output/news/2026-08-10.db'; c=sqlite3.connect(p); print(c.execute(\"SELECT execution_date,period_key,action FROM period_executions WHERE execution_date='2026-08-10' AND period_key='daily_delivery' ORDER BY action\").fetchall()); c.close()"
find output -type f -print | sort
```

预期：存在 `('2026-08-10', 'daily_delivery', 'push')`；新 output 只含本次生成的数据，不复用备份内旧缓存。

- [ ] **步骤 9：验证同日幂等**

再次运行任务 8 步骤 7。

预期：日志显示今天已成功交付并跳过分析/通知，没有第二次企业微信发送，退出码 0。

- [ ] **步骤 10：仅在失败时回滚**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml stop trendradar trendradar-mcp
mv /mnt/d/project/trendradar/output /mnt/d/project/trendradar/output.failed-20260810-previous-day-window
mv /mnt/d/project/trendradar/output.backup-20260810-previous-day-window /mnt/d/project/trendradar/output
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d
```

预期：旧 output 原样恢复，失败的新数据保留在 `output.failed-20260810-previous-day-window`；不删除任何目录。
