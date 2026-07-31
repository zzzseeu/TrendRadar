# 农业育种热点新闻搜索实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 使用无需 API Key 的 GDELT 与 Google News RSS 搜索中英文作物育种新闻，将跨媒体报道聚合为最近 24 小时内的最多 5 条热点并接入现有 AI 推送流程。

**架构：** 新建独立的新闻搜索模块，分别负责供应商解析、统一条目、URL/标题去重和搜索前置热点分。搜索结果作为一个合成 RSS 源合并入现有 `RSSData`，在 SQLite 中保存聚合元数据；AI 管道将前置热点分、相关度和育种价值合成为最终热点分，并限制搜索结果最多 5 条。

**技术栈：** Python 3.12、requests、feedparser、SQLite、unittest、Docker Compose、容器内 `/app/.venv`

---

## 文件结构

- 创建：`trendradar/crawler/news_search.py`，供应商客户端、统一数据模型、跨来源聚合和热点评分。
- 修改：`trendradar/core/loader.py`，加载并验证 `rss.news_search` 配置。
- 修改：`config/config.yaml`，提供中英文查询、来源开关、上限和权威域名配置。
- 修改：`trendradar/storage/base.py`，为 RSS 条目增加搜索聚合字段。
- 修改：`trendradar/storage/rss_schema.sql`，持久化搜索聚合字段。
- 修改：`trendradar/storage/sqlite_mixin.py`，迁移、保存并读取搜索聚合字段。
- 修改：`trendradar/__main__.py`，执行新闻搜索并合并为合成 RSS 源。
- 修改：`trendradar/ai/filter_pipeline.py`，计算最终热点分、限制前 5 条并传递报道来源数。
- 修改：`trendradar/report/formatter.py`，在通知和报告中注明独立报道来源数。
- 创建：`tests/test_news_search.py`，供应商解析、时间过滤、聚合、热点分和容错测试。
- 创建：`tests/test_news_search_pipeline.py`，配置、存储、AI 排序和格式化集成测试。

### 任务 1：加载可编辑的搜索配置

**文件：**
- 修改：`config/config.yaml`
- 修改：`trendradar/core/loader.py:186-224`
- 创建：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写失败的配置加载测试**

```python
class NewsSearchConfigTests(unittest.TestCase):
    def test_loader_exposes_validated_news_search_config(self):
        loaded = _load_rss_config({
            "rss": {
                "news_search": {
                    "enabled": True,
                    "max_results_per_provider": 40,
                    "max_hotspots": 5,
                    "similarity_threshold": 0.86,
                    "topics": [{
                        "id": "gene-editing",
                        "zh": "作物 基因编辑 育种",
                        "en": "crop gene editing breeding",
                    }],
                }
            }
        })

        search = loaded["NEWS_SEARCH"]
        self.assertTrue(search["ENABLED"])
        self.assertEqual(search["MAX_RESULTS_PER_PROVIDER"], 40)
        self.assertEqual(search["MAX_HOTSPOTS"], 5)
        self.assertEqual(search["TOPICS"][0]["id"], "gene-editing")
```

- [ ] **步骤 2：运行配置测试并确认红灯**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps -T \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar:/workspace -w /workspace trendradar \
  -m unittest tests.test_news_search_pipeline.NewsSearchConfigTests -v
```

预期：FAIL，`NEWS_SEARCH` 尚未由 `_load_rss_config` 返回。

- [ ] **步骤 3：实现配置加载与边界校验**

在 `_load_rss_config` 中将配置转换为运行时大写键，并把上限约束在安全范围：

```python
news_search = rss.get("news_search", {})
max_results = max(1, min(int(news_search.get("max_results_per_provider", 50)), 100))
max_hotspots = max(1, min(int(news_search.get("max_hotspots", 5)), 20))
similarity = max(0.5, min(float(news_search.get("similarity_threshold", 0.86)), 1.0))

