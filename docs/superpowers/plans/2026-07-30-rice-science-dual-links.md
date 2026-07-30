# Rice Science 双链接实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 Rice Science 推送和 HTML 报告增加「官方原文 + 备用检索」双链接，同时保持抓取、存储、去重和 AI 分析行为不变。

**架构：** 使用纯链接转换模块，只为 `rice-science` 的标准 ScienceDirect PII URL 和非空完整标题生成 Semantic Scholar 标题检索 URL。转换结果通过现有 RSS 字典和 AI 报告数据传递到展示层，由企业微信 Markdown 与 HTML 渲染器选择性追加备用链接；生成过程不发起网络请求。

**技术栈：** Python 3.12、标准库 `urllib.parse`、`unittest`、TrendRadar 现有 Markdown/HTML 渲染器、Docker 容器内 `uv.lock` 环境。

---

## 文件结构

- 创建 `trendradar/utils/article_links.py`：校验 Rice Science 官方链接并生成规范化备用 URL。
- 创建 `tests/test_rice_science_links.py`：覆盖链接生成、数据传递、企业微信和 HTML 双链接。
- 修改 `trendradar/__main__.py`：原始 RSS 条目转换时写入 `reader_url`。
- 修改 `trendradar/core/analyzer.py`：非 AI RSS 统计保留 `reader_url`。
- 修改 `trendradar/ai/filter_pipeline.py`：AI RSS 报告条目生成并保留 `reader_url`。
- 修改 `trendradar/report/formatter.py`：企业微信标题格式追加备用检索链接。
- 修改 `trendradar/notification/wework_pdf.py`：可选 PDF 简报模式的重点新闻展示备用链接。
- 修改 `trendradar/report/html.py`：主 HTML 报告的 RSS 区域展示备用链接。
- 修改 `trendradar/report/rss_html.py`：RSS 专用 HTML 报告展示备用链接。
- 修改 `tests/test_wework_pdf.py`：验证可选 PDF 简报中的双链接。

> **2026-07-30 修订：** 任务 1–4 记录首次 Jina Reader 方案的 TDD 实现历史。真实验收发现 Jina 首次返回 HTTP 401，重试的 HTTP 200 正文仍是 `Are you a robot?` 反爬页，不包含论文内容，因此该方案已失效。现行替代方案和验收步骤以任务 5 为准；旧代码片段与旧预期仅用于追溯，不得用于新实现。

### 任务 1：实现严格的备用链接生成器

**文件：**

- 创建：`trendradar/utils/article_links.py`
- 创建：`tests/test_rice_science_links.py`

- [ ] **步骤 1：编写失败的链接生成测试**

```python
import unittest

from trendradar.utils.article_links import build_reader_url


class RiceScienceReaderUrlTests(unittest.TestCase):
    def test_builds_reader_url_and_removes_tracking_query(self):
        result = build_reader_url(
            "rice-science",
            "https://www.sciencedirect.com/science/article/pii/"
            "S1672630826000879?dgcid=rss_sd_all",
        )
        self.assertEqual(
            result,
            "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
            "S1672630826000879",
        )

    def test_rejects_other_feeds_hosts_and_paths(self):
        self.assertEqual(
            build_reader_url(
                "molecular-plant",
                "https://www.sciencedirect.com/science/article/pii/S1672630826000879",
            ),
            "",
        )
        self.assertEqual(
            build_reader_url(
                "rice-science",
                "https://example.com/science/article/pii/S1672630826000879",
            ),
            "",
        )
        self.assertEqual(
            build_reader_url(
                "rice-science",
                "https://www.sciencedirect.com/journal/rice-science",
            ),
            "",
        )
```

- [ ] **步骤 2：运行测试并确认红灯**

运行：

```bash
docker compose -f docker/docker-compose.yml run --rm --no-deps \
  -v /mnt/d/project/TrendRadar:/workspace:ro -w /workspace \
  -e UV_PROJECT_ENVIRONMENT=/app/.venv --entrypoint uv trendradar \
  run --locked --no-sync python -m unittest \
  tests.test_rice_science_links.RiceScienceReaderUrlTests
```

预期：ERROR，报错 `ModuleNotFoundError: No module named 'trendradar.utils.article_links'`。

- [ ] **步骤 3：编写最少实现**

