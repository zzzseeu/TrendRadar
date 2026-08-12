# 水稻优先四模块监控实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在现有自然周 PDF 主线上增加水稻产业动态、按水稻优先规则生成政策/产业/科研三个 Top20，并扩展可验证的国内外权威来源和 180–300 字证据摘要。

**架构：** 保留现有 `run → 自然周快照 → 严格 AI → 单次选择 → 四段叙事 → 原子 PDF → 企业微信文件投递` 主线。分类结果增加 `industry` 与 `species_scope`，选择器一次性生成不可变的三榜对象；来源性质由受控注册表决定，发现渠道由固定源和 Google News 的合并证据决定；PDF 只消费已持久化且通过 grounding 的摘要和来源状态，不再次调用模型。

**技术栈：** Python 3、SQLite、LiteLLM/OpenAI-compatible JSON Object、`unittest`、现有轻量 HTML 解析器、Chromium、Poppler、YAML。

---

## 文件结构

### 新建文件

- `trendradar/crawler/rss/source_registry.py`：集中定义固定来源的类别、官方域名、列表地址和启用状态；不保存密钥或运行状态。
- `trendradar/core/source_coverage.py`：把日级抓取状态汇总为周级成功/异常覆盖，并生成可展示的安全错误摘要。
- `tests/test_ai_filter_rice_modules.py`：四分类、物种范围、阈值和摘要证据契约。
- `tests/test_weekly_rice_priority.py`：三榜水稻优先、跨模块占用、Top20/Top5 和确定性排序。
- `tests/test_source_registry.py`：来源类别、官方域名、发现渠道和配置一致性。
- `tests/test_weekly_source_coverage.py`：部分失败继续、全部失败中止、成功空结果和失败的区分。
- `tests/test_official_rice_sources.py`：新增国际/国内来源的离线真实页面解析契约。
- `tests/fixtures/official_rice_sources/*.html`：经官方列表页取得并去除无关脚本的稳定 HTML fixture。
- `tests/fixtures/official_rice_sources/*.pdf`：仅供确实以 PDF 发布列表或报告的来源解析测试。

### 修改文件

- `config/config.yaml`、`config/config.en.yaml`：修正 CGIAR 地址，增加来源元数据和通过 fixture 验证的来源；所有模式维持唯一阈值 `0.5`。
- `config/ai_interests.txt`、`config/ai_filter/prompt.txt`：定义 `policy/industry/research/exclude`、`rice/general/other_crop`、政策/产业/科研边界和结构化摘要要求。
- `config/ai_analysis_prompt.txt`：从三段周报扩展为政策、产业、科研、气象四段叙事。
- `trendradar/ai/filter.py`：严格解析四分类、物种范围和摘要降级标记。
- `trendradar/ai/filter_pipeline.py`：贯穿 `species_scope`、来源类别、发现渠道和校审摘要。
- `trendradar/ai/analyzer.py`：增加 `industry_trends`，校验四段叙事及其证据命名空间。
- `trendradar/storage/ai_filter_schema.sql`、`trendradar/storage/sqlite_mixin.py`：迁移并读写 `industry`、`species_scope` 和来源元数据；旧分类与 analyzed 一并失效。
- `trendradar/storage/rss_schema.sql`、`trendradar/storage/base.py`：为 RSS 条目保存可确定的来源类别和发现渠道。
- `trendradar/core/rss_snapshot.py`：合并固定源和 Google News 的同文证据，并优先保留官方链接。
- `trendradar/core/weekly.py`：三模块一次选择、水稻层级补位和来源覆盖接线。
- `trendradar/crawler/rss/fetcher.py`、`trendradar/crawler/rss/web_news.py`：读取来源注册表并解析新增官方列表页。
- `trendradar/crawler/news_search.py`：保留原始发布域名和 Google News 发现渠道，供稳定合并。
- `trendradar/__main__.py`：在现有 weekly 主线中接入三榜、来源覆盖、四段叙事和四模块 PDF；不改投递状态机。
- `trendradar/report/weekly_pdf.py`：渲染政策、产业、科研、气象四模块，显示长摘要、来源类别、发现渠道和异常来源。
- `tests/test_ai_filter_module_contract.py`、`tests/test_ai_filter_module_storage.py`、`tests/test_weekly_digest.py`、`tests/test_weekly_pdf_report.py`、`tests/test_weekly_pdf_delivery.py`、`tests/test_weekly_schedule.py`：迁移现有两榜/三段 fixture，并保留锁、账本、检查点和 PDF-only 回归。
- `README.md`、`README-EN.md`、`docs/news-push-technical-implementation.md`：说明四模块、水稻优先、来源覆盖和摘要证据规则。

## 不改动边界

- 不改变 `WeeklyAttemptLock`、逐账号 PDF 账本、global push checkpoint、partial resume 和企业微信 file 消息实现。
- 不重新引入通用 weekly HTML、Markdown 或文字消息回退。
- 不让 PDF 渲染器再次调用 AI；卡片只使用数据库读回的 `ai_summary`。
- 不把新闻搜索供应商当作原始发布来源。
- 不删除历史数据库或输出文件；schema 迁移只失效不兼容的 AI 结果与 analyzed 状态。

### 任务 1：扩展严格 AI 分类和 SQLite 契约

**文件：**
- 创建：`tests/test_ai_filter_rice_modules.py`
- 修改：`config/ai_filter/prompt.txt`
- 修改：`config/ai_interests.txt`
- 修改：`trendradar/ai/filter.py`
- 修改：`trendradar/storage/ai_filter_schema.sql`
- 修改：`trendradar/storage/sqlite_mixin.py`
- 修改：`tests/test_ai_filter_module_contract.py`
- 修改：`tests/test_ai_filter_module_storage.py`

- [ ] **步骤 1：编写四分类和物种范围 RED 测试**