runtime_news_search = {
    "ENABLED": bool(news_search.get("enabled", False)),
    "MAX_RESULTS_PER_PROVIDER": max_results,
    "MAX_HOTSPOTS": max_hotspots,
    "SIMILARITY_THRESHOLD": similarity,
    "PROVIDERS": news_search.get("providers", {"gdelt": True, "google_news": True}),
    "AUTHORITY_DOMAINS": news_search.get("authority_domains", []),
    "TOPICS": news_search.get("topics", []),
}
```

将其放入 `_load_rss_config` 返回值的 `NEWS_SEARCH`。

- [ ] **步骤 4：在 `config/config.yaml` 添加实际搜索配置**

```yaml
  news_search:
    enabled: true
    max_results_per_provider: 50
    max_hotspots: 5
    similarity_threshold: 0.86
    providers:
      gdelt: true
      google_news: true
    authority_domains:
      - reuters.com
      - nature.com
      - science.org
      - caas.cn
      - moa.gov.cn
      - cgiar.org
      - irri.org
    topics:
      - id: gene-editing
        zh: "作物 基因编辑 分子育种"
        en: "crop gene editing molecular breeding"
      - id: genomic-breeding
        zh: "作物 基因组选择 全基因组关联 育种"
        en: "crop genomic selection genome-wide association breeding"
      - id: germplasm-traits
        zh: "作物 种质资源 抗病 抗逆 产量 品质 改良"
        en: "crop germplasm disease resistance stress tolerance yield quality breeding"
      - id: seed-policy
        zh: "作物 品种审定 生物育种 种业"
        en: "crop variety approval biological breeding seed industry"
```

- [ ] **步骤 5：运行配置测试并提交**

运行目标测试，预期 PASS；然后：

```bash
git add config/config.yaml trendradar/core/loader.py tests/test_news_search_pipeline.py
git commit -m "feat: configure agricultural news search"
```

### 任务 2：实现 GDELT 与 Google News RSS 客户端

**文件：**
- 创建：`trendradar/crawler/news_search.py`
- 创建：`tests/test_news_search.py`

- [ ] **步骤 1：编写失败的供应商解析测试**

```python
class ProviderParsingTests(unittest.TestCase):
    def test_gdelt_parses_direct_article_and_seen_date(self):
        payload = {"articles": [{
            "title": "New genomic selection method improves wheat breeding",
            "url": "https://example.org/wheat?utm_source=gdelt",
            "domain": "example.org",
            "language": "English",
            "seendate": "20260731T080000Z",
        }]}
        article = GDELTClient().parse(payload, "genomic-breeding")[0]
        self.assertEqual(article.publisher, "example.org")
        self.assertEqual(article.published_at, "2026-07-31T08:00:00+00:00")

    def test_google_rss_parses_title_source_and_pubdate(self):
        article = GoogleNewsRSSClient().parse(GOOGLE_RSS, "gene-editing", "zh")[0]
        self.assertEqual(article.title, "水稻基因编辑取得新进展")
        self.assertEqual(article.publisher, "示例农业报")
        self.assertTrue(article.published_at.endswith("+00:00"))
```

测试夹具 `GOOGLE_RSS` 使用一个包含 `<title>`、`<link>`、`<pubDate>` 和 `<source>` 的最小 RSS 文档。

- [ ] **步骤 2：运行解析测试并确认红灯**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps -T \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar:/workspace -w /workspace trendradar \
  -m unittest tests.test_news_search.ProviderParsingTests -v
```

预期：ERROR，`trendradar.crawler.news_search` 尚不存在。

- [ ] **步骤 3：实现统一条目和 GDELT 客户端**

```python
@dataclass
class SearchArticle:
    title: str
    url: str
    published_at: str
    publisher: str
    language: str
    topic: str
    providers: set[str] = field(default_factory=set)
    related_publishers: set[str] = field(default_factory=set)
    summary: str = ""
    source_count: int = 1
    pre_hot_score: float = 0.0

@dataclass
class NewsSearchResult:
    items: list[SearchArticle]
    failed_providers: list[str] = field(default_factory=list)

class GDELTClient:
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def build_params(self, query: str, max_results: int) -> dict:
        return {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": "24h",
            "sort": "datedesc",
            "maxrecords": max_results,
        }
```

`fetch` 使用传入的 `DirectFirstSession` 请求 JSON；`parse` 接受 `YYYYMMDDTHHMMSSZ` 与 ISO 8601 时间，只保留标题、URL 和时间均存在的条目。

- [ ] **步骤 4：实现 Google News RSS 客户端**

