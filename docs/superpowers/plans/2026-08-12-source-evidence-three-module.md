# 水稻周报来源证据三模块实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 在现有官方来源和周报交付主线上，将新闻确定性收敛为“时事动态、科研进展”两个新闻模块，并与“全国农业气象周报”组成唯一三模块 PDF。

**架构：** 新增一个纯函数来源证据判定器：显式学术来源直接归为科研，其他来源仅在正文明确给出期刊名或完整论文题名证据时归为科研，否则归为时事动态。AI 不再决定模块，只返回是否入选、物种范围、标签、相关性、重要性和证据摘要；SQLite、选择器、三段叙事、PDF 与 artifact contract 使用同一两模块契约。

**技术栈：** Python 3、SQLite、LiteLLM/OpenAI-compatible JSON Object、现有 RSS/网页抓取器、Chromium/Poppler、`unittest`

---

## 文件结构

- 创建：`trendradar/ai/source_evidence.py`，确定性来源/正文证据模块判定。
- 创建：`tests/test_weekly_source_evidence.py`，来源分类、AI 不可覆盖、双榜排序和迁移契约。
- 修改：`config/config.yaml`、`config/config.en.yaml`，为学术期刊和预印本增加显式 `content_category: scholarly`。
- 修改：`config/ai_filter/prompt.txt`、`config/ai_analysis_prompt.txt`、`config/ai_interests.txt`，收敛为 include/exclude、两新闻模块和三段叙事。
- 修改：`trendradar/ai/module_contract.py`、`trendradar/ai/filter.py`、`trendradar/ai/filter_pipeline.py`，移除 AI 模块决策并贯穿确定性模块。
- 修改：`trendradar/storage/ai_filter_schema.sql`、`trendradar/storage/sqlite_mixin.py`，只持久化 `current_events | research`，旧结果和 analyzed 同时失效。
- 修改：`trendradar/core/weekly.py`、`trendradar/ai/analyzer.py`、`trendradar/__main__.py`、`trendradar/report/weekly_pdf.py`，生成双榜、三段叙事和三模块 PDF。
- 修改相关 weekly/AI/storage 测试，删除只服务旧四模块枚举的重复断言，保留调度、锁、逐账号账本、原子 PDF 和普通模式回归。

### 任务 1：确定性模块判定与 AI/SQLite 契约

- [ ] **步骤 1：编写 RED 测试**

在 `tests/test_weekly_source_evidence.py` 覆盖：

```python
def test_scholarly_feed_is_always_research():
    assert classify_source_evidence(
        {"source_id": "rice-science", "content": "正文"},
        {"rice-science": "scholarly"},
    ).module_type == "research"

def test_official_story_requires_explicit_publication_evidence():
    assert classify_source_evidence(
        {"source_id": "irri-news", "content": "研究取得进展"}, {}
    ).module_type == "current_events"
    assert classify_source_evidence(
        {"source_id": "irri-news", "content": "论文发表于 Nature Plants"}, {}
    ).module_type == "research"

def test_ai_response_cannot_choose_or_override_module():
    # AI 返回 include/species/tag/score/importance/summary，不接受 module_type。
```

增加旧 schema 含 `policy/industry/research` 时 results 与 analyzed 同时清空、二次迁移幂等、strict 写读仅允许 `current_events/research` 的测试。

- [ ] **步骤 2：运行 RED**

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_source_evidence \
  tests.test_ai_filter_module_contract \
  tests.test_ai_filter_module_storage -v
```

预期：缺少判定器、旧 AI 仍要求 `module_type`、旧 schema 仍接受 `policy/industry`。

- [ ] **步骤 3：实现最小生产契约**

`source_evidence.py` 输出不可变 `ModuleEvidence(module_type, reason)`；只识别：

- 配置显式 `scholarly`；
- 正文中的受控期刊名；
- “发表于/刊载于/published in/appeared in”发表语句；
- 明确的论文题名引介语句或论文链接锚文本。

仅有 DOI、作者、“研究表明/取得进展/发表成果”不得升级为科研。内容缺失时，非学术来源固定为 `current_events`。

AI JSON 每个 ID 恰好一次，字段为 `id/include/species_scope/tag_id/score/importance_score/summary`；`include=false` 可无 `tag_id` 且不写 result。pipeline 在正文富化后计算模块并传给 parser 元数据，parser 忽略并拒绝模型返回的 `module_type`。

SQLite 新鲜 schema 使用：

```sql
module_type TEXT NOT NULL
  CHECK(module_type IN ('current_events', 'research'))
```

检测旧枚举或缺列时清空 results/analyzed 后重建，不默认映射旧行。模块/存储契约版本递增，并进入 prompt/artifact hash。

- [ ] **步骤 4：运行任务 1 GREEN 并提交**

运行步骤 2 同一命令，预期 0 failures/errors；然后提交：

```bash
git add config trendradar/ai trendradar/storage \
  tests/test_weekly_source_evidence.py \
  tests/test_ai_filter_module_contract.py tests/test_ai_filter_module_storage.py