```python
def test_strict_batch_returns_every_id_with_module_and_species():
    response = {"items": [
        {"id": 1, "module_type": "policy", "species_scope": "rice",
         "tag_id": 11, "score": 0.5, "importance_score": 0.8,
         "summary": "稻谷最低收购价政策明确执行对象与时间。"},
        {"id": 2, "module_type": "industry", "species_scope": "rice",
         "tag_id": 12, "score": 0.7, "importance_score": 0.6,
         "summary": "水稻订单项目披露参与方和实施进度。"},
        {"id": 3, "module_type": "research", "species_scope": "other_crop",
         "tag_id": 13, "score": 0.6, "importance_score": 0.7,
         "summary": "小麦研究提供可迁移的育种方法。"},
        {"id": 4, "module_type": "exclude", "species_scope": "general",
         "score": 0.1, "importance_score": 0.1, "summary": "无实质信息。"},
    ]}
    parsed = ai_filter._parse_classify_response(
        json.dumps(response, ensure_ascii=False),
        titles=[{"id": value} for value in (1, 2, 3, 4)],
        tags=[{"id": value} for value in (11, 12, 13)], strict=True,
    )
    assert [(item["module_type"], item["species_scope"]) for item in parsed] == [
        ("policy", "rice"), ("industry", "rice"),
        ("research", "other_crop"),
    ]

def test_industry_rejects_non_rice_species():
    invalid = {"items": [
        {"id": 1, "module_type": "industry", "species_scope": "general",
         "tag_id": 11, "score": 0.8, "importance_score": 0.8,
         "summary": "泛农业市场。"}
    ]}
    assert ai_filter._parse_classify_response(
        json.dumps(invalid, ensure_ascii=False), titles=[{"id": 1}],
        tags=[{"id": 11}], strict=True,
    ) is None
```

测试文件在同一 `TestCase.setUp()` 中使用现有 `AIFilter` 构造方式；schema 迁移测试直接扩展 `tests/test_ai_filter_module_storage.py` 已有的 `create_legacy_news_db()`、`local_backend()` 和 `seed_tag()`，只给旧表补 `module_type` 而不补 `species_scope`，避免新建第二套旧库工厂。查询辅助函数如下：

```python
def pragma_columns(path: Path, table: str) -> dict[str, sqlite3.Row]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return {
            row["name"]: row
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

def query_scalar(path: Path, sql: str) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute(sql).fetchone()[0])

```

- [ ] **步骤 2：运行 RED 测试并确认旧枚举失败**

运行：`.venv/bin/python -m unittest tests.test_ai_filter_rice_modules tests.test_ai_filter_module_contract -v`

预期：FAIL；旧代码拒绝 `industry`、不要求 `species_scope`，或结果缺少该字段。

- [ ] **步骤 3：实现严格解析和 Prompt 契约**

在 `trendradar/ai/filter.py` 定义并统一使用：

```python
REPORT_MODULES = frozenset({"policy", "industry", "research"})
ALL_MODULES = REPORT_MODULES | {"exclude"}
SPECIES_SCOPES = frozenset({"rice", "general", "other_crop"})

def _valid_module_species(module_type: str, species_scope: str) -> bool:
    return (
        module_type in ALL_MODULES
        and species_scope in SPECIES_SCOPES
        and (module_type != "industry" or species_scope == "rice")
    )
```

分类 Prompt 必须要求顶层 `{"items": [...]}`，每个输入 ID 恰好一次；`policy/industry/research` 必须有合法 `tag_id`，`exclude` 不写结果行；未知、重复、遗漏、布尔分数、NaN/Infinity、非法枚举触发一次 JSON repair，二次非法整批失败。

- [ ] **步骤 4：编写 schema 迁移和回滚 RED 测试**

```python
def test_legacy_module_rows_are_invalidated_without_defaulting_species():
    path = create_legacy_news_db(tempdir)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE ai_filter_results ADD COLUMN module_type TEXT"
        )
        connection.execute(
            "UPDATE ai_filter_results SET module_type = 'policy'"
        )
    backend = local_backend(tempdir)
    backend._get_connection(DATE)
    columns = pragma_columns(path, "ai_filter_results")
    assert "species_scope" in columns
    assert query_scalar(path, "SELECT COUNT(*) FROM ai_filter_results") == 0
    assert query_scalar(path, "SELECT COUNT(*) FROM ai_filter_analyzed_news") == 0

def test_strict_batch_rolls_back_results_and_analyzed_on_species_failure():
    conn.execute("""
        CREATE TRIGGER fail_second_analyzed
        BEFORE INSERT ON ai_filter_analyzed_news
        WHEN NEW.news_item_id = 2
        BEGIN
            SELECT RAISE(ABORT, 'second analyzed insert failed');
        END
    """)
    conn.commit()
    with self.assertRaises(sqlite3.IntegrityError):
        backend.replace_ai_filter_batch_strict(
            [result(1, tag_id, "policy", "rice"),
             result(2, tag_id, "research", "general")],
            [1, 2], [], INTERESTS_FILE, PROMPT_HASH, DATE,
        )
    assert conn.execute("SELECT COUNT(*) FROM ai_filter_results").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ai_filter_analyzed_news").fetchone()[0] == 0
```

把现有 `result(news_item_id, tag_id, module_type)` 工厂扩展为 `result(news_item_id, tag_id, module_type, species_scope="rice")`，并在返回字典中写入 `species_scope`。

- [ ] **步骤 5：迁移 SQLite 读写路径**

新表约束采用：

```sql
module_type TEXT NOT NULL
  CHECK(module_type IN ('policy', 'industry', 'research')),
species_scope TEXT NOT NULL
  CHECK(species_scope IN ('rice', 'general', 'other_crop')),
CHECK(module_type != 'industry' OR species_scope = 'rice')
```

旧库不得通过 `DEFAULT 'research'` 或 `DEFAULT 'general'` 迁移；检测到缺列时重建结果表或增加 nullable 列后立即清空 `ai_filter_results` 和 `ai_filter_analyzed_news`。普通和 strict INSERT、两套 SELECT、写后读回键都加入 `species_scope`；NULL 或非法值 strict read 必须失败。