```python
# trendradar/utils/article_links.py
from urllib.parse import urlsplit


RICE_SCIENCE_FEED_ID = "rice-science"
SCIENCEDIRECT_HOST = "www.sciencedirect.com"
PII_PATH_PREFIX = "/science/article/pii/"
JINA_READER_PREFIX = "https://r.jina.ai/http://www.sciencedirect.com"


def build_reader_url(source_id: str, url: str) -> str:
    if source_id != RICE_SCIENCE_FEED_ID or not url:
        return ""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if (parsed.hostname or "").lower() != SCIENCEDIRECT_HOST:
        return ""
    if not parsed.path.startswith(PII_PATH_PREFIX):
        return ""
    pii = parsed.path[len(PII_PATH_PREFIX):].strip("/")
    if not pii or "/" in pii or not pii.isalnum():
        return ""
    return f"{JINA_READER_PREFIX}{PII_PATH_PREFIX}{pii}"
```

- [ ] **步骤 4：运行测试并确认绿灯**

运行任务 1 步骤 2 的同一命令。

预期：`Ran 2 tests`，结果 `OK`。

- [ ] **步骤 5：提交链接生成器**

```bash
git add trendradar/utils/article_links.py tests/test_rice_science_links.py
git commit -m "feat(链接): 添加 Rice Science 备用阅读地址"
```

### 任务 2：在非 AI 与 AI 数据流中传递 `reader_url`

**文件：**

- 修改：`tests/test_rice_science_links.py`
- 修改：`trendradar/__main__.py:16-20,1265-1322`
- 修改：`trendradar/core/analyzer.py:645-660`
- 修改：`trendradar/ai/filter_pipeline.py:12-20,491-548,710-735`

- [ ] **步骤 1：编写失败的数据传递测试**

在 `tests/test_rice_science_links.py` 中增加：

```python
from types import SimpleNamespace

from trendradar.__main__ import NewsAnalyzer
from trendradar.ai.filter_pipeline import AIFilterPipeline
from trendradar.core.analyzer import count_rss_frequency
from trendradar.storage.base import RSSItem


class RiceScienceReaderUrlPropagationTests(unittest.TestCase):
    url = (
        "https://www.sciencedirect.com/science/article/pii/"
        "S1672630826000879?dgcid=rss_sd_all"
    )
    reader_url = (
        "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
        "S1672630826000879"
    )

    def test_raw_rss_conversion_adds_reader_url(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            rss_config={
                "FRESHNESS_FILTER": {"ENABLED": False, "MAX_AGE_DAYS": 1}
            },
            rss_feeds=[{"id": "rice-science", "max_age_days": 1}],
            config={"TIMEZONE": "Asia/Shanghai", "DEBUG": False},
        )
        result = analyzer._convert_rss_items_to_list(
            {
                "rice-science": [
                    RSSItem(
                        title="Test",
                        feed_id="rice-science",
                        url=self.url,
                    )
                ]
            },
            {"rice-science": "Rice Science"},
        )
        self.assertEqual(result[0]["reader_url"], self.reader_url)

    def test_keyword_stats_preserve_reader_url(self):
        stats, _ = count_rss_frequency(
            [{
                "title": "Rice breeding",
                "feed_id": "rice-science",
                "feed_name": "Rice Science",
                "url": self.url,
                "reader_url": self.reader_url,
                "published_at": "",
            }],
            [],
            [],
            quiet=True,
        )
        self.assertEqual(stats[0]["titles"][0]["reader_url"], self.reader_url)

    def test_ai_report_generates_reader_url_only_for_rice_science(self):
        pipeline = AIFilterPipeline(
            {
                "RSS": {
                    "ENABLED": True,
                    "FEEDS": [],
                    "FRESHNESS_FILTER": {"ENABLED": False, "MAX_AGE_DAYS": 1},
                },
                "AI_FILTER": {},
                "FILTER": {},
                "TIMEZONE": "Asia/Shanghai",
            },
            storage_manager=None,
            get_time_func=lambda: None,
        )
        result = pipeline._build_filter_result(
            raw_results=[{
                "tag": "水稻",
                "title": "Rice breeding",
                "source_id": "rice-science",
                "source_name": "Rice Science",
                "source_type": "rss",
                "url": self.url,
                "ranks": [1],
            }],
            tags=[{"tag": "水稻", "priority": 1}],
            total_processed=1,
        )
        self.assertEqual(result.highlights[0]["reader_url"], self.reader_url)
        _, rss_stats, _ = pipeline.convert_to_report_data(result)
        self.assertEqual(
            rss_stats[0]["titles"][0]["reader_url"],
            self.reader_url,
        )
```

- [ ] **步骤 2：运行测试并确认红灯**

运行：

