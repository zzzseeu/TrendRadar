# 每日成功检查点推送实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 每天北京时间 10:00 推送上次完整成功交付之后首次发现的农业新闻，失败时保留积压，成功或合法空周期才推进检查点。

**架构：** 在现有 `period_executions` 上增加跨日期读取最近成功时间的能力，并新增按首次抓取时间聚合 RSS/新闻搜索结果的 `DailyDeliveryAggregator`。系统级首次发现由固定、版本化且不可变的 first-seen identity 账本提供：raw RSS 与 durable outbox 同事务提交，账本把 inventory 的 listed provenance 与单一 SQLite 只读快照绑定，只消费 `source_generation > watermark` 的 outbox；旧数据仅一次性严格回填，之后稳定查询不再打开历史库。`daily_delivery` 模式在入口冻结 run_at/run_date，只把该日期的权威快照交给 AI、报告和通知；strict AI 使用事务性标签快照、完整分类协议和最终叙事校验；全部配置端点成功后通过 strict period 读写 API 推进检查点。Remote 的共享 `news/{date}.db` 所有写者及其他 strict mutation 统一使用真实 If-Match/If-None-Match conditional PUT，任何严格阶段失败都返回非零。

**技术栈：** Python 3.10+、SQLite、pytz、TrendRadar 存储抽象、现有 AI 筛选与通知调度器、Docker Compose、unittest

---

## 文件结构

- 创建 `trendradar/core/rss_snapshot.py`：周报和每日交付共用的 RSS 身份、标题 GUID、丰富度和来源合并工具。
- 修改 `trendradar/core/weekly.py`：改用公共快照工具，行为保持不变。
- 创建 `trendradar/core/daily_delivery.py`：成功检查点窗口、首次发现时间解析、跨日聚合、去重、快照落库和 ID 校验。
- 修改 `trendradar/storage/base.py`：声明最近周期执行、strict period、strict RSS、first-seen 和 strict 标签快照接口；未实现的第三方 strict 能力明确抛错。
- 修改 `trendradar/storage/rss_schema.sql`：raw RSS 同事务 outbox、generation 元数据和稳定标题 fallback 持久化。
- 创建 `trendradar/storage/first_seen_schema.sql`：固定 `rss/first-seen-v1.db` 的版本元数据、canonical identity 主键、source version/watermark、processed write 与时间索引。
- 修改 `trendradar/storage/sqlite_mixin.py`：读取准确 `executed_at`，实现绑定 listed provenance 的增量 outbox/watermark 消费、一次性历史回填、不可变 first-seen upsert/候选查询和事务性 strict 标签替换。
- 修改 `trendradar/storage/local.py`：本地固定账本、同步保存和 strict 标签快照。
- 修改 `trendradar/storage/remote.py`：远端版本 provenance、dirty authoritative 状态、连接失效/原子刷新、共享 news 全写者 conditional PUT CAS 和单一账本对象。
- 修改 `trendradar/storage/manager.py`：一致转发 strict period、first-seen 与 strict 标签接口。
- 修改 `trendradar/core/scheduler.py`：向业务编排暴露最近执行时间，并按 report mode 成对路由 strict period 读取与写入；调度解析接受冻结 run_at。
- 修改 `trendradar/ai/filter.py`：strict 分类解析完整 flat schema/ID/tag/唯一性/有限数值/非空字符串协议，一次 repair 后仍非法则整批失败。
- 修改 `trendradar/ai/filter_pipeline.py`：允许快照 ID 成为权威范围；strict 标签全量原子替换并读回；范围内分类或存储批次失败时关闭交付。
- 修改 `trendradar/ai/analyzer.py`：grounding 和配置裁剪后校验最终可交付叙事。
- 修改 `trendradar/context.py`：把权威快照范围和显式 operation_date 同时传给 AI 分类和报告转换。
- 修改 `trendradar/__main__.py`：入口冻结 run_at/run_date，接入每日快照、共享 news 保存检查、严格失败、重试、空周期成功和全部端点成功检查点。
- 修改 `trendradar/report/html.py`：显示“每日新增”。
- 修改 `trendradar/notification/splitter.py`：显示每日交付模式和精确周期标签。
- 修改 `trendradar/notification/renderer.py`：为各通知渲染器补充每日交付空状态名称。
- 修改 `config/timeline.yaml`：七天均使用 `daily_delivery`，每日只允许一次完整成功。
- 创建 `tests/test_daily_delivery.py`：窗口、首次发现时间、聚合、幂等和检查点存储测试。
- 创建 `tests/test_daily_delivery_schedule.py`：编排、重试、空周期、严格失败和配置测试。
- 创建 `tests/test_daily_delivery_report.py`：HTML 与通知头部测试。
- 创建 `tests/test_daily_delivery_review3.py`：first-seen 一次性回填/不可变/重试、远程 provenance、strict 分类协议、最终 grounding、标签事务和第三方 strict capability 测试。
- 创建 `tests/test_daily_delivery_review4.py`：outbox 新进程恢复、title-only 原子持久化、source watermark、真实 conditional CAS/dirty、strict period 与 flat scalar 类型测试。
- 创建 `tests/test_daily_delivery_review5.py`：listed-version 一致快照/增量 watermark、共享 news 全写者 CAS、strict period 读取与跨午夜 operation_date 主链测试。
- 修改 `tests/test_weekly_digest.py`：公共快照工具重构后的周报回归断言。
- 修改 `tests/test_weekly_schedule.py`：确认 weekly 能力保留且严格规则未退化。
- 修改 `tests/test_news_search_pipeline.py`：确认每日交付下固定 RSS 或搜索来源失败会中止。

