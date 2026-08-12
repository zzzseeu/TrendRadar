# 周报论文跨来源去重实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 使用 DOI、PII 和明确完整论文名合并周报科研进展中的同论文多来源卡片，并优先保留期刊原文。

**架构：** 在新的纯函数模块中提取标准 DOI、标准 PII、完整论文名和论文身份；`select_weekly_modules()` 在科研模块排序和 Top 20 截断前按论文身份合并，并把非主卡片保存为 `related_sources`。PDF 模板只渲染一个主卡片，并展示经过 URL 安全校验的关联来源。

**技术栈：** Python 3.12、`re`、现有 `unittest`、现有周报选择器与 HTML/PDF 模板。

---

## 文件结构

- 创建：`trendradar/core/paper_identity.py` —— DOI、明确论文名、论文身份提取及来源优先级纯函数。
- 修改：`trendradar/core/weekly.py` —— 仅在 research 模块调用论文身份合并，产生 `related_sources`。
- 修改：`trendradar/report/weekly_pdf.py` —— 在主卡片内显示去重后的关联来源链接。
- 修改：`tests/test_weekly_three_module.py` —— 真实 Molecular Plant/作科所重复、DOI/论文名边界和排序回归。
- 修改：`tests/test_weekly_pdf_report.py` —— 单卡片与关联来源 HTML 契约。

### 任务 1：论文身份与科研模块合并

**文件：**
- 创建：`trendradar/core/paper_identity.py`
- 修改：`trendradar/core/weekly.py:80-188`
- 测试：`tests/test_weekly_three_module.py`

- [ ] **步骤 1：编写失败的 DOI 去重测试**

在 `tests/test_weekly_three_module.py` 新增：

```python
def test_research_deduplicates_journal_and_institution_story_by_doi(self):
    journal = _item(
        "research", 1,
        title="Strigolactones restrict sugar acquisition in rice tiller buds",
        source_id="molecular-plant", source_name="Molecular Plant",
        url="https://www.sciencedirect.com/science/article/pii/S1674205226002613",
        content_excerpt="DOI: https://doi.org/10.1016/j.molp.2026.08.003",
    )
    institution = _item(
        "research", 2,
        title="作科所揭示独脚金内酯调控水稻分蘖新机制",
        source_id="caas-crop-research", source_name="中国农科院作科所",
        url="https://ics.caas.cn/example.htm",
        content_excerpt=(
            "原文链接：https://www.cell.com/molecular-plant/fulltext/"
            "S1674-2052(26)00261-3；doi:10.1016/j.molp.2026.08.003"
        ),
    )

    selection = select_weekly_modules([institution, journal], min_score=0.5)

    self.assertEqual(len(selection.research), 1)
    self.assertEqual(selection.research[0]["source_id"], "molecular-plant")
    self.assertEqual(selection.research[0]["paper_doi"], "10.1016/j.molp.2026.08.003")
    self.assertEqual(
        selection.research[0]["related_sources"],
        [{"source_name": "中国农科院作科所", "url": "https://ics.caas.cn/example.htm"}],
    )
```

- [ ] **步骤 2：运行测试确认 RED**

运行：

```bash
PYTHONPATH=. /mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_three_module.WeeklyModuleSelectionCompatibilityTests.test_research_deduplicates_journal_and_institution_story_by_doi -v
```

预期：FAIL，research 实际长度为 2。

- [ ] **步骤 3：补充论文名与冲突边界 RED**

新增三个独立测试（共同复用 `_item()`）：

```python
def test_research_deduplicates_by_explicit_full_paper_title_without_doi(self):
    title = "A complete rice breeding paper title"
    journal = _item("research", 1, title=title, source_id="rice-science")
    story = _item(
        "research", 2, title="机构发布科研进展",
        content_excerpt=f"论文题为《{title}》",
    )
    selection = select_weekly_modules([story, journal], min_score=0.5)
    self.assertEqual(len(selection.research), 1)

def test_same_news_title_with_distinct_dois_is_not_merged(self):
    rows = [
        _item("research", 1, title="Same title", content_excerpt="doi:10.1000/one"),
        _item("research", 2, title="Same title", content_excerpt="doi:10.1000/two"),
    ]
    self.assertEqual(
        len(select_weekly_modules(rows, min_score=0.5).research), 2
    )

def test_current_events_keeps_existing_url_guid_title_identity(self):
    rows = [
        _item("current_events", 1, title="同一动态", url="", guid="same"),
        _item("current_events", 2, title="另一个标题", url="", guid="same"),
    ]
    self.assertEqual(
        len(select_weekly_modules(rows, min_score=0.5).current_events), 1
    )
```

论文名测试使用期刊原文标题，以及机构正文中的 `论文题为《完整标题》`；DOI 冲突测试断言保留两条。

- [ ] **步骤 4：实现最小论文身份纯函数**

在 `trendradar/core/paper_identity.py` 实现：