```bash
docker compose -f docker/docker-compose.yml run --rm --no-deps \
  -v /mnt/d/project/TrendRadar:/workspace:ro -w /workspace \
  -e UV_PROJECT_ENVIRONMENT=/app/.venv --entrypoint uv trendradar \
  run --locked --no-sync python -m unittest \
  tests.test_rice_science_links.RiceScienceReaderUrlPropagationTests
```

预期：3 个测试因缺少 `reader_url` 键而失败。

- [ ] **步骤 3：编写最少的数据传递实现**

在 `trendradar/__main__.py` 导入 `build_reader_url`，并在 RSS 字典中增加：

```python
"reader_url": build_reader_url(feed_id, item.url),
```

在 `trendradar/core/analyzer.py` 的 `title_data` 中增加：

```python
"reader_url": item.get("reader_url", ""),
```

在 `trendradar/ai/filter_pipeline.py` 导入 `build_reader_url`，在
`_build_filter_result` 创建标签条目时增加：

```python
"reader_url": build_reader_url(
    r.get("source_id", ""),
    r.get("url", ""),
),
```

在 `convert_to_report_data` 的 `title_entry` 中保留该字段：

```python
"reader_url": item.get("reader_url", ""),
```

- [ ] **步骤 4：运行测试并确认绿灯**

运行任务 2 步骤 2 的同一命令。

预期：`Ran 3 tests`，结果 `OK`。

- [ ] **步骤 5：提交数据传递改动**

```bash
git add tests/test_rice_science_links.py trendradar/__main__.py \
  trendradar/core/analyzer.py trendradar/ai/filter_pipeline.py
git commit -m "feat(推送): 传递 Rice Science 备用阅读链接"
```

### 任务 3：在企业微信与 HTML 中展示双链接

**文件：**

- 修改：`tests/test_rice_science_links.py`
- 修改：`tests/test_wework_pdf.py`
- 修改：`trendradar/report/formatter.py:59-250`
- 修改：`trendradar/notification/wework_pdf.py:45-115`
- 修改：`trendradar/report/html.py:1906-1955,2163-2210`
- 修改：`trendradar/report/rss_html.py:349-390`

- [ ] **步骤 1：编写失败的企业微信与 HTML 渲染测试**

在 `tests/test_rice_science_links.py` 中增加：

```python
from trendradar.report.formatter import format_title_for_platform
from trendradar.report.html import render_html_content
from trendradar.report.rss_html import render_rss_html_content


class RiceScienceDualLinkRenderingTests(unittest.TestCase):
    official_url = (
        "https://www.sciencedirect.com/science/article/pii/S1672630826000879"
    )
    reader_url = (
        "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
        "S1672630826000879"
    )

    def _title_data(self):
        return {
            "title": "Rice breeding",
            "source_name": "Rice Science",
            "time_display": "",
            "count": 1,
            "ranks": [1],
            "rank_threshold": 5,
            "url": self.official_url,
            "mobile_url": "",
            "reader_url": self.reader_url,
            "is_new": False,
        }

    def test_wework_title_contains_official_and_reader_links(self):
        content = format_title_for_platform("wework", self._title_data())
        self.assertIn(f"[Rice breeding]({self.official_url})", content)
        self.assertIn(f"[📖 备用阅读]({self.reader_url})", content)

    def test_wework_without_reader_url_keeps_single_link(self):
        item = self._title_data()
        item["reader_url"] = ""
        content = format_title_for_platform("wework", item)
        self.assertNotIn("备用阅读", content)

    def test_main_and_rss_html_contain_reader_link(self):
        title = self._title_data()
        report_html = render_html_content(
            {
                "stats": [],
                "new_titles": [],
                "failed_ids": [],
                "total_new_count": 0,
            },
            total_titles=1,
            rss_items=[{"word": "水稻", "count": 1, "titles": [title]}],
        )
        rss_html = render_rss_html_content(
            [{
                "title": "Rice breeding",
                "feed_id": "rice-science",
                "feed_name": "Rice Science",
                "url": self.official_url,
                "reader_url": self.reader_url,
            }],
            total_count=1,
        )
        for content in (report_html, rss_html):
            self.assertIn("📖 备用阅读", content)
            self.assertIn(self.reader_url, content)

    def test_rss_html_without_reader_url_keeps_single_link(self):
        content = render_rss_html_content(
            [{
                "title": "Other source",
                "feed_id": "other",
                "feed_name": "Other",
                "url": "https://example.com/article",
            }],
            total_count=1,
        )
        self.assertIn("https://example.com/article", content)
        self.assertNotIn("备用阅读", content)

    def test_rss_html_escapes_reader_url(self):
        content = render_rss_html_content(
            [{
                "title": "Rice breeding",
                "feed_id": "rice-science",
                "feed_name": "Rice Science",
                "url": self.official_url,
                "reader_url": 'https://example.com/?a=1&b="x"',
            }],
            total_count=1,
        )
        self.assertIn(
            "https://example.com/?a=1&amp;b=&quot;x&quot;",
            content,
        )
```