- [ ] **步骤 6：运行模块与存储 GREEN 测试**

运行：`.venv/bin/python -m unittest tests.test_ai_filter_rice_modules tests.test_ai_filter_module_contract tests.test_ai_filter_module_storage tests.test_ai_filter_rule_invalidation -v`

预期：全部 PASS；SQLite 二次迁移无副作用。

- [ ] **步骤 7：提交任务 1**

```bash
git add config/ai_filter/prompt.txt config/ai_interests.txt \
  trendradar/ai/filter.py trendradar/storage/ai_filter_schema.sql \
  trendradar/storage/sqlite_mixin.py tests/test_ai_filter_rice_modules.py \
  tests/test_ai_filter_module_contract.py tests/test_ai_filter_module_storage.py
git commit -m "feat(ai): 增加水稻产业与物种分类契约"
```

### 任务 2：实现水稻优先三榜单次选择

**文件：**
- 创建：`tests/test_weekly_rice_priority.py`
- 修改：`trendradar/core/weekly.py`
- 修改：`trendradar/ai/filter_pipeline.py`
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_digest.py`
- 修改：`tests/test_weekly_three_module.py`

- [ ] **步骤 1：编写三榜排序与跨模块占用 RED 测试**

```python
def test_three_modules_use_species_tiers_and_global_precedence():
    shared = item("共同文章", "policy", "rice", url="https://official/a")
    selection = select_weekly_modules([
        shared,
        item("共同文章", "industry", "rice", url="https://official/a"),
        item("其他作物高分政策", "policy", "other_crop", importance=0.99),
        item("水稻政策", "policy", "rice", importance=0.51),
        item("通用育种", "research", "general", importance=0.99),
        item("水稻文献", "research", "rice", importance=0.51),
    ], min_score=0.5)
    assert [x["title"] for x in selection.policy][:2] == ["共同文章", "水稻政策"]
    assert all(x["url"] != "https://official/a" for x in selection.industry)
    assert [x["title"] for x in selection.research][:2] == ["水稻文献", "通用育种"]
```

测试文件使用完整候选工厂，避免依赖现有 fixture 的隐式默认值：

```python
def item(
    title: str, module: str, species: str, *, url: str | None = None,
    importance: float = 0.7, relevance: float = 0.7,
) -> dict:
    return {
        "title": title,
        "url": url or f"https://example.test/{quote(title)}",
        "guid": "",
        "module_type": module,
        "species_scope": species,
        "importance_score": importance,
        "relevance_score": relevance,
        "content_level": "full_text",
        "published_at": "2026-08-05T08:00:00+08:00",
        "source_name": "fixture",
    }
```

再覆盖 `0.49` 排除、`0.50` 纳入、每榜最多 20、各榜 Top5、政策第 21 条仍占用身份、industry 非 rice 不可进入、输入顺序打乱输出不变。

- [ ] **步骤 2：运行 RED 测试**

运行：`.venv/bin/python -m unittest tests.test_weekly_rice_priority tests.test_weekly_digest -v`

预期：FAIL；`WeeklyNewsSelection` 仍只有 `policy/research`，且没有物种层级。

- [ ] **步骤 3：扩展不可变选择结果和排序键**

```python
@dataclass(frozen=True)
class WeeklyNewsSelection:
    policy: tuple[dict, ...]
    industry: tuple[dict, ...]
    research: tuple[dict, ...]

MODULE_PRECEDENCE = ("policy", "industry", "research")
SPECIES_ORDER = {
    "policy": {"rice": 0, "general": 1, "other_crop": 2},
    "industry": {"rice": 0},
    "research": {"rice": 0, "general": 1, "other_crop": 2},
}
```

选择流程必须先收集所有达线身份并按 `policy → industry → research` 占用，再在模块内按物种层级、importance、relevance、证据完整度、发布时间、来源、标题、稳定身份排序，最后各截 20 并写连续 `module_rank`/前五 `highlight_rank`。

- [ ] **步骤 4：贯穿 pipeline 字段并移除 weekly 预裁剪**

`_build_filter_result()` 和 `convert_to_report_data()` 显式透传：

```python
{
    "module_type": row["module_type"],
    "species_scope": row["species_scope"],
    "relevance_score": row["relevance_score"],
    "importance_score": row["importance_score"],
    "content_level": row["content_level"],
    "ai_summary": row["ai_summary"],
}
```

weekly 权威快照禁止 `MAX_SEARCH_HOTSPOTS`、`MAX_NEWS_PER_KEYWORD` 和裸标题去重提前裁剪；普通模式保持现状。

- [ ] **步骤 5：让主线只调用一次选择器**

`NewsAnalyzer._select_weekly_rss_items()` 创建一次 `WeeklyNewsSelection`，后续 AI 叙事、PDF 和投递复用同一实例；删除两榜拼接 fallback。返回给分析器的分组项必须保留 `module_type/module_rank/species_scope`。

- [ ] **步骤 6：运行 GREEN 和普通模式兼容测试**

运行：`.venv/bin/python -m unittest tests.test_weekly_rice_priority tests.test_weekly_digest tests.test_weekly_three_module tests.test_news_search_pipeline -v`

预期：全部 PASS；ordinary 的默认搜索热点上限仍按配置生效。

- [ ] **步骤 7：提交任务 2**

```bash
git add trendradar/core/weekly.py trendradar/ai/filter_pipeline.py \
  trendradar/__main__.py tests/test_weekly_rice_priority.py \
  tests/test_weekly_digest.py tests/test_weekly_three_module.py \
  tests/test_news_search_pipeline.py