### 任务 1：提取可复用的 RSS 快照身份规则

**文件：**
- 创建：`trendradar/core/rss_snapshot.py`
- 修改：`trendradar/core/weekly.py:1-242`
- 修改：`tests/test_weekly_digest.py`

- [ ] **步骤 1：为公共身份规则编写失败测试**

在 `tests/test_weekly_digest.py` 增加直接针对公共工具的测试：canonical URL 优先、无 URL 时标题身份稳定、标题 GUID 不含正文、信息丰富度和搜索来源集合保持原周报语义。

```python
from trendradar.core.rss_snapshot import (
    item_identity,
    stable_title_guid,
)


def test_shared_snapshot_identity_prefers_canonical_url(self):
    item = RSSItem(
        title="Rice breeding update",
        feed_id="feed-a",
        url="https://example.org/paper?utm_source=rss",
    )
    self.assertEqual(
        item_identity(item),
        ("url", "https://example.org/paper"),
    )


def test_shared_title_guid_is_stable_and_namespaced(self):
    item = RSSItem(title=" Rice   Breeding ", feed_id="feed-a")
    first = stable_title_guid(item, namespace="weekly")
    second = stable_title_guid(item, namespace="weekly")
    self.assertEqual(first, second)
    self.assertTrue(first.startswith("weekly-title:"))
    self.assertNotIn("Rice", first)
```

- [ ] **步骤 2：运行测试验证缺少公共模块**

运行：

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_weekly_digest -v
```

预期：FAIL，报错 `ModuleNotFoundError: No module named 'trendradar.core.rss_snapshot'`。

- [ ] **步骤 3：实现公共快照工具并迁移周报**

`trendradar/core/rss_snapshot.py` 提供以下完整接口；`weekly.py` 删除对应私有函数并调用这些函数。

```python
"""RSS 快照构建共用的稳定身份和合并规则。"""

import hashlib

from trendradar.crawler.news_search import canonicalize_url, normalize_title
from trendradar.storage.base import RSSItem


def item_identity(item: RSSItem) -> tuple:
    canonical = canonicalize_url(item.url)
    if canonical:
        return ("url", canonical)
    normalized = normalize_title(item.title)
    if not normalized:
        return ()
    return ("title", item.feed_id, normalized)


def stable_title_guid(item: RSSItem, namespace: str) -> str:
    identity = f"{item.feed_id}\0{normalize_title(item.title)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{namespace}-title:{digest}"


def item_richness(item: RSSItem) -> tuple[int, int, float, bool]:
    return (
        len(item.summary or ""),
        item.source_count or 0,
        item.pre_hot_score or 0.0,
        bool(item.author),
    )


def search_providers(item: RSSItem) -> set[str]:
    return {
        provider.strip()
        for provider in (item.search_providers or "").split(",")
        if provider.strip()
    }
```

迁移后周报标题回退调用为：

```python
if not canonical_url and not item.guid:
    item.guid = stable_title_guid(item, namespace="weekly")
```

- [ ] **步骤 4：运行周报聚合回归测试**

运行同步骤 2 命令。预期：`tests.test_weekly_digest` 全部 PASS，原有 SQLite 幂等、GUID 和 canonical URL 测试不变。

- [ ] **步骤 5：提交公共快照工具**

```bash
git add trendradar/core/rss_snapshot.py trendradar/core/weekly.py tests/test_weekly_digest.py
git commit -m "refactor: 复用RSS快照身份规则"
```

### 任务 2：跨日期读取最近成功检查点

**文件：**
- 修改：`trendradar/storage/base.py:330-390`
- 修改：`trendradar/storage/sqlite_mixin.py:793-875`
- 修改：`trendradar/storage/local.py:180-205`
- 修改：`trendradar/storage/remote.py:420-445`
- 修改：`trendradar/storage/manager.py:280-295`
- 修改：`trendradar/core/scheduler.py:284-315`
- 创建：`tests/test_daily_delivery.py`

- [ ] **步骤 1：编写本地 SQLite 最近检查点失败测试**

测试必须使用临时目录和真实 SQLite，记录三天执行状态，并证明只返回 `daily_delivery/push` 在截止日期前的最新 `executed_at`。

```python
def shanghai(year, month, day, hour, minute):
    return pytz.timezone("Asia/Shanghai").localize(
        datetime(year, month, day, hour, minute)
    )


class DailyDeliveryCheckpointTests(unittest.TestCase):
    def test_latest_success_checkpoint_crosses_daily_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalStorageBackend(data_dir=tmp, timezone="Asia/Shanghai")
            times = {
                "2026-08-07": shanghai(2026, 8, 7, 10, 0),
                "2026-08-08": shanghai(2026, 8, 8, 10, 2),
                "2026-08-09": shanghai(2026, 8, 9, 10, 4),
            }
            for date_str, now in times.items():
                with patch.object(backend, "_get_configured_time", return_value=now):
                    self.assertTrue(backend.record_period_execution(
                        date_str, "daily_delivery", "push"
                    ))

            self.assertEqual(
                backend.get_latest_period_execution(
                    "daily_delivery", "push", "2026-08-08"
                ),
                "2026-08-08 10:02:00",
            )
            self.assertIsNone(backend.get_latest_period_execution(
                "other", "push", "2026-08-09"
            ))
            backend.cleanup()
