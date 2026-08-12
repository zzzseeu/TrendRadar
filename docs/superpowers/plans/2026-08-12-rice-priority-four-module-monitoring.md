# 水稻优先四模块监控实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框跟踪进度。

**目标：** 在不引入新来源平台的前提下，扩展现有周报为政策、水稻产业动态、科研文献、农业气象四模块，并接入全部已确定的国内外官方来源。

**架构：** 普通官方站继续使用 `web_news._WebNewsProfile`；PDF、专题和文档列表统一使用一个共享 `official_document` 解析路径。现有 RSS 日库、AI 严格 JSON、自然周快照、PDF 原子生成、企业微信逐账号账本和周锁保持为唯一主线。

**技术栈：** Python 3、requests、现有轻量 HTML DOM、SQLite、Chromium/Poppler、unittest。

---

## 文件职责

- `trendradar/crawler/rss/web_news.py`：普通 HTML 列表与共享文档列表解析。
- `trendradar/crawler/rss/fetcher.py`：按现有 `source_type` 调用两种解析路径。
- `config/config.yaml`、`config/config.en.yaml`：来源 URL、类别、发现渠道及启用状态。
- `config/ai_filter/prompt.txt`、`config/ai_interests.txt`：四分类、物种范围和水稻优先规则。
- `trendradar/ai/filter.py`、`filter_pipeline.py`：严格解析和字段透传。
- `trendradar/core/weekly.py`：政策、产业、科研各自排序、去重和 Top20。
- `trendradar/report/weekly_pdf.py`、`trendradar/ai/analyzer.py`：四模块叙事、卡片摘要和来源状态。
- `trendradar/__main__.py`：沿用现有周报编排、锁、检查点和 PDF 投递。
- `tests/test_official_rice_sources.py`：新增来源离线解析契约。
- `tests/test_weekly_four_module.py`：四模块选择、摘要和 PDF 主链契约。

### 任务 1：四分类与水稻优先选择

- [ ] 新增失败测试：严格 JSON 必须返回 `policy|industry|research|exclude` 和 `rice|other_crop|not_applicable`；产业非水稻拒绝；阈值 `0.49` 排除、`0.50` 纳入；政策和产业只收水稻，科研先水稻后其他作物补位。
- [ ] 用项目虚拟环境运行 `tests.test_weekly_four_module`，确认失败原因是缺少 `industry/species_scope` 契约。
- [ ] 最小修改 Prompt、严格解析、SQLite 字段透传和 `select_weekly_modules()`；去重优先级为政策、产业、科研，各模块独立 Top20/Top5。
- [ ] 重跑 `tests.test_weekly_four_module tests.test_ai_filter_module_contract tests.test_weekly_digest`，确认通过。
- [ ] 提交 `feat(weekly): 增加水稻产业四模块分类`。

### 任务 2：复用现有解析器接入普通官方站

- [ ] 保存经过在线验证的最小 HTML fixture；新增失败测试，要求真实文章 URL、标题、发布日期可提取，导航和分页链接不得成为新闻。
- [ ] 先覆盖已在线确认的 CGIAR、越南 PPD、发改委、统计局、农业农村部、黑龙江、湖北；再覆盖页面结构相同但需代理或备用入口的菲律宾农业部、PhilRice、全国农技推广中心、国家粮储局、湖南、江苏。
- [ ] 仅扩展 `_WebNewsProfile`：增加精确 URL regex、日期 URL 格式、可选水稻召回词和官方 URL 还原；不新增来源注册平台。
- [ ] 在两份 YAML 中加入来源元数据。访问入口或 fixture 未验证的来源保留为 `enabled: false`；江西保持禁用直到稳定列表入口确定。
- [ ] 运行 `tests.test_official_rice_sources tests.test_news_search_pipeline tests.test_weekly_configuration`，确认通过。
- [ ] 提交 `feat(source): 复用网页解析器接入水稻官方来源`。

### 任务 3：共享文档列表适配

- [ ] 新增失败测试，覆盖 AMIS 和日本 MAFF 的 PDF 标题、发布日期、官方 URL；覆盖 FAO、USDA ERS、印度来源在缺少稳定文章列表时 fail closed，而不是把导航返回为新闻。
- [ ] 在 `web_news.py` 增加一个共享 `parse_official_document_html()`；配置使用 `source_type: official_document`，不为单站创建类或数据库。
- [ ] 只有标题、日期和官方文档链接均可验证的来源才启用；其余来源保留禁用配置并记录准确原因。
- [ ] 运行 `tests.test_official_rice_sources`，并确认结构变化返回失败而非成功空集。
- [ ] 提交 `feat(source): 支持官方水稻文档列表`。

### 任务 4：四模块叙事、正文摘要和来源状态

- [ ] 新增失败测试：政策、产业、科研三类卡片各显示 180–300 字正文摘要；证据不足时明确显示“基于摘要”或“仅基于标题”；四段叙事均使用稳定 evidence ID。
- [ ] 新增失败测试：单个或多个新闻来源失败仍生成 PDF，并列出来源和日期；全部新闻来源失败或气象周报缺失时中止。
- [ ] 最小扩展现有 AI 结果与 PDF 模板；PDF 不二次调用模型，不生成通用 HTML，不改变 PDF-only 企业微信投递、逐账号账本、周锁和 checkpoint 时序。
- [ ] 运行 `tests.test_weekly_four_module tests.test_weekly_pdf_report tests.test_weekly_pdf_delivery tests.test_weekly_schedule`，确认通过。
- [ ] 提交 `feat(weekly): 输出四模块水稻周报`。

### 任务 5：验证与文档收口

- [ ] 更新中英文 README 和技术说明：周二至周日静默采集、周一自然周 PDF、四模块、三类 Top20、全局阈值 0.5、部分来源失败继续输出。
- [ ] 运行聚焦测试：`tests.test_official_rice_sources tests.test_weekly_four_module tests.test_weekly_pdf_report tests.test_weekly_pdf_delivery tests.test_weekly_schedule tests.test_news_search_pipeline`。
- [ ] 运行真实 Chromium/Poppler 多页 PDF 验证，以及 `bash tests/test_portable_deployment.sh`、`git diff --check`。
- [ ] 只在聚焦测试全部通过后运行一次全量 discovery；记录明确的 `Ran ... OK` 和退出码。
- [ ] 删除被新契约取代的重复测试，保留锁、账本、远程 SQLite、自然周窗口和普通模式兼容测试。
- [ ] 提交 `docs(weekly): 收口水稻四模块监控说明`。