```python
DOI_RE = re.compile(r"(?i)\b10\.\d{4,9}/[-._;()/:A-Z0-9]+")

def normalize_doi(value: object) -> str:
    match = DOI_RE.search(str(value or ""))
    return match.group(0).rstrip(".,;:)]}").lower() if match else ""

def extract_paper_doi(item: Mapping[str, object]) -> str:
    for field in ("url", "guid", "title", "summary", "content_excerpt", "ai_summary"):
        doi = normalize_doi(item.get(field))
        if doi:
            return doi
    return ""

def paper_identity(item: Mapping[str, object]) -> tuple[str, str] | None:
    doi = extract_paper_doi(item)
    if doi:
        return ("doi", doi)
    title = extract_explicit_paper_title(item)
    return ("paper_title", normalize_paper_title(title)) if title else None
```

`extract_explicit_paper_title()` 只接受：直接学术来源的自身标题、`论文/文章/研究题为《标题》`、英文 `Please cite this article as: Title (年份)`；不得做模糊匹配。

- [ ] **步骤 5：在科研模块合并来源**

在 `trendradar/core/weekly.py` 增加 research 专用合并：

```python
def _deduplicate_research_items(items: list[dict]) -> list[dict]:
    # 有 paper_identity 时按论文身份分组，否则沿用 report_item_identity。
    # 组内优先 scholarly source，其次使用 weekly_module_sort_key。
    # 主卡片写 paper_doi/paper_title；其他安全来源写 related_sources。
```

`select_weekly_modules()` 的 research 路径使用该函数；current_events 继续使用 `_deduplicate_module_items()`。

- [ ] **步骤 6：运行任务 1 测试确认 GREEN**

运行：

```bash
PYTHONPATH=. /mnt/d/project/trendradar/.venv/bin/python -m unittest tests.test_weekly_three_module -v
```

预期：全部通过，真实重复只保留 Molecular Plant 主卡片。

- [ ] **步骤 7：提交任务 1**

```bash
git add trendradar/core/paper_identity.py trendradar/core/weekly.py tests/test_weekly_three_module.py
git commit -m "fix(weekly): 按 DOI 和论文名合并科研来源"
```

### 任务 2：PDF 关联来源展示

**文件：**
- 修改：`trendradar/report/weekly_pdf.py:70-108`
- 测试：`tests/test_weekly_pdf_report.py`

- [ ] **步骤 1：编写失败的单卡片渲染测试**

新增测试构造一条 research 主卡片，包含：

```python
"related_sources": [
    {"source_name": "中国农科院作科所", "url": "https://ics.caas.cn/example.htm"}
]
```

断言期刊主链接、关联来源链接各出现一次，同论文主标题只出现一次，HTML 包含 `关联来源：中国农科院作科所`。

- [ ] **步骤 2：运行测试确认 RED**

运行：

```bash
PYTHONPATH=. /mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_pdf_report.WeeklyPdfTemplateTests.test_research_card_renders_related_institution_source_once -v
```

预期：FAIL，HTML 中没有关联来源。

- [ ] **步骤 3：实现关联来源安全渲染**

在 `_render_news_card()` 中：

```python
related_links = [
    _link(source.get("url"), source.get("source_name") or "关联来源")
    for source in item.get("related_sources") or []
    if isinstance(source, dict)
]
```

过滤空链接、与主 URL 相同的链接及重复链接；输出一行 `关联来源：来源名`。继续复用 `_safe_http_url()` 与 `_text()`，禁止渲染非 HTTP(S) URL。

- [ ] **步骤 4：运行任务 2 测试确认 GREEN**

运行：

```bash
PYTHONPATH=. /mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_pdf_report.WeeklyPdfTemplateTests \
  tests.test_weekly_three_module -v
```

预期：全部通过。

- [ ] **步骤 5：在正式 Docker 镜像验证 PDF**

运行只读挂载、断网的 PDF 模板测试：

```bash
docker run --rm --network none \
  -v "$PWD:/workspace:ro" -w /workspace \
  --entrypoint /app/.venv/bin/python docker-trendradar:latest \
  -m unittest tests.test_weekly_pdf_report.WeeklyPdfGenerationValidationTests -v
```

预期：Chromium/Poppler 测试通过，PDF 为 A4 且多页。

- [ ] **步骤 6：提交任务 2**

```bash
git add trendradar/report/weekly_pdf.py tests/test_weekly_pdf_report.py
git commit -m "fix(pdf): 展示论文关联来源"
```

### 任务 3：聚焦回归与合并准备

**文件：**
- 验证：`trendradar/core/paper_identity.py`
- 验证：`trendradar/core/weekly.py`
- 验证：`trendradar/report/weekly_pdf.py`

- [ ] **步骤 1：运行聚焦回归**

```bash
PYTHONPATH=. /mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_three_module \
  tests.test_weekly_source_evidence \
  tests.test_weekly_pdf_report \
  tests.test_weekly_pdf_delivery -v
```

预期：除宿主机 Chromium 专项外全部通过；Chromium 专项以任务 2 的 Docker 结果为准。

- [ ] **步骤 2：静态检查**

```bash
git diff --check
/mnt/d/project/trendradar/.venv/bin/python -m py_compile \
  trendradar/core/paper_identity.py trendradar/core/weekly.py \
  trendradar/report/weekly_pdf.py
```

预期：均退出 0。

- [ ] **步骤 3：确认范围**

```bash
git status --short
git diff --stat main...HEAD
```

预期：仅设计/计划、论文身份、周报选择器、PDF 模板及对应测试发生变化；无 `output`、`.env`、缓存或数据库文件。