git commit -m "feat(weekly): 增加水稻优先三榜选择"
```

### 任务 3：建立来源注册表与发现渠道合并

**文件：**
- 创建：`trendradar/crawler/rss/source_registry.py`
- 创建：`tests/test_source_registry.py`
- 修改：`trendradar/crawler/rss/fetcher.py`
- 修改：`trendradar/crawler/news_search.py`
- 修改：`trendradar/core/rss_snapshot.py`
- 修改：`trendradar/storage/base.py`
- 修改：`trendradar/storage/rss_schema.sql`
- 修改：`trendradar/storage/sqlite_mixin.py`
- 修改：`trendradar/ai/filter_pipeline.py`

- [ ] **步骤 1：编写来源类别与发现渠道 RED 测试**

```python
def test_registry_controls_category_without_ai_guessing():
    assert source_metadata("rice-science").category == "academic_journal"
    assert source_metadata("moa-seed-policy").category == "government"
    assert source_metadata("cgiar-news").category == "international_org"
    assert source_metadata("irri-news").category == "research_institution"

def test_fixed_and_google_same_article_merge_to_both_and_official_url():
    fixed_item = RSSItem(
        title="Rice A", feed_id="philrice-news", feed_name="PhilRice",
        url="https://www.philrice.gov.ph/news/rice-a", published_at="2026-08-05",
        source_category="research_institution", discovery_channel="fixed",
        publisher_domain="philrice.gov.ph",
    )
    google_item = RSSItem(
        title="Rice A", feed_id="agri-breeding-search", feed_name="PhilRice",
        url="https://news.google.com/articles/x", published_at="2026-08-05T02:00:00+00:00",
        source_category="research_institution", discovery_channel="google_news",
        publisher_domain="philrice.gov.ph",
    )
    merged = merge_discovery_evidence([fixed_item, google_item])
    assert merged.url == "https://www.philrice.gov.ph/news/rice-a"
    assert merged.discovery_channel == "both"
    assert merged.source_category == "research_institution"
```

- [ ] **步骤 2：运行 RED 测试**

运行：`.venv/bin/python -m unittest tests.test_source_registry tests.test_news_search tests.test_weekly_digest -v`

预期：FAIL；`RSSItem` 和数据库尚无 `source_category/discovery_channel`。

- [ ] **步骤 3：实现受控来源注册表**

```python
@dataclass(frozen=True)
class SourceMetadata:
    feed_id: str
    category: str
    official_domains: tuple[str, ...]

SOURCE_CATEGORIES = frozenset({
    "academic_journal", "government", "international_org",
    "research_institution", "corporate_official", "news_media",
})
DISCOVERY_CHANNELS = frozenset({"fixed", "google_news", "both"})

def source_metadata(feed_id: str) -> SourceMetadata:
    try:
        return SOURCE_REGISTRY[feed_id]
    except KeyError as exc:
        raise ValueError(f"未注册的固定来源: {feed_id}") from exc
```

同文合并生产接口固定为 `merge_discovery_evidence(items: Sequence[RSSItem]) -> RSSItem`；测试构造真实 `RSSItem`，不创建仅供测试的 `merge_items/fixed/google` 旁路。

注册表覆盖所有启用 fixed feeds；启动配置检查在 feed 未注册、类别非法或官方域名为空时 fail closed。Google News 条目使用 publisher domain 映射原始类别，不能把 `news.google.com` 当来源。

- [ ] **步骤 4：迁移 RSS schema 和条目类型**

给 `RSSItem` 增加：

```python
source_category: str = ""
discovery_channel: str = "fixed"
publisher_domain: str = ""
```

`rss_items` 增加同名列并迁移：固定源旧行通过 feed registry 确定性回填；搜索旧行无法证明渠道时保留空值并在新周报候选中 fail closed，不猜测 `both`。

- [ ] **步骤 5：合并相同文章的渠道证据**

规范 URL 相同直接合并；Google redirect 只有在能还原官方链接或 publisher domain 与 fixed 官方域名唯一匹配且标题达到现有 similarity threshold 时合并。合并后 `discovery_channel="both"`，内容丰富度较高者提供摘要，官方 fixed URL 优先。

- [ ] **步骤 6：运行 GREEN 测试**

运行：`.venv/bin/python -m unittest tests.test_source_registry tests.test_news_search tests.test_news_search_pipeline tests.test_weekly_digest -v`

预期：全部 PASS，Remote 整库 CAS 无需新增旁路写入。

- [ ] **步骤 7：提交任务 3**

```bash
git add trendradar/crawler/rss/source_registry.py \
  trendradar/crawler/rss/fetcher.py trendradar/crawler/news_search.py \
  trendradar/core/rss_snapshot.py trendradar/storage/base.py \
  trendradar/storage/rss_schema.sql trendradar/storage/sqlite_mixin.py \
  trendradar/ai/filter_pipeline.py tests/test_source_registry.py \
  tests/test_news_search.py tests/test_news_search_pipeline.py tests/test_weekly_digest.py