```

另加远程后端单元测试：模拟 `list_objects_v2` 分页仅返回 `news/YYYY-MM-DD.db`，断言按日期倒序下载并在找到首条记录后停止。

- [ ] **步骤 2：运行检查点测试验证接口缺失**

运行：

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery.DailyDeliveryCheckpointTests -v
```

预期：FAIL，报错 `LocalStorageBackend` 没有 `get_latest_period_execution`。

- [ ] **步骤 3：实现单库查询和存储后端枚举**

在 `StorageBackend` 增加默认接口，避免破坏第三方后端：

```python
def get_latest_period_execution(
    self,
    period_key: str,
    action: str,
    through_date: str,
) -> Optional[str]:
    """返回截止日期内最近一次成功执行的本地时区时间。"""
    return None
```

在 `SQLiteStorageMixin` 增加只读单库方法：

```python
def _get_period_execution_at_impl(
    self, date_str: str, period_key: str, action: str
) -> Optional[str]:
    try:
        conn = self._get_connection(date_str)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='period_executions'"
        )
        if not cursor.fetchone():
            return None
        cursor.execute(
            "SELECT executed_at FROM period_executions "
            "WHERE execution_date = ? AND period_key = ? AND action = ? "
            "ORDER BY executed_at DESC LIMIT 1",
            (date_str, period_key, action),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as exc:
        raise RuntimeError(
            f"读取周期执行时间失败: {date_str}/{period_key}/{action}: {exc}"
        ) from exc
```

本地后端仅枚举已存在的 `data_dir/news/*.db`，解析合法日期、过滤 `date <= through_date`、倒序调用单库方法。远程后端通过 paginator 枚举 `Prefix="news/"`，仅接受正则 `news/(\d{4}-\d{2}-\d{2})\.db`，同样倒序查询。读取失败必须抛出，不能退化成“没有检查点”。

- [ ] **步骤 4：接入管理器和 Scheduler**

`StorageManager` 与 `Scheduler` 使用一致签名转发：

```python
def latest_execution(
    self, period_key: str, action: str, through_date: str
) -> Optional[str]:
    return self.storage.get_latest_period_execution(
        period_key, action, through_date
    )
```

测试增加 `Scheduler.latest_execution()` 精确转发断言。

- [ ] **步骤 5：运行检查点和现有调度测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery tests.test_weekly_schedule -v
```

预期：全部 PASS；不存在检查点返回 `None`，查询异常向上抛出。

- [ ] **步骤 6：提交检查点读取能力**

```bash
git add trendradar/storage/base.py trendradar/storage/sqlite_mixin.py trendradar/storage/local.py trendradar/storage/remote.py trendradar/storage/manager.py trendradar/core/scheduler.py tests/test_daily_delivery.py
git commit -m "feat: 支持跨日期读取成功检查点"
```

### 任务 3：构建每日交付窗口和 RSS 快照

**文件：**
- 创建：`trendradar/core/daily_delivery.py`
- 修改：`tests/test_daily_delivery.py`

- [ ] **步骤 1：编写窗口和首次发现时间失败测试**

覆盖首次 24 小时、已有检查点、左开右闭边界、跨午夜日期、完整 SQLite 时间和 `HH-MM` 时间结合数据库日期解析。

```python
def test_first_delivery_uses_previous_twenty_four_hours(self):
    now = shanghai(2026, 8, 9, 10, 0)
    window = daily_delivery_window(now, None, "Asia/Shanghai")
    self.assertEqual(window.start, shanghai(2026, 8, 8, 10, 0))
    self.assertEqual(window.end, now)
    self.assertEqual(window.storage_dates, ["2026-08-08", "2026-08-09"])
    self.assertFalse(window.contains(shanghai(2026, 8, 8, 10, 0)))
    self.assertTrue(window.contains(shanghai(2026, 8, 9, 10, 0)))


def test_time_only_first_seen_uses_own_database_date(self):
    parsed = parse_discovered_at("09-45", "2026-08-08", "Asia/Shanghai")
    self.assertEqual(parsed, shanghai(2026, 8, 8, 9, 45))
```

- [ ] **步骤 2：运行窗口测试验证模块缺失**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery.DailyDeliveryWindowTests -v
```

预期：FAIL，报错缺少 `trendradar.core.daily_delivery`。

- [ ] **步骤 3：实现交付窗口和时间解析**

核心类型和边界必须如下：