在 `tests/test_wework_pdf.py` 的
`test_preview_contains_brief_summary_and_exactly_five_highlights` 调用
`build_wework_pdf_preview` 前增加：

```python
self.items[0]["reader_url"] = (
    "https://r.jina.ai/http://www.sciencedirect.com/science/article/pii/"
    "S1672630826000879"
)
```

并在现有断言后增加：

```python
self.assertIn("📖 备用阅读", preview)
self.assertIn(self.items[0]["reader_url"], preview)
```

- [ ] **步骤 2：运行测试并确认红灯**

运行：

```bash
docker compose -f docker/docker-compose.yml run --rm --no-deps \
  -v /mnt/d/project/TrendRadar:/workspace:ro -w /workspace \
  -e UV_PROJECT_ENVIRONMENT=/app/.venv --entrypoint uv trendradar \
  run --locked --no-sync python -m unittest \
  tests.test_rice_science_links.RiceScienceDualLinkRenderingTests \
  tests.test_wework_pdf.WeWorkPdfPreviewTests
```

预期：双链接断言失败，现有输出只包含官方链接。

- [ ] **步骤 3：编写最少的企业微信渲染实现**

在 `trendradar/report/formatter.py` 中增加：

```python
reader_url = title_data.get("reader_url", "")
```

并仅在 `platform in ("wework", "bark")` 分支的标题后增加：

```python
if reader_url:
    formatted_title += f" [📖 备用阅读]({reader_url})"
```

在 `trendradar/notification/wework_pdf.py` 的重点新闻循环中增加：

```python
reader_url = _clean_text(item.get("reader_url"))
if reader_url:
    lines.append(f"   [📖 备用阅读]({reader_url})")
```

- [ ] **步骤 4：编写最少的 HTML 渲染实现**

在 `trendradar/report/html.py` 的 RSS 统计条目标题后增加：

```python
reader_url = title_data.get("reader_url", "")
if reader_url:
    rss_html += (
        f' <a href="{html_escape(reader_url)}" target="_blank" '
        'class="rss-link">📖 备用阅读</a>'
    )
```

在独立 RSS 区域的标题渲染中增加：

```python
reader_url = item.get("reader_url", "")
if reader_url:
    standalone_html += (
        f' <a href="{html_escape(reader_url)}" target="_blank" '
        'class="news-link">📖 备用阅读</a>'
    )
```

在 `trendradar/report/rss_html.py` 的官方标题链接后增加：

```python
reader_url = item.get("reader_url", "")
if reader_url:
    html += (
        f' <a href="{html_escape(reader_url)}" target="_blank" '
        'class="rss-link">📖 备用阅读</a>'
    )
```

- [ ] **步骤 5：运行目标测试并确认绿灯**

运行任务 3 步骤 2 的同一命令。

预期：所有目标测试通过。

- [ ] **步骤 6：提交展示层改动**

```bash
git add tests/test_rice_science_links.py tests/test_wework_pdf.py \
  trendradar/report/formatter.py trendradar/notification/wework_pdf.py \
  trendradar/report/html.py \
  trendradar/report/rss_html.py
git commit -m "feat(推送): 展示 Rice Science 官方与备用链接"
```

### 任务 4：完整回归与真实链接抽查

**文件：**

- 验证：`tests/test_rice_science_links.py`
- 验证：`tests/test_direct_first_proxy.py`
- 验证：`tests/test_wework_pdf.py`
- 验证：`tests/test_portable_deployment.sh`

- [ ] **步骤 1：运行全部 Python 单元测试**

```bash
docker compose -f docker/docker-compose.yml run --rm --no-deps \
  -v /mnt/d/project/TrendRadar:/workspace:ro -w /workspace \
  -e UV_PROJECT_ENVIRONMENT=/app/.venv --entrypoint uv trendradar \
  run --locked --no-sync python -m unittest discover -s tests -p "test_*.py"
```

预期：全部测试通过，无 ERROR 或 FAIL。

- [ ] **步骤 2：运行部署检查**