```python
class GoogleNewsRSSClient:
    endpoint = "https://news.google.com/rss/search"

    def build_params(self, query: str, language: str) -> dict:
        locale = (
            {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
            if language == "zh"
            else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
        )
        return {"q": f"{query} when:1d", **locale}
```

`parse` 使用 `feedparser`，从 `<source>` 读取媒体名，从标题末尾的 ` - 媒体名` 去除重复来源文本，保留 Google News 跳转链接作为无法无损解析直链时的可访问链接。

- [ ] **步骤 5：运行供应商测试并提交**

运行目标测试，预期全部 PASS；然后：

```bash
git add trendradar/crawler/news_search.py tests/test_news_search.py
git commit -m "feat: add keyless agricultural news providers"
```

### 任务 3：实现严格过滤、跨来源聚合和前置热点分

**文件：**
- 修改：`trendradar/crawler/news_search.py`
- 修改：`tests/test_news_search.py`

- [ ] **步骤 1：编写失败的过滤、聚合与容错测试**

```python
class SearchAggregationTests(unittest.TestCase):
    def test_rejects_missing_future_and_expired_dates(self):
        result = coordinator.aggregate([
            article("missing", ""),
            article("future", "2026-07-31T15:01:00+08:00"),
            article("old", "2026-07-30T14:59:59+08:00"),
            article("fresh", "2026-07-31T14:00:00+08:00"),
        ], now="2026-07-31T15:00:00+08:00")
        self.assertEqual([item.title for item in result], ["fresh"])

    def test_merges_similar_reports_and_counts_publishers(self):
        result = coordinator.aggregate([REPORT_A, REPORT_B])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_count, 2)

    def test_one_provider_failure_does_not_drop_other_results(self):
        result = coordinator.search()
        self.assertEqual(result.failed_providers, ["gdelt"])
        self.assertEqual(len(result.items), 1)
```

- [ ] **步骤 2：运行聚合测试并确认红灯**

预期：FAIL，协调器、严格过滤和聚合尚未实现。

- [ ] **步骤 3：实现 URL 规范化和同语种标题相似聚合**

```python
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid"}

def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query)
        if key.lower() not in TRACKING_KEYS
    ])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))

def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()
```

先按规范 URL 精确合并，再对同语种标题使用配置阈值（默认 `0.86`）合并；聚合后保留权威域名优先、否则时间更新的主链接。

- [ ] **步骤 4：实现前置热点分和供应商容错**

```python
coverage = min(source_count / 3, 1.0)
authority = 1.0 if publisher_domain in authority_domains else 0.0
recency = max(0.0, 1.0 - age_hours / 24.0)
pre_hot_score = round(0.5 * coverage + 0.3 * authority + 0.2 * recency, 4)
```

`AgriculturalNewsSearch.search()` 分别捕获 GDELT 和 Google 异常，返回 `NewsSearchResult(items, failed_providers)`；只有两个来源都失败时结果为空，但不抛出到主流程。

- [ ] **步骤 5：运行聚合测试并提交**

运行 `tests.test_news_search`，预期全部 PASS；然后：

```bash
git add trendradar/crawler/news_search.py tests/test_news_search.py
git commit -m "feat: aggregate and rank breeding news coverage"
```

### 任务 4：持久化搜索聚合元数据

**文件：**
- 修改：`trendradar/storage/base.py:64-115`
- 修改：`trendradar/storage/rss_schema.sql:20-45`
- 修改：`trendradar/storage/sqlite_mixin.py:105-115,900-955,1000-1060,1690-1770,1805-1840`
- 修改：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写失败的 RSS 元数据往返测试**

```python
def test_search_metadata_survives_rss_item_round_trip(self):
    item = RSSItem(
        title="Breeding hotspot",
        feed_id="agri-breeding-search",
        source_count=3,
        pre_hot_score=0.82,
        search_topic="gene-editing",
        search_providers="gdelt,google_news",
    )
    restored = RSSItem.from_dict(item.to_dict())
    self.assertEqual(restored.source_count, 3)
    self.assertEqual(restored.pre_hot_score, 0.82)
```

SQLite 测试创建临时日库，保存后通过 `get_all_rss_ids` 检查四个字段均未丢失。

- [ ] **步骤 2：运行元数据测试并确认红灯**