```python
def normalize_delivery_datetime(value: datetime, timezone: str) -> datetime:
    tz = pytz.timezone(timezone)
    if value.tzinfo is None:
        return tz.localize(value)
    return value.astimezone(tz)


def parse_discovered_at(
    value: str,
    storage_date: str,
    timezone: str,
) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    tz = pytz.timezone(timezone)
    for time_format in ("%H-%M", "%H:%M"):
        try:
            clock = datetime.strptime(text, time_format).time()
            day = datetime.strptime(storage_date, "%Y-%m-%d").date()
            return tz.localize(datetime.combine(day, clock))
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return normalize_delivery_datetime(parsed, timezone)


@dataclass(frozen=True)
class DailyDeliveryWindow:
    start: datetime
    end: datetime
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M}—{self.end:%Y-%m-%d %H:%M}"

    @property
    def storage_dates(self) -> list[str]:
        day_count = (self.end.date() - self.start.date()).days
        return [
            (self.start + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(day_count + 1)
        ]

    def contains(self, value: datetime) -> bool:
        return self.start < value <= self.end


def daily_delivery_window(
    now: datetime,
    checkpoint: Optional[str],
    timezone: str,
) -> DailyDeliveryWindow:
    end = normalize_delivery_datetime(now, timezone)
    start = (
        parse_discovered_at(checkpoint, end.strftime("%Y-%m-%d"), timezone)
        if checkpoint
        else end - timedelta(hours=24)
    )
    if start is None or start >= end:
        raise RuntimeError("每日交付检查点无效")
    return DailyDeliveryWindow(start=start, end=end, timezone=timezone)
```

`parse_discovered_at()` 明确支持 `YYYY-MM-DD HH:MM:SS`、ISO 8601、`HH-MM` 和 `HH:MM`；无时区的完整时间按配置时区本地化，不得沿用 `parse_iso_datetime()` 的“无时区按 UTC”语义。

- [ ] **步骤 4：编写跨日聚合失败测试**

使用真实 `LocalStorageBackend` 构造前日和当日 RSS 库，覆盖以下验收：

- 检查点前条目排除，检查点后条目保留；
- 发布日期很旧但首次抓取时间在窗口内的条目保留；
- canonical URL 重复合并来源和更丰富摘要；
- 无 URL 条目使用 `daily-delivery-title:` GUID；
- 快照写入当前日期库后每个身份都有 SQLite ID；
- 重建相同窗口保持身份和 ID 集合幂等；
- 窗口内所有日库缺失时抛错；成功空抓取返回合法空快照；
- 任一读到的日库含 `failed_ids` 时抛出明确错误，避免推进不完整检查点。
- 检查点积压超过 2 天时仍读取完整日期范围，不设置静默丢弃上限。

聚合器只读取窗口日库以取得本轮候选内容；候选的系统级最早发现时间必须查询
`rss/first-seen-v1.db`，不得每轮重新扫描历史日库。原始 RSS 日库把 durable outbox、
generation 与条目/crawl record 同事务提交；账本按 source version/watermark 幂等消费。
账本缺失/旧版本时一次性严格回填截止窗口结束日期的所有现存 RSS 日库（含起点日期中
早于起点的记录），以后只打开 version/generation 变化的日库，稳定查询不得打开历史库。
变化日库必须先绑定 inventory 的 listed version，在同一只读事务中只查询
`source_generation > watermark` 的 outbox 并读取 generation，事务前后 provenance 不变
才允许把 listed version/generation/watermark 原子写入账本；观察到更新版本不代表已经
消费。初次迁移之外不得全量重放 outbox 或回退扫描 `rss_items`。
raw 已提交、ledger 同步失败后必须允许新进程从 outbox 恢复，不依赖旧 payload 重现，
也不写 delivered 状态。

```python
def save_rss_day(backend, date_str, crawl_time, items):
    grouped = {}
    names = {}
    for item in items:
        grouped.setdefault(item.feed_id, []).append(item)
        names[item.feed_id] = item.feed_name or item.feed_id
    saved = backend.save_rss_data(RSSData(
        date=date_str,
        crawl_time=crawl_time,
        items=grouped,
        id_to_name=names,
        failed_ids=[],
    ))
    if not saved:
        raise AssertionError("RSS 测试数据保存失败")


def test_late_indexed_old_article_is_selected_by_first_seen(self):
    article = RSSItem(
        title="Old publication, newly indexed",
        feed_id="search",
        url="https://example.org/late",
        published_at="2026-07-01T00:00:00Z",
    )
    save_rss_day(self.backend, "2026-08-09", "09-30", [article])
    snapshot = DailyDeliveryAggregator(
        self.backend, "Asia/Shanghai"
    ).build(
        now=shanghai(2026, 8, 9, 10, 0),
        checkpoint="2026-08-08 10:00:00",
    )
    self.assertEqual([item.title for item in snapshot.iter_items()], [
        "Old publication, newly indexed"
    ])
```

- [ ] **步骤 5：运行聚合测试验证失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery.DailyDeliveryAggregatorTests -v
```

预期：FAIL，报错缺少 `DailyDeliveryAggregator`。

- [ ] **步骤 6：实现聚合、落库和 ID 校验**

实现 `DailyDeliverySnapshot`，字段固定为：`window`、`data`、`allowed_rss_ids`、`missing_dates`、`total_read`、`filtered_out`、`duplicate_count`。聚合使用任务 1 的公共工具；条目所属日库日期必须传入 `parse_discovered_at(item.first_time or item.crawl_time, storage_date, timezone)`。

快照落库要求：

```python
data = RSSData(
    date=window.end.strftime("%Y-%m-%d"),
    crawl_time=window.end.strftime("%Y-%m-%d %H:%M:%S"),
    items=grouped_items,
    id_to_name=id_to_name,
    failed_ids=[],
)
if not self.storage.save_rss_data(data):
    raise RuntimeError("每日交付快照保存失败")