git commit -m "feat(ai): 按来源证据确定周报模块"
```

### 任务 2：双榜、三段叙事和三模块 PDF

- [ ] **步骤 1：编写 RED 测试**

覆盖以下行为：

```python
def test_weekly_selection_builds_current_events_and_research_top20():
    selection = select_weekly_modules(items, min_score=0.5)
    assert len(selection.current_events) <= 20
    assert len(selection.research) <= 20
    assert all(x["species_scope"] == "rice" for x in selection.current_events)
    assert [x["species_scope"] for x in selection.research][:1] == ["rice"]

def test_pdf_has_exactly_three_primary_modules():
    html = render_weekly_pdf_html(...)
    assert html.count('class="primary-module"') == 3
    assert "时事动态" in html and "科研进展" in html and "全国农业气象周报" in html
    assert "政策动态" not in html and "四模块" not in html
```

补充同一 canonical URL 只出现一次、科研证据优先、两个 Top5、每张卡片显示正文总结/证据层级/排名/主题/链接、两个新闻模块都为空时仍可生成气象-only PDF。

- [ ] **步骤 2：运行 RED**

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_digest tests.test_weekly_three_module \
  tests.test_weekly_pdf_report tests.test_ai_analyzer_response -v
```

预期：旧 `policy/industry/research` 三榜与四段叙事、四模块 HTML 断言失败。

- [ ] **步骤 3：实现双榜和唯一三模块主线**

- `WeeklyNewsSelection` 只保留 `current_events`、`research`。
- 时事动态只接受 `rice`；科研按 `rice` 后 `other_crop` 排序；统一阈值 0.5，各最多 20、各 Top5。
- 全局身份先去重，科研证据身份优先占用，不能重复进入时事动态。
- `AIAnalysisResult` weekly 字段只保留 `current_events_trends`、`research_trends`、`weather_risks`；grounding 证据 ID 为 `[current_events:N]`、`[research:N]`、`[weather:official]`。
- PDF 只接收两个新闻列表与天气，文件名为 `农业育种新闻周报_三模块_...pdf`，一级标题恰好三个。
- 保留来源失败说明、A4/20MB/原子替换、企业微信 file-only、逐账号账本、global checkpoint、周锁和 partial resume。

- [ ] **步骤 4：运行任务 2 GREEN 并提交**

运行步骤 2 同一命令，预期 0 failures/errors；然后提交：

```bash
git add trendradar/core/weekly.py trendradar/ai/analyzer.py \
  trendradar/__main__.py trendradar/report/weekly_pdf.py \
  config/ai_analysis_prompt.txt tests/test_weekly_digest.py \
  tests/test_weekly_three_module.py tests/test_weekly_pdf_report.py \
  tests/test_ai_analyzer_response.py
git commit -m "feat(weekly): 收敛来源证据三模块周报"
```

### 任务 3：来源配置、兼容回归与文档清理

- [ ] **步骤 1：配置学术来源并清理旧四模块测试**

中英文配置仅给明确期刊/预印本来源标记 `content_category: scholarly`；官方、机构、企业、搜索源不标记学术。保留此前已验证的新增来源和解析器，不减少新闻源。

删除 `tests/test_weekly_four_module.py`，将仍有效的来源覆盖、长摘要、部分来源失败、调度/账本断言迁入三模块测试；清除生产配置、Prompt、README 和技术文档中的 `policy/industry`、四段叙事与“四模块”产品语义。

- [ ] **步骤 2：运行聚焦兼容验证**

```bash
/mnt/d/project/trendradar/.venv/bin/python -m unittest \
  tests.test_weekly_source_evidence tests.test_official_rice_sources \
  tests.test_elsevier_full_text tests.test_news_search_pipeline \
  tests.test_weekly_digest tests.test_weekly_three_module \
  tests.test_weekly_pdf_report tests.test_weekly_pdf_delivery \
  tests.test_weekly_schedule tests.test_ai_filter_module_contract \
  tests.test_ai_filter_module_storage tests.test_ai_analyzer_response -v
```

随后运行真实 Chromium/Poppler PDF 验证单项、`bash tests/test_portable_deployment.sh` 与 `git diff --check`。不重复运行全量 discovery，除非聚焦测试揭示跨模块回归。

- [ ] **步骤 3：静态门禁与提交**

```bash
rg -n "四模块|policy_trends|industry_trends|module_type.*policy|module_type.*industry" \
  trendradar config README.md README-EN.md docs/news-push-technical-implementation.md
```

预期生产与用户文档无旧产品契约命中；兼容迁移注释可以明确列出旧枚举。提交：

```bash
git add README.md README-EN.md config trendradar tests \
  docs/news-push-technical-implementation.md \
  docs/superpowers/plans/2026-08-12-source-evidence-three-module.md
git commit -m "refactor(weekly): 清理旧四模块分类主线"
```