预期：ERROR，`RSSItem` 尚不接受搜索字段。

- [ ] **步骤 3：扩展数据模型与数据库迁移**

```python
source_count: int = 1
pre_hot_score: float = 0.0
search_topic: str = ""
search_providers: str = ""
```

`rss_schema.sql` 增加同名列；`_migrate_rss_schema` 对已有日库逐列执行：

```python
if "source_count" not in columns:
    conn.execute("ALTER TABLE rss_items ADD COLUMN source_count INTEGER DEFAULT 1")
if "pre_hot_score" not in columns:
    conn.execute("ALTER TABLE rss_items ADD COLUMN pre_hot_score REAL DEFAULT 0")
if "search_topic" not in columns:
    conn.execute("ALTER TABLE rss_items ADD COLUMN search_topic TEXT DEFAULT ''")
if "search_providers" not in columns:
    conn.execute("ALTER TABLE rss_items ADD COLUMN search_providers TEXT DEFAULT ''")
```

- [ ] **步骤 4：更新 SQLite 保存和所有 RSS 读取路径**

在 INSERT、UPDATE、`get_rss_data`、`get_latest_rss_data`、`get_all_rss_ids` 和 AI 分类结果 RSS JOIN 中显式读写四个字段，禁止依赖 `SELECT *`。对普通 RSS 使用数据库默认值。

- [ ] **步骤 5：运行元数据测试并提交**

运行 `tests.test_news_search_pipeline`，预期元数据测试 PASS；然后：

```bash
git add trendradar/storage/base.py trendradar/storage/rss_schema.sql \
  trendradar/storage/sqlite_mixin.py tests/test_news_search_pipeline.py
git commit -m "feat: persist breeding news search metadata"
```

### 任务 5：把搜索结果合并进 RSS 抓取流程

**文件：**
- 修改：`trendradar/__main__.py:990-1080`
- 修改：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写失败的主流程合并测试**

```python
def test_merge_search_result_adds_synthetic_rss_feed(self):
    rss_data = RSSData(date="2026-07-31", crawl_time="15:00", items={})
    result = NewsSearchResult(items=[SEARCH_HOTSPOT], failed_providers=["gdelt"])

    merge_news_search_into_rss(rss_data, result)

    item = rss_data.items["agri-breeding-search"][0]
    self.assertEqual(item.source_count, 2)
    self.assertEqual(item.pre_hot_score, SEARCH_HOTSPOT.pre_hot_score)
    self.assertEqual(rss_data.id_to_name["agri-breeding-search"], "农业育种热点搜索")
```

- [ ] **步骤 2：运行合并测试并确认红灯**

预期：ERROR，主流程合并函数尚不存在。

- [ ] **步骤 3：实现纯函数合并和运行时调用**

在 `trendradar.__main__` 添加可单测纯函数，将搜索条目转换为 `RSSItem`：

```python
SEARCH_FEED_ID = "agri-breeding-search"

def merge_news_search_into_rss(rss_data, search_result):
    rss_data.id_to_name[SEARCH_FEED_ID] = "农业育种热点搜索"
    rss_data.items[SEARCH_FEED_ID] = [
        RSSItem(
            title=item.title,
            feed_id=SEARCH_FEED_ID,
            feed_name="农业育种热点搜索",
            url=item.url,
            guid=canonicalize_url(item.url),
            published_at=item.published_at,
            summary=item.summary,
            author=item.publisher,
            source_count=item.source_count,
            pre_hot_score=item.pre_hot_score,
            search_topic=item.topic,
            search_providers=",".join(sorted(item.providers)),
            crawl_time=rss_data.crawl_time,
            first_time=rss_data.crawl_time,
            last_time=rss_data.crawl_time,
        )
        for item in search_result.items
    ]
```

`_crawl_rss_data` 在固定来源 `fetch_all()` 后读取 `NEWS_SEARCH`；启用时调用协调器并合并。搜索失败只打印 `[新闻搜索]` 日志，不加入固定 RSS 的失败数量。

- [ ] **步骤 4：运行合并与现有 RSS 测试并提交**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps -T \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar:/workspace -w /workspace trendradar \
  -m unittest tests.test_news_search_pipeline tests.test_rss_strict_freshness \
  tests.test_sciencedirect_rss_dates -v