snapshot.data = data
snapshot.allowed_rss_ids = self._resolve_allowed_ids(data)
```

无 URL/GUID 条目调用 `stable_title_guid(item, namespace="daily-delivery")`。解析后的身份集合与 SQLite 返回身份集合不相等时抛出 `RuntimeError("每日交付快照 ID 解析失败：存在未持久化条目")`。

- [ ] **步骤 7：运行每日聚合和周报回归测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery tests.test_weekly_digest -v
```

预期：全部 PASS。

- [ ] **步骤 8：提交每日交付聚合器**

```bash
git add trendradar/core/daily_delivery.py tests/test_daily_delivery.py
git commit -m "feat: 构建每日新增交付快照"
```

### 任务 4：让 AI 严格限定在每日快照 ID

**文件：**
- 修改：`trendradar/ai/filter_pipeline.py:43-140,210-235`
- 修改：`trendradar/context.py:500-550`
- 修改：`tests/test_daily_delivery.py`
- 修改：`tests/test_weekly_digest.py`

- [ ] **步骤 1：编写权威 ID 范围失败测试**

新增测试证明：`allowed_rss_ids={7}` 且 `rss_ids_authoritative=True` 时，ID 7 即使发布日期超过全局 2 天也保留，ID 8 即使很新也排除；每日范围中任一分类批次失败返回失败结果而不是部分成功。

```python
def test_authoritative_snapshot_ids_override_publication_freshness(self):
    pipeline = AIFilterPipeline(
        config={
            "TIMEZONE": "Asia/Shanghai",
            "RSS": {
                "ENABLED": True,
                "FEEDS": [],
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 2},
            },
            "AI": {},
            "AI_FILTER": {},
            "FILTER": {},
        },
        storage_manager=MagicMock(),
        get_time_func=lambda: shanghai(2026, 8, 9, 10, 0),
        allowed_rss_ids={7},
        rss_ids_authoritative=True,
    )
    self.assertTrue(pipeline._is_rss_item_in_scope(
        "search", "2026-07-01T00:00:00Z", 7
    ))
    self.assertFalse(pipeline._is_rss_item_in_scope(
        "search", "2026-08-09T01:00:00Z", 8
    ))
```

- [ ] **步骤 2：运行 AI 范围测试验证参数缺失**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery.DailyDeliveryAIScopeTests -v
```

预期：FAIL，报错 `unexpected keyword argument 'rss_ids_authoritative'`。

- [ ] **步骤 3：实现权威 ID 语义并贯穿 Context**

`AIFilterPipeline` 增加布尔参数并在范围判断中优先返回：

```python
if self._allowed_rss_ids is not None:
    allowed = news_item_id in self._allowed_rss_ids
    if self._rss_ids_authoritative:
        return allowed
    if not allowed:
        return False
if self._rss_window is not None:
    return self._rss_window.contains(published_at)
return self._is_rss_item_fresh(feed_id, published_at)
```

`AppContext._get_ai_filter_pipeline()`、`run_ai_filter()` 和 `convert_ai_filter_to_report_data()` 均增加并透传同名参数。分类批次完整性错误改为范围通用文案 `范围内 AI 分类批次失败，已拒绝使用部分结果`，weekly 现有关闭语义保持不变。

- [ ] **步骤 4：运行每日和周报 AI 测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery tests.test_weekly_digest tests.test_ai_filter_classification_resilience -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交 AI 快照范围支持**

```bash
git add trendradar/ai/filter_pipeline.py trendradar/context.py tests/test_daily_delivery.py tests/test_weekly_digest.py
git commit -m "feat: 限定AI处理每日交付快照"
```

### 任务 5：接入每日交付编排、严格失败和重试

**文件：**
- 修改：`trendradar/__main__.py:30-125,225-255,399-535,914-1065,1160-1385,1641-1920`
- 创建：`tests/test_daily_delivery_schedule.py`
- 修改：`tests/test_news_search_pipeline.py`
- 修改：`tests/test_weekly_schedule.py`

- [ ] **步骤 1：编写端到端编排失败测试**

测试用 mock 隔离网络，但必须覆盖完整 `NewsAnalyzer.run()` 调用链：

1. Scheduler 最近检查点传给 `DailyDeliveryAggregator.build()`；
2. 聚合完成后 `window`、`allowed_rss_ids` 和权威标志同时进入 AI 分类与报告转换；
3. `daily_delivery` 报告只使用快照 RSS，热榜仍采集保存但不进入报告，避免重复当前榜单；
4. 全部通知端点成功才记录 `daily_delivery/push`；
5. 通知、AI 分类、AI 摘要、HTML 报告、RSS 保存、固定 RSS 源、搜索供应商或热榜来源失败时不记录检查点并让 `run()` 返回 `False`；
6. 前次 analyze 已记录但 push 未记录时补跑会重新分析；
7. 同日 push 已记录时不重复 AI 和通知；
8. 合法空快照不通知但记录 push，第二次运行不重复分析；
9. 普通模式继续保持任一通知渠道成功语义，weekly 继续全部成功语义。

```python
def delivery_schedule():
    return ResolvedSchedule(
        period_key="daily_delivery",
        period_name="每日新增",
        day_plan="daily",
        collect=True,
        analyze=True,
        push=True,
        report_mode="daily_delivery",
        ai_mode="daily_delivery",
        once_analyze=True,
        once_push=True,
    )