git commit -m "feat(source): 持久化来源类别与发现渠道"
```

### 任务 4：接入并验证国际水稻官方来源

**文件：**
- 创建：`tests/test_official_rice_sources.py`
- 创建：`tests/fixtures/official_rice_sources/cgiar-news-events.html`
- 创建：`tests/fixtures/official_rice_sources/fao-rice.html`
- 创建：`tests/fixtures/official_rice_sources/amis-market-monitor.html`
- 创建：`tests/fixtures/official_rice_sources/usda-ers-rice.html`
- 创建：`tests/fixtures/official_rice_sources/philippines-da.html`
- 创建：`tests/fixtures/official_rice_sources/philrice-news.html`
- 创建：`tests/fixtures/official_rice_sources/india-agri-statistics.html`
- 创建：`tests/fixtures/official_rice_sources/india-food-distribution.html`
- 创建：`tests/fixtures/official_rice_sources/japan-maff-rice.html`
- 创建：`tests/fixtures/official_rice_sources/vietnam-plant-production.html`
- 修改：`trendradar/crawler/rss/web_news.py`
- 修改：`trendradar/crawler/rss/source_registry.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`

- [ ] **步骤 1：保存可追溯的官方列表 fixture**

仅从以下官方入口取得 fixture，并在测试文件常量中记录 `SOURCE_URL` 与抓取日期：

```python
OFFICIAL_LISTS = {
    "cgiar-news": "https://www.cgiar.org/news-events/",
    "fao-rice": "https://www.fao.org/markets-and-trade/commodities/rice/",
    "amis-rice": "https://www.amis-outlook.org/amis-monitoring/monthly-report/en/",
    "usda-ers-rice": "https://www.ers.usda.gov/topics/crops/rice/market-outlook",
    "philippines-da": "https://www.da.gov.ph/category/news/",
    "philrice-news": "https://www.philrice.gov.ph/news/",
    "india-agri-statistics": "https://desagri.gov.in/",
    "india-food-distribution": "https://dfpd.gov.in/",
    "japan-maff-rice": "https://www.maff.go.jp/e/policies/agri/",
    "vietnam-plant-production": "https://www.ppd.gov.vn/",
}
```

如果实际官方站重定向到新的同机构列表页，先更新此常量、规格链接注释和 fixture，再实现；不得用搜索结果页或第三方转载页替代。

- [ ] **步骤 2：为每个 fixture 编写同一解析契约 RED**

```python
def assert_official_items(feed_id, fixture_name, expected_domain):
    items = parse_web_news_html(load_fixture(fixture_name), feed_id, OFFICIAL_LISTS[feed_id])
    assert items
    assert all(item.title and item.published_at for item in items)
    assert all(urlsplit(item.url).hostname.endswith(expected_domain) for item in items)
    assert any(item.summary for item in items)

def load_fixture(name: str) -> str:
    return (
        Path(__file__).parent / "fixtures" / "official_rice_sources" / name
    ).read_text(encoding="utf-8")
```

每个来源另断言：导航/分页链接不会成为新闻、没有日期的卡片被拒绝、结构不匹配抛 `ValueError` 而非成功空集。

- [ ] **步骤 3：运行国际来源 RED**

运行：`.venv/bin/python -m unittest tests.test_official_rice_sources.InternationalOfficialSourceTests -v`

预期：FAIL；对应 `_WebNewsProfile` 尚不存在或解析器返回空。

- [ ] **步骤 4：实现最小 profile 和配置**

为每个来源在 `_PROFILES` 增加官方域名约束、文章 URL regex、日期必需规则和作者；只有测试通过的 feed 在两份 YAML 中 `enabled: true`。CGIAR 使用 `/news-events/`，删除旧 `/news` 地址；代理仅可作为 `fetch_url`，输出 URL 必须恢复 `www.cgiar.org`。

- [ ] **步骤 5：运行 GREEN 与现有网页源回归**

运行：`.venv/bin/python -m unittest tests.test_official_rice_sources.InternationalOfficialSourceTests tests.test_rice_science_links tests.test_news_search_pipeline -v`

预期：全部 PASS。

- [ ] **步骤 6：提交任务 4**

```bash
git add config/config.yaml config/config.en.yaml \
  trendradar/crawler/rss/web_news.py trendradar/crawler/rss/source_registry.py \
  tests/test_official_rice_sources.py tests/fixtures/official_rice_sources
git commit -m "feat(source): 接入国际水稻权威来源"
```

### 任务 5：接入并验证国内水稻官方来源

**文件：**
- 修改：`tests/test_official_rice_sources.py`
- 创建：`tests/fixtures/official_rice_sources/natesc.html`
- 创建：`tests/fixtures/official_rice_sources/ndrc-rice-price.html`
- 创建：`tests/fixtures/official_rice_sources/lswz-control.html`
- 创建：`tests/fixtures/official_rice_sources/lswz-transactions.html`
- 创建：`tests/fixtures/official_rice_sources/stats-grain.html`
- 创建：`tests/fixtures/official_rice_sources/moa-rice-variety.html`
- 创建：`tests/fixtures/official_rice_sources/heilongjiang-agri.html`
- 创建：`tests/fixtures/official_rice_sources/hunan-agri.html`
- 创建：`tests/fixtures/official_rice_sources/jiangxi-agri.html`
- 创建：`tests/fixtures/official_rice_sources/hubei-agri.html`
- 创建：`tests/fixtures/official_rice_sources/jiangsu-agri.html`
- 修改：`trendradar/crawler/rss/web_news.py`
- 修改：`trendradar/crawler/rss/source_registry.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`

- [ ] **步骤 1：固定国内官方列表入口和 fixture**

列表入口采用机构官方栏目，不采用站内搜索结果或媒体转载：全国农技推广网 `https://www.natesc.org.cn/`、国家发改委价格司重要工作 `https://www.ndrc.gov.cn/fzggw/jgsj/jgs/sjdt/`、国家粮食和物资储备局 `https://www.lswz.gov.cn/`、国家统计局最新发布 `https://www.stats.gov.cn/sj/zxfb/`、农业农村部种业管理司现有官方栏目，以及黑龙江、湖南、江西、湖北、江苏省农业农村厅官方新闻/通知栏目。

每个 fixture 记录最终 URL、抓取日期和 SHA-256；如果页面没有稳定日期、正文链接或摘要，该源配置保持 `enabled: false`。

- [ ] **步骤 2：编写国内来源 RED 和水稻筛选边界测试**

```python
def test_provincial_profile_keeps_rice_article_and_drops_unrelated_government_news():
    items = parse_web_news_html(fixture, "hunan-rice-news", list_url)
    assert any("水稻" in (item.title + item.summary) for item in items)
    assert all("畜牧" not in item.title for item in items)

def test_structure_mismatch_is_failure_not_successful_empty():
    with self.assertRaisesRegex(ValueError, "未找到有效官方文章"):
        parse_web_news_html("<html><nav>栏目导航</nav></html>", "natesc-rice", list_url)
```

配置级关键词只用于省级综合栏目召回水稻候选，不替代 AI `species_scope`；国家级稻谷政策/统计专栏不做标题硬过滤。

- [ ] **步骤 3：运行国内来源 RED**