```bash
docker compose -f docker/docker-compose.yml run --rm --no-deps \
  -v /mnt/d/project/TrendRadar:/workspace:ro -w /workspace \
  --entrypoint bash trendradar tests/test_portable_deployment.sh
```

预期：输出 `PASS: 本地部署路径可移植性检查通过`。

- [ ] **步骤 3：执行格式与敏感信息检查**

```bash
git diff --check -- \
  trendradar/utils/article_links.py trendradar/__main__.py \
  trendradar/core/analyzer.py trendradar/ai/filter_pipeline.py \
  trendradar/report/formatter.py trendradar/notification/wework_pdf.py \
  trendradar/report/html.py trendradar/report/rss_html.py \
  tests/test_rice_science_links.py tests/test_wework_pdf.py
! rg -n --hidden -g '!docker/.env' -g '!output/**' \
  '(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})' \
  trendradar/utils/article_links.py trendradar/__main__.py \
  trendradar/core/analyzer.py trendradar/ai/filter_pipeline.py \
  trendradar/report/formatter.py trendradar/notification/wework_pdf.py \
  trendradar/report/html.py trendradar/report/rss_html.py \
  tests/test_rice_science_links.py tests/test_wework_pdf.py
```

预期：`git diff --check` 无输出；敏感信息扫描无匹配。

- [ ] **步骤 4：抽查真实备用检索 URL**

使用容器项目 `.venv` 请求测试文章：

```bash
docker compose -f docker/docker-compose.yml exec -T trendradar \
  /app/.venv/bin/python -c \
  'import requests; from urllib.parse import quote; t="Induction Effect of Chelerythrine on Apoptosis of Sf9 Cells: A Preliminary Investigation Based on Cell Morphology and Activity"; u="https://www.semanticscholar.org/search?q="+quote(t, safe=""); s=requests.Session(); s.trust_env=False; r=s.get(u, timeout=(10,45)); print(r.status_code, len(r.text), t in r.text); r.raise_for_status(); assert t in r.text'
```

预期：HTTP 状态码为 `200`，且响应包含目标论文完整标题。

- [ ] **步骤 5：确认提交范围**

```bash
git status --short
git log -4 --oneline
```

预期：实现相关文件均已提交；原有 `index.html`、`output/` 和未纳入本功能的文档仍保持未暂存状态。

### 任务 5：用 Semantic Scholar 标题检索替换失效的 Jina 入口

**文件：**

- 修改：`trendradar/utils/article_links.py`
- 修改：`trendradar/__main__.py`
- 修改：`trendradar/ai/filter_pipeline.py`
- 修改：`trendradar/report/formatter.py`
- 修改：`trendradar/notification/wework_pdf.py`
- 修改：`trendradar/report/html.py`
- 修改：`trendradar/report/rss_html.py`
- 修改：`tests/test_rice_science_links.py`
- 修改：`tests/test_wework_pdf.py`
- 修改：设计文档和本计划

- [ ] **步骤 1：先修改测试并确认 RED**

覆盖标题 URL 编码、空标题、原始 RSS 和 AI 数据流传标题、全部渲染文案、缺失 `reader_url` 兼容，以及主 HTML 独立 RSS 分支的特殊字符 URL 转义。使用容器内 `/app/.venv` 运行目标测试，预期旧两参数函数、Jina URL 和「📖 备用阅读」断言失败。

- [ ] **步骤 2：最小实现 Semantic Scholar 检索 URL**

将纯函数签名更新为：

```python
def build_reader_url(source_id: str, url: str, title: str) -> str:
```

仅在来源、ScienceDirect 主机、PII 路径和非空标题全部校验通过时返回：

```text
https://www.semanticscholar.org/search?q=<完整标题的 URL 编码>
```

所有数据流调用传入对应标题。`reader_url` 仍不参与存储、去重、新增判断、排序或 AI prompt。

- [ ] **步骤 3：更新展示文案并确认 GREEN**

企业微信 Markdown、可选 PDF 预览、主 HTML RSS 区域、主 HTML 独立 RSS 区域和 RSS 专用 HTML 均使用「🔎 备用检索」。HTML URL 继续使用 `html_escape()` 并带 `target="_blank"`；缺少 `reader_url` 时只显示官方链接。

- [ ] **步骤 4：完整验证**

运行目标测试、全部 `unittest`、`tests/test_portable_deployment.sh`、`git diff --check`、敏感信息扫描，并用任务 4 步骤 4 的命令真实抽查 Semantic Scholar 检索页包含目标标题。