def test_delivery_failure_keeps_checkpoint_for_retry(self):
    scheduler = MagicMock()
    scheduler.already_executed.return_value = False
    scheduler.latest_execution.return_value = "2026-08-08 10:00:00"
    scheduler.record_execution.return_value = True
    dispatcher = MagicMock()
    dispatcher.dispatch_all.return_value = {"wework": True, "email": False}
    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    analyzer.ctx = SimpleNamespace(
        config={
            "ENABLE_NOTIFICATION": True,
            "SHOW_VERSION_UPDATE": False,
            "AI_ANALYSIS": {"ENABLED": False},
        },
        platform_ids=[],
        create_notification_dispatcher=MagicMock(return_value=dispatcher),
        create_scheduler=MagicMock(return_value=scheduler),
        prepare_report=MagicMock(return_value={}),
        format_date=MagicMock(return_value="2026-08-09"),
    )
    analyzer.report_mode = "daily_delivery"
    analyzer.frequency_file = None
    analyzer.proxy_url = None
    analyzer.update_info = None
    analyzer._hotlist_total_count = 0
    analyzer._rss_matched_count = 1
    analyzer._rss_total_count = 1
    analyzer._rss_source_total = 2
    analyzer._rss_source_failed = 0
    analyzer._report_period_label = "2026-08-08 10:00—2026-08-09 10:00"
    analyzer._has_notification_configured = MagicMock(return_value=True)
    analyzer._has_valid_content = MagicMock(return_value=False)

    delivered = analyzer._send_notification_if_needed(
        [],
        "每日新增",
        "daily_delivery",
        rss_items=[{"count": 1}],
        schedule=delivery_schedule(),
    )

    self.assertFalse(delivered)
    scheduler.record_execution.assert_not_called()
```

- [ ] **步骤 2：运行编排测试验证模式缺失**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery_schedule -v
```

预期：FAIL，报错没有 `daily_delivery` 策略或未调用聚合器。

- [ ] **步骤 3：增加模式状态并构建每日快照**

`NewsAnalyzer.MODE_STRATEGIES` 增加：

```python
"daily_delivery": {
    "mode_name": "每日新增模式",
    "description": "每日新增模式（上次成功后首次发现的内容）",
    "report_type": "每日新增",
    "should_send_notification": True,
},
```

初始化增加 `_rss_ids_authoritative = False`。`_process_rss_data_by_mode()` 在当前 RSS 抓取保存后读取：

```python
scheduler = self.ctx.create_scheduler()
date_str = self._operation_date()
checkpoint = scheduler.latest_execution(
    "daily_delivery", "push", date_str
)
snapshot = DailyDeliveryAggregator(
    self.storage_manager, self.ctx.timezone
).build(self._operation_run_at(), checkpoint)
self._rss_window = None
self._allowed_rss_ids = snapshot.allowed_rss_ids
self._rss_ids_authoritative = True
self._report_period_label = snapshot.window.label
```

快照转列表必须 `apply_freshness=False`；筛选资格已经由首次发现窗口确定，发布日期仅展示。`_run_analysis_pipeline()` 向 Context 的两条 AI 路径都传 `rss_ids_authoritative=self._rss_ids_authoritative`。

单次 `run()` 必须在任何调度/采集前只读取一次配置时区时钟，冻结 `run_at`、`run_date`
与输出时间名。上例的 scheduler resolve、聚合器 build、RSSData date/crawl_time、strict
tag/result/analyzed/RSS ID API、通知幂等检查和 checkpoint 均使用冻结值，不得在跨午夜时
调用 `date=None` 或再次读取 wall clock。

- [ ] **步骤 4：实现严格来源、AI、报告和端点失败**

增加集中判断，避免散落仅判断 weekly：

```python
@staticmethod
def _is_strict_delivery_mode(mode: str) -> bool:
    return mode in {"weekly", "daily_delivery"}
```

用该函数统一控制：AI 筛选不回退、AI 摘要失败中止、`require_all_targets=True`、通知结果必须全部为真、RSS 保存或抓取异常向上抛出。`daily_delivery` 当前抓取的 `failed_ids`、`rss_data.failed_ids` 或 `search_result.failed_providers` 非空时抛出带来源名称的 `RuntimeError`；异常不能被“继续使用固定 RSS”分支吞掉。

HTML 已启用但 `generate_html()` 返回空路径时抛出 `RuntimeError("每日交付 HTML 报告生成失败")`。日报只使用快照 RSS：`_execute_mode_strategy()` 为 `daily_delivery` 传空热榜数据和空 `new_titles`，但本轮热榜仍按原流程抓取与保存。

- [ ] **步骤 5：实现空周期、同日幂等和失败补跑**

提取检查点记录助手：

```python
def _record_delivery_checkpoint(
    self, schedule: ResolvedSchedule
) -> bool:
    if not schedule.period_key:
        return False
    return self.ctx.create_scheduler().record_execution(
        schedule.period_key, "push", self._operation_date()
    )
```