运行：`.venv/bin/python -m unittest tests.test_official_rice_sources.DomesticOfficialSourceTests -v`

预期：FAIL；profile 缺失或综合栏目产生无关条目。

- [ ] **步骤 4：实现 profile、域名约束和启用门槛**

在 `_WebNewsProfile` 增加可选 `required_terms=("水稻", "稻米", "稻谷", "稻作")`，仅给省级综合栏目使用；过滤依据是标题加列表摘要。详情正文仍进入 ArticleContentFetcher 和 AI，不因机构名称自动标记 rice。

- [ ] **步骤 5：运行 GREEN 和配置一致性测试**

运行：`.venv/bin/python -m unittest tests.test_official_rice_sources.DomesticOfficialSourceTests tests.test_source_registry tests.test_weekly_configuration -v`

预期：全部 PASS；没有 fixture 证据的科研机构候选保持 disabled。

- [ ] **步骤 6：提交任务 5**

```bash
git add config/config.yaml config/config.en.yaml \
  trendradar/crawler/rss/web_news.py trendradar/crawler/rss/source_registry.py \
  tests/test_official_rice_sources.py tests/fixtures/official_rice_sources
git commit -m "feat(source): 接入国内水稻官方来源"
```

### 任务 6：实现部分来源失败的周级覆盖语义

**文件：**
- 创建：`trendradar/core/source_coverage.py`
- 创建：`tests/test_weekly_source_coverage.py`
- 修改：`trendradar/core/weekly.py`
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_digest.py`
- 修改：`tests/test_weekly_schedule.py`

- [ ] **步骤 1：编写覆盖状态 RED 测试**

```python
def test_partial_source_failure_keeps_verified_items_and_status():
    snapshot = aggregator.build(run_at)
    assert [item.title for item in snapshot.iter_items()] == ["可验证水稻政策"]
    assert len(snapshot.coverage.successful_source_ids) == 9
    assert len(snapshot.coverage.failures) == 1
    assert snapshot.coverage.failures[0].feed_id == "philrice-news"
    assert snapshot.coverage.failures[0].affected_dates == ("2026-08-06",)

def test_all_enabled_news_sources_unavailable_aborts():
    with self.assertRaisesRegex(RuntimeError, "全部启用新闻来源均不可访问"):
        aggregator.build(run_at)
```

另覆盖：成功空结果计入成功来源；缺日、HTTP、解析异常分别保留；错误摘要不得含 webhook、代理 URL、数据库路径或堆栈。

- [ ] **步骤 2：运行 RED 测试**

运行：`.venv/bin/python -m unittest tests.test_weekly_source_coverage tests.test_weekly_digest -v`

预期：FAIL；旧 aggregator 对任一 failed source 直接中止。

- [ ] **步骤 3：实现周级覆盖对象**

```python
@dataclass(frozen=True)
class SourceFailure:
    feed_id: str
    source_name: str
    failure_type: str
    message: str
    affected_dates: tuple[str, ...]

@dataclass(frozen=True)
class WeeklySourceCoverage:
    successful_source_ids: frozenset[str]
    failures: tuple[SourceFailure, ...]

    @property
    def all_unavailable(self) -> bool:
        return not self.successful_source_ids
```

状态依据 `rss_crawl_status` 的 source/date 事实构建；成功空抓取是 success，缺记录是 missing，failed 行保持 failed。`sanitize_failure_message()` 只输出固定枚举和截断后的用户可读文本。

- [ ] **步骤 4：修改 aggregator 和主线门控**

周快照继续读取可用日库并筛选发布时间；部分来源/日期异常进入 coverage，不丢弃其他已验证条目。只有 `coverage.all_unavailable` 才中止新闻周报。天气门控保持原有严格失败。周一运行后的后续日采集继续写正常状态，不改写历史失败行。

- [ ] **步骤 5：运行 GREEN 和调度回归**

运行：`.venv/bin/python -m unittest tests.test_weekly_source_coverage tests.test_weekly_digest tests.test_weekly_schedule -v`

预期：全部 PASS；周锁、失败重试和 checkpoint 顺序不变。

- [ ] **步骤 6：提交任务 6**

```bash
git add trendradar/core/source_coverage.py trendradar/core/weekly.py \
  trendradar/__main__.py tests/test_weekly_source_coverage.py \
  tests/test_weekly_digest.py tests/test_weekly_schedule.py
git commit -m "feat(weekly): 支持部分来源异常继续出报"
```

### 任务 7：生成四段叙事和证据约束的长卡片摘要

**文件：**
- 修改：`config/ai_analysis_prompt.txt`
- 修改：`config/ai_filter/prompt.txt`
- 修改：`trendradar/ai/filter.py`
- 修改：`trendradar/ai/analyzer.py`
- 修改：`tests/test_ai_filter_rice_modules.py`
- 修改：`tests/test_ai_analyzer_response.py`
- 修改：`tests/test_weekly_pdf_delivery.py`

- [ ] **步骤 1：编写 180–300 字摘要与降级 RED 测试**

```python
def test_full_text_summary_is_structured_and_grounded():
    item = self.classify_one(
        content_level="full_text", evidence=FULL_TEXT_POLICY_EVIDENCE
    )
    assert 180 <= len(item["ai_summary"]) <= 300
    assert "最低收购价" in item["ai_summary"]
    assert "5000万吨" in item["ai_summary"]
    assert "仅基于标题" not in item["ai_summary"]

def test_summary_and_title_only_use_explicit_evidence_labels():
    assert self.classify_one(
        content_level="summary", evidence=SUMMARY_ONLY_EVIDENCE
    )["ai_summary"].startswith("基于摘要：")
    assert self.classify_one(
        content_level="title_only", evidence=TITLE_ONLY_EVIDENCE
    )["ai_summary"].startswith("仅基于标题：")