```

预期：全部 PASS；然后提交 `trendradar/__main__.py` 与测试。

### 任务 6：计算最终热点分、限制前 5 条并展示报道数

**文件：**
- 修改：`trendradar/ai/filter_pipeline.py:530-620,700-750`
- 修改：`trendradar/report/formatter.py:8-55`
- 修改：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写失败的最终排名与格式化测试**

```python
def test_search_results_are_ranked_by_combined_hot_score_and_capped_at_five(self):
    result = pipeline._build_filter_result(raw_results=SIX_SEARCH_RESULTS, total_processed=6)
    search_items = [
        item for tag in result.tags for item in tag["items"]
        if item["source_id"] == "agri-breeding-search"
    ]
    self.assertEqual(len(search_items), 5)
    self.assertGreaterEqual(search_items[0]["final_hot_score"], search_items[-1]["final_hot_score"])

def test_formatter_shows_independent_source_count(self):
    rendered = format_title_for_platform("wework", SEARCH_TITLE_DATA)
    self.assertIn("3家来源", rendered)
```

- [ ] **步骤 2：运行排名测试并确认红灯**

预期：FAIL，搜索元数据尚未进入 AI 结果，且不会限制 5 条。

- [ ] **步骤 3：传递元数据并计算最终热点分**

从 `get_all_rss_ids` 和分类结果传递 `source_count`、`pre_hot_score`、`search_topic`、`search_providers`。仅对 `source_id == "agri-breeding-search"` 计算：

```python
final_hot_score = round(
    0.45 * pre_hot_score
    + 0.35 * relevance_score
    + 0.20 * importance_score,
    4,
)
```

按 `final_hot_score` 降序选取配置 `MAX_HOTSPOTS`（默认 5），从各标签组移除其余搜索条目；普通固定来源仍使用现有重要性/相关性排序。

- [ ] **步骤 4：把报道数传到报告并渲染**

`convert_to_report_data` 把 `source_count` 和 `final_hot_score` 放入 `title_entry`。`_append_ai_details` 在 `source_count > 1` 时按平台追加 `🔥 N家来源`；HTML 使用转义后的 `<span class="coverage-count">`。

- [ ] **步骤 5：运行排名、格式化和现有通知测试并提交**

运行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps -T \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar:/workspace -w /workspace trendradar \
  -m unittest tests.test_news_search_pipeline tests.test_rice_science_links \
  tests.test_wework_pdf -v
```

预期：全部 PASS；然后提交 AI 管道、格式化器和测试。

### 任务 7：完整验证、部署与实际搜索审计

**文件：**
- 验证：`tests/`
- 验证：`output/html/latest/current.html`
- 验证：`output/rss/<当天日期>.db`

- [ ] **步骤 1：运行完整测试套件**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml run --rm --no-deps -T \
  --entrypoint /app/.venv/bin/python \
  -v /mnt/d/project/trendradar:/workspace -w /workspace trendradar \
  -m unittest discover -s tests -v
```

预期：失败数为 0。

- [ ] **步骤 2：检查相关补丁质量**

```bash
git diff --check -- config/config.yaml trendradar tests
git status --short
```

预期：本次相关文件无空白错误；已有生成文件和用户文件保持原样。

- [ ] **步骤 3：重建正式容器并立即补跑**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build --force-recreate trendradar
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -m trendradar
```

预期：GDELT 与 Google News RSS 至少一个成功；两个都失败时固定来源任务仍以退出码 0 完成。

- [ ] **步骤 4：审计实际搜索结果**

使用容器内 `/app/.venv/bin/python` 只读查询当天 RSS 数据库，检查 `feed_id='agri-breeding-search'`：

- 每条 `published_at` 在最终报告生成时刻前滚动 24 小时内；
- URL 或相似标题没有重复占用热点名额；
- `source_count`、`pre_hot_score` 与 `search_providers` 非空且合理；
- AI 相关度低于 `0.7` 的条目不进入最终报告；
- 最终搜索热点不超过 5 条，并显示独立报道来源数。

- [ ] **步骤 5：确认容器状态并记录结果**

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
git log --oneline -8
```

预期：`trendradar` 为 `Up`，所有功能提交可追溯，未自动推送远程。