规则顺序固定：抓取并保存后，若同日 push 已成功则跳过分析和通知；合法空内容时不调用 dispatcher，直接记录 push；有内容时仅在所有端点成功后记录 push。记录失败使 `run()` 返回 `False`。`_run_ai_analysis()` 把 weekly 专用的 `weekly_push_pending` 改为 `strict_push_pending`，使每日交付补跑重新生成有效摘要。

- [ ] **步骤 6：运行编排、搜索和 weekly 回归测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery_schedule tests.test_news_search_pipeline tests.test_weekly_schedule -v
```

预期：全部 PASS；失败测试中
`record_execution("daily_delivery", "push", "2026-08-09")` 从未调用。

- [ ] **步骤 7：提交每日交付主路径**

```bash
git add trendradar/__main__.py tests/test_daily_delivery_schedule.py tests/test_news_search_pipeline.py tests/test_weekly_schedule.py
git commit -m "feat: 接入每日成功检查点交付"
```

### 任务 6：切换每日时间线并显示每日新增报告

**文件：**
- 修改：`config/timeline.yaml:395-505`
- 修改：`trendradar/report/html.py:1494-1510`
- 修改：`trendradar/notification/splitter.py:294-305,380-395`
- 修改：`trendradar/notification/renderer.py:110-140,250-280`
- 创建：`tests/test_daily_delivery_report.py`
- 修改：`tests/test_daily_delivery_schedule.py`
- 修改：`tests/test_weekly_configuration.py`

- [ ] **步骤 1：编写时间线和报告显示失败测试**

```python
ROOT = Path(__file__).resolve().parents[1]
REPORT_DATA = {
    "stats": [],
    "new_titles": [],
    "failed_ids": [],
    "total_new_count": 0,
    "rss_matched_count": 0,
    "rss_total_count": 0,
    "rss_source_total": 0,
    "rss_source_failed": 0,
}


def test_custom_timeline_runs_daily_delivery_every_day(self):
    timeline = yaml.safe_load(
        (ROOT / "config/timeline.yaml").read_text(encoding="utf-8")
    )["custom"]
    self.assertEqual(list(timeline["periods"]), ["daily_delivery"])
    self.assertEqual(
        timeline["day_plans"],
        {"daily": {"periods": ["daily_delivery"]}},
    )
    self.assertEqual(
        timeline["week_map"],
        {1: "daily", 2: "daily", 3: "daily", 4: "daily",
         5: "daily", 6: "daily", 7: "daily"},
    )
    period = timeline["periods"]["daily_delivery"]
    self.assertEqual(period["report_mode"], "daily_delivery")
    self.assertEqual(period["once"], {"analyze": True, "push": True})
    config = yaml.safe_load(
        (ROOT / "config/config.yaml").read_text(encoding="utf-8")
    )
    self.assertEqual(config["rss"]["freshness_filter"]["max_age_days"], 2)
    self.assertEqual(
        GDELTClient().build_params("rice breeding", 10)["timespan"],
        "48h",
    )
    self.assertIn(
        "when:2d",
        GoogleNewsRSSClient().build_params("rice breeding", "en")["q"],
    )


def test_daily_delivery_header_contains_exact_window(self):
    report_data = dict(REPORT_DATA)
    report_data["period_label"] = "2026-08-08 10:00—2026-08-09 10:00"
    content = split_content_into_batches(
        report_data=report_data,
        format_type="wework",
        mode="daily_delivery",
        report_type="每日新增",
    )[0]
    self.assertIn("类型： 每日新增", content)
    self.assertIn("周期： 2026-08-08 10:00—2026-08-09 10:00", content)
```

- [ ] **步骤 2：运行配置和报告测试验证失败**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery_report tests.test_daily_delivery_schedule tests.test_weekly_configuration -v
```

预期：FAIL，当前时间线仍为周一 weekly，渲染器未显示“每日新增”。

- [ ] **步骤 3：修改 custom 时间线**

配置目标必须精确为：

```yaml
custom:
  name: "每日新增推送"
  description: "每天汇总上次完整成功推送后首次发现的内容。"
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
    daily_delivery:
      name: "每日新增"
      start: "00:00"
      end: "24:00"
      collect: true
      analyze: true
      ai_mode: "follow_report"
      push: true
      report_mode: "daily_delivery"
      once:
        analyze: true
        push: true
  day_plans:
    daily:
      periods: ["daily_delivery"]
  week_map:
    1: "daily"
    2: "daily"
    3: "daily"
    4: "daily"
    5: "daily"
    6: "daily"
    7: "daily"
  overlap:
    policy: "error_on_overlap"
```

保留文件上方其他示例和 weekly 核心代码；仅 active `custom` 不再引用 weekly。

- [ ] **步骤 4：补充 HTML 和通知模式名称**

HTML 模式映射增加 `daily_delivery -> 每日新增`；splitter 的 `mode_map` 和空状态增加相同映射；飞书、钉钉等 renderer 的模式分支显示“每日新增模式下暂无匹配内容”。不改变 `incremental` 的既有文案。