```

`classify_one()` 在测试类中通过现有 fake AI client 返回 `json_object`，调用真实 `AIFilter.classify_batch()`；测试常量使用以下确定文本，grounding 的第二次响应显式返回相同事实：

```python
FULL_TEXT_POLICY_EVIDENCE = (
    "有关部门明确2026年继续在稻谷主产区执行最低收购价政策，"
    "限定收购总量5000万吨，并分别规定籼稻、粳稻数量和分批下达条件。"
)
SUMMARY_ONLY_EVIDENCE = "研究摘要报告一个水稻抗逆材料及其田间表型。"
TITLE_ONLY_EVIDENCE = "水稻订单生产项目在湖南启动"
```

用 mock grounding response 证明虚构数字、机构、基因、政策效果会令整批失败，不能回退为旧的一句话。

- [ ] **步骤 2：运行摘要 RED 测试**

运行：`.venv/bin/python -m unittest tests.test_ai_filter_rice_modules -v`

预期：FAIL；现有解析器只截断到 300 字，不校验模块事实维度和降级前缀。

- [ ] **步骤 3：更新分类 Prompt 与摘要验证**

Prompt 分模块要求：政策包含措施/对象/条件时间/影响，产业包含事项/参与方/数据进度/产业影响，科研包含问题/材料方法/结果/验证/育种价值。`filter.py` 只做可机械验证的边界：非空、最大 300、证据层级前缀；事实正确性继续由现有逐条 grounding 模型校审，不用关键词猜测。

- [ ] **步骤 4：编写四段叙事 RED 测试**

```python
def test_weekly_requires_policy_industry_research_and_weather_narratives():
    complete = AIAnalysisResult(
        success=True,
        policy_trends="政策判断 [policy:1]",
        industry_trends="产业判断 [industry:1]",
        research_trends="科研判断 [research:1]",
        weather_risks="气象判断 [weather:official]",
    )
    assert has_required_narrative(complete, report_mode="weekly")
    complete.industry_trends = ""
    assert not has_required_narrative(complete, report_mode="weekly")
```

再覆盖空模块使用 `[industry:none]`，非空模块禁止 none；跨命名空间引用和未知证据 ID 均严格失败。

- [ ] **步骤 5：扩展 `AIAnalysisResult` 和 grounding 分区**

```python
@dataclass
class AIAnalysisResult:
    # 既有普通模式字段保持不变
    policy_trends: str = ""
    industry_trends: str = ""
    research_trends: str = ""
    weather_risks: str = ""
```

weekly prompt 输入按 `policy/industry/research/weather` 四个证据区序列化；grounding review 的 allowed IDs 分区校验，任何新闻模块有条目时必须引用其 `[module:N]`。

- [ ] **步骤 6：运行 GREEN 测试**

运行：`.venv/bin/python -m unittest tests.test_ai_filter_rice_modules tests.test_ai_analyzer_response tests.test_weekly_pdf_delivery -v`

预期：全部 PASS；普通 current/daily 的旧叙事字段仍可用。

- [ ] **步骤 7：提交任务 7**

```bash
git add config/ai_analysis_prompt.txt config/ai_filter/prompt.txt \
  trendradar/ai/filter.py trendradar/ai/analyzer.py \
  tests/test_ai_filter_rice_modules.py tests/test_ai_analyzer_response.py \
  tests/test_weekly_pdf_delivery.py
git commit -m "feat(ai): 生成四段周报叙事与结构化摘要"
```

### 任务 8：渲染四模块 PDF、来源状态和长摘要

**文件：**
- 修改：`trendradar/report/weekly_pdf.py`
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_pdf_report.py`
- 修改：`tests/test_weekly_pdf_delivery.py`
- 修改：`tests/test_wework_pdf.py`

- [ ] **步骤 1：编写四模块 HTML 与长摘要 RED 测试**

```python
def test_four_modules_render_once_with_source_metadata_and_status():
    html = render_weekly_pdf_html(
        policy_items=[policy], industry_items=[industry], research_items=[research],
        ai_analysis=analysis, agro_weather=weather, source_coverage=coverage,
        period_label="2026-08-03—2026-08-09", generated_at=run_at,
    )
    assert html.count(policy["url"]) == 1
    assert html.count(industry["url"]) == 1
    assert "水稻产业时事动态" in html
    assert "国际组织" in html and "固定监控源与 Google News" in html
    assert "数据源状态" in html and "PhilRice" in html
    assert long_summary in html
```

再断言三个新闻模块各 ≤20、各自 Top5、跨模块 URL 唯一、气象不占名额、错误区不包含代理地址/路径/密钥。

- [ ] **步骤 2：运行 HTML RED 测试**

运行：`.venv/bin/python -m unittest tests.test_weekly_pdf_report.WeeklyPdfTemplateTests -v`

预期：FAIL；renderer 尚无 industry 和 source coverage 参数。

- [ ] **步骤 3：扩展 renderer 的显式接口**

```python
def render_weekly_pdf_html(
    *, policy_items: Sequence[dict], industry_items: Sequence[dict],
    research_items: Sequence[dict], ai_analysis: AIAnalysisResult,
    agro_weather: AgroWeatherReport,
    source_coverage: WeeklySourceCoverage,
    period_label: str, generated_at: datetime,
) -> str:
    ...
```

标题顺序固定为政策、产业、科研、气象；卡片显示 module rank、证据 ID、主主题、来源类别、原始来源、发现渠道、发布时间、`ai_summary` 和原文链接。删除“普通新闻/三模块”旧文案。

- [ ] **步骤 4：修复长摘要分页样式**

新闻卡片不得固定高度；把 `.news-card { break-inside: avoid-page; }` 改为允许超长卡片自然跨页，同时让标题、meta 和至少首段文本保持在一起。字号维持可读下限，不通过缩小字体塞页。

- [ ] **步骤 5：运行真实 Chromium/Poppler 验证**

运行：`.venv/bin/python -m unittest tests.test_weekly_pdf_report.WeeklyPdfGenerationValidationTests -v`