- [ ] **步骤 5：运行配置、输出和可移植部署测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery_report tests.test_daily_delivery_schedule tests.test_weekly_configuration tests.test_weekly_report_output -v
bash tests/test_portable_deployment.sh
```

预期：全部 PASS；便携部署仍断言 `0 10 * * *`，无需改变 `docker/.env` 或 Compose 默认值。

- [ ] **步骤 6：提交时间线和报告显示**

```bash
git add config/timeline.yaml trendradar/report/html.py trendradar/notification/splitter.py trendradar/notification/renderer.py tests/test_daily_delivery_report.py tests/test_daily_delivery_schedule.py tests/test_weekly_configuration.py
git commit -m "feat: 每日十点推送新增内容"
```

### 任务 7：完整验证、审查、集成和部署

**文件：**
- 验证：`trendradar/`、`config/`、`tests/`
- 运行时：`docker/docker-compose.yml`、`docker/.env`（只读确认，不提交密钥文件）

- [ ] **步骤 1：运行每日交付聚焦测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report tests.test_news_search_pipeline -v
```

预期：全部 PASS。

- [ ] **步骤 2：运行关键兼容回归测试**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest tests.test_weekly_digest tests.test_weekly_schedule tests.test_weekly_report_output tests.test_elsevier_full_text tests.test_direct_first_proxy tests.test_email_delivery -v
```

预期：全部 PASS，weekly、Elsevier Insttoken、直连 AI、代理和多收件人语义保持可用。

- [ ] **步骤 3：运行全量测试和静态验证**

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python -e PYTHONPATH=/workspace -v "$PWD":/workspace -w /tmp docker-trendradar -m unittest discover -s /workspace/tests -v
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
git diff --check
```

预期：全部测试 PASS；两个 `bash -n` 和 `git diff --check` 无输出；LiteLLM 在禁网环境的价格表回退警告可忽略。

最终审查还必须运行 `tests.test_daily_delivery_review3`、`tests.test_daily_delivery_review4`
与 `tests.test_daily_delivery_review5`，并确认以下性能/一致性契约：多日库
backfill 后第二次候选查询不调用日库连接；远程 404→出现和 v1→v2 会刷新 checkpoint、
RSS 和账本连接；strict tag 批次上传后 provenance 必须变化；未知/畸形/重复分类响应、
最终空 grounding、标签中途失败或第三方缺 strict capability 都不形成成功交付。
另外确认 title-only/raw/outbox 原子性、新进程恢复、source watermark、existing/create 的
conditional PUT、三个竞争时点、dirty strict read、strict period CAS 回滚以及 scalar 类型
repair；listed-version 快照、真正增量 watermark、共享 news 全写者 CAS、strict period
has/record 成对路由和冻结 operation_date 跨午夜主链也必须覆盖。跨进程通知
exactly-once/分布式推送租约不在本计划范围内，端点可能重复语义保留。

- [ ] **步骤 4：做只读代码审查并修复 Critical/Important**

审查必须逐项确认：检查点只在完整成功后写入；空周期可推进；时间边界无丢失；时间只有 `HH-MM` 时使用所属数据库日期；快照 ID 权威范围同时进入分类和转换；同日成功补跑不分析不发送；全部通知端点成功语义包含多账号和邮件收件人；旧模式未被全局改写。

若发现 Critical/Important，先增加能复现问题的失败测试，再做最小修复，并重新执行步骤 1–3。

- [ ] **步骤 5：提交审查修复或确认工作树干净**

若有修复，精确暂存本计划涉及的实现文件并提交：

```bash
git add trendradar/core/rss_snapshot.py trendradar/core/weekly.py trendradar/core/daily_delivery.py trendradar/storage/base.py trendradar/storage/sqlite_mixin.py trendradar/storage/local.py trendradar/storage/remote.py trendradar/storage/manager.py trendradar/core/scheduler.py trendradar/ai/filter_pipeline.py trendradar/context.py trendradar/__main__.py trendradar/report/html.py trendradar/notification/splitter.py trendradar/notification/renderer.py config/timeline.yaml tests/test_daily_delivery.py tests/test_daily_delivery_schedule.py tests/test_daily_delivery_report.py tests/test_weekly_digest.py tests/test_weekly_schedule.py tests/test_weekly_configuration.py tests/test_news_search_pipeline.py
git commit -m "fix: 完善每日交付失败边界"
```

若无修复，运行 `git status --short`，预期无未提交实现文件。

- [ ] **步骤 6：快进合并到 main**

在主工作区确认仅有用户原先的 `index.html`、输出数据库和旧文档未跟踪文件，然后执行：

```bash
git merge --ff-only feature/daily-success-checkpoint
```

预期：main 快进到每日交付实现，不暂存或覆盖用户原有文件。

- [ ] **步骤 7：重建并启动服务**

确认 `docker/.env` 仍为 `CRON_SCHEDULE="0 10 * * *"`，不打印任何密钥，然后执行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
docker compose --env-file docker/.env -f docker/docker-compose.yml logs --tail 120 trendradar
```

预期：`trendradar` 与 `trendradar-mcp` 均为 running/healthy；日志显示 cron `0 10 * * *`；不手工触发真实通知。

- [ ] **步骤 8：验证下次自动运行时间**

根据部署时的北京时间说明下一次自动运行时间。若当天 10:00 已过，下一次为次日 10:00；若未到，则为当天 10:00。保留 weekly 功能但 active custom 时间线应解析为 `daily_delivery`。