预期：PASS；生成 A4 多页 PDF，`pdfinfo` 页数 ≥2，`pdftotext` 能提取四个模块标题、长摘要首尾和页眉页脚；PDF ≤20MB，无页面边界溢出断言失败。

- [ ] **步骤 6：验证主线只发送一个 PDF**

运行：`.venv/bin/python -m unittest tests.test_weekly_pdf_delivery tests.test_wework_pdf -v`

预期：PASS；不调用 Markdown/text renderer，partial retry 只续投失败账号，global checkpoint 重试零重复外呼。

- [ ] **步骤 7：提交任务 8**

```bash
git add trendradar/report/weekly_pdf.py trendradar/__main__.py \
  tests/test_weekly_pdf_report.py tests/test_weekly_pdf_delivery.py \
  tests/test_wework_pdf.py
git commit -m "feat(pdf): 输出水稻优先四模块周报"
```

### 任务 9：升级 artifact contract、文档和最终验证

**文件：**
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_pdf_delivery.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 修改：`README.md`
- 修改：`README-EN.md`
- 修改：`docs/news-push-technical-implementation.md`

- [ ] **步骤 1：编写契约失效 RED 测试**

```python
def test_old_three_module_partial_ledger_never_resumes_four_module_pdf():
    old_hash = artifact_hash(template_version="three-module-v1")
    new_hash = artifact_hash(template_version="rice-four-module-v1")
    record_account_action(old_hash, pdf_digest, account_hash)
    assert analyzer._resume_weekly_pdf_delivery(expected_contract_hash=new_hash) is None
    dispatcher.assert_not_called()
```

另分别变更 module enum、species enum、分类 Prompt、兴趣文件、分析 Prompt、MIN_SCORE、schema version、source mapping version，断言 contract hash 改变。

- [ ] **步骤 2：运行 RED 测试**

运行：`.venv/bin/python -m unittest tests.test_weekly_pdf_delivery -v`

预期：至少 PDF 模板版本和来源映射版本尚未进入 hash，测试 FAIL。

- [ ] **步骤 3：集中定义并冻结新 artifact contract**

```python
WEEKLY_ARTIFACT_SCHEMA_VERSION = "rice-four-module-v1"

contract = {
    "version": WEEKLY_ARTIFACT_SCHEMA_VERSION,
    "modules": ["policy", "industry", "research"],
    "species": ["rice", "general", "other_crop"],
    "min_score": 0.5,
    "source_registry_version": SOURCE_REGISTRY_VERSION,
    "classification_prompt": classify_prompt,
    "interests": interests_text,
    "analysis_prompt": analysis_prompt,
}
```

在 run 开始冻结 hash，分类、PDF build 和 delivery 前比较同一 expected hash；action 仍为 `weekly-pdf:<contract_sha256>:<pdf_sha256>:<account_sha256>`，不持久化 webhook。

- [ ] **步骤 4：更新中英文配置和文档**

文档明确：周二至周日静默采集、周一自然周 PDF；政策/产业/科研各 Top20；水稻优先补位；全局阈值 0.5；部分来源异常在 PDF 列出；气象缺失仍中止；卡片摘要按正文/摘要/标题证据降级。删除“政策+科研双榜”“三模块 PDF”和旧 CGIAR `/news` 文案。

- [ ] **步骤 5：运行聚焦集成测试**

运行：

```bash
.venv/bin/python -m unittest \
  tests.test_ai_filter_rice_modules \
  tests.test_ai_filter_module_contract \
  tests.test_ai_filter_module_storage \
  tests.test_weekly_rice_priority \
  tests.test_source_registry \
  tests.test_official_rice_sources \
  tests.test_weekly_source_coverage \
  tests.test_weekly_digest \
  tests.test_weekly_pdf_report \
  tests.test_weekly_pdf_delivery \
  tests.test_weekly_schedule \
  tests.test_wework_pdf -v
```

预期：全部 PASS，无网络的解析测试只读取 fixtures。

- [ ] **步骤 6：运行普通模式兼容与全量测试**

运行：

```bash
.venv/bin/python -m unittest tests.test_daily_delivery tests.test_news_search_pipeline -v
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

预期：全部 PASS；如全量 discovery 重复加载同一模块，必须先修正 discovery 配置，不把重复失败计为多个独立缺陷。

- [ ] **步骤 7：运行静态和部署验证**

```bash
git diff --check
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
rg -n "min_score:\\s*0\\.7|三模块|政策.{0,8}科研双榜|www\\.cgiar\\.org/news(?:[\"'/?]|$)" \
  config trendradar README.md README-EN.md docs/news-push-technical-implementation.md
```

预期：前三项 exit 0；`rg` 无匹配并以 exit 1 结束。规格和计划文档不纳入旧术语门禁，因为它们需要描述迁移背景。

- [ ] **步骤 8：提交任务 9**

```bash
git add trendradar/__main__.py tests/test_weekly_pdf_delivery.py \
  config/config.yaml config/config.en.yaml README.md README-EN.md \
  docs/news-push-technical-implementation.md
git commit -m "docs(weekly): 完成四模块配置与交付说明"
```

- [ ] **步骤 9：验证提交范围和工作树**

```bash
git log --oneline -9
git diff HEAD~9 HEAD --check
git status --short
```

预期：只包含本计划列出的代码、测试、fixture、配置和文档；现有用户数据库、`output/`、PDF 和无关 `index.html` 变动未被暂存或覆盖。

## 规格覆盖自检映射

- 四分类、物种范围、0.5、数据库迁移：任务 1。
- 三榜 Top20/Top5、水稻优先、跨模块占用：任务 2。
- 来源类型、发现渠道、同文合并：任务 3。
- 国际与国内官方来源及真实 fixture：任务 4、5。
- 部分失败继续、全部失败中止、来源状态：任务 6。
- 四段叙事、180–300 字摘要、grounding：任务 7。
- 四模块 PDF、长摘要分页、企业微信 PDF-only：任务 8。
- artifact contract、普通模式兼容、文档与全量验证：任务 9。
