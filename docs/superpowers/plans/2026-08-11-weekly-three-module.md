# 每周 PDF 三模块与统一相关性标准实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将每周农业育种 PDF 收敛为“农业育种政策、农业育种科研文献、农业气象周报”三模块，政策与科研各自 TOP20，并让所有模式只使用 `relevance_score >= 0.5` 这一条准入标准。

**架构：** 每日采集与自然周严格快照保持不变；AI 对快照中每个候选穷尽式输出 `policy | research | exclude`，仅前两类写入带 `module_type` 的 SQLite 结果。周选择器先按政策优先全局去重，再按同一排序键分别截取两个 TOP20；AI 叙事和 PDF 直接消费同一选择结果，企业微信仍只发送一个 PDF。旧 Prompt/阈值分类会通过迁移和 Prompt 哈希整体失效，新 PDF 在临时文件验证后原子替换，最后才精确清理旧布局产物。

**技术栈：** Python 3.12、SQLite、OpenAI 兼容结构化 JSON、Jinja/HTML 字符串模板、Headless Chromium、Poppler、unittest、Docker、企业微信文件消息。

---

## 全局约束

- 实现只在 `/mnt/d/project/trendradar/.worktrees/weekly-three-module` 的 `feature/weekly-three-module` 分支完成，不修改主工作树现有运行产物。
- 项目 Python 命令使用镜像内 `/app/.venv/bin/python`；源代码从该 worktree 只读或读写挂载到 `/workspace`，不调用系统 Python。
- 周报内容窗口仍为北京时间上一自然周 `[周一 00:00, 下周一 00:00)`；first-seen、抓取时间和 checkpoint 不得重新成为内容资格规则。
- `policy` 与 `research` 的唯一准入线来自 `AI_FILTER.MIN_SCORE`；生产中文配置、英文配置和 loader 缺省均为 `0.5`，不得增加 weekly 覆盖。
- 企业宣传、会议动态、领导调研、行业倡议和泛化宣传不是硬排除理由；依据核心政策或科研事实分类与评分。只有无关、重复、无法分类或内容无效时使用 `exclude`。
- 政策优先去重发生在 TOP20 截断前；同一稳定身份进入政策候选后，不得再进入科研，即使该政策候选最终排在第 21 名之后。
- `exclude` 必须证明输入已被 AI 处理，但不得写入 `ai_filter_results`；其 `ai_filter_analyzed_news.matched` 为 0。
- 原始 RSS 日库、来源状态、first-seen/outbox、provenance、调度 checkpoint 和逐账号投递账本均为权威状态，不作为“缓存”删除。
- 不新增政府专用爬虫、不恢复文字消息、不打开图形浏览器、不引入 weekly 专用阈值或第二套候选窗口。
- 每个任务先获得有效 RED，再做最小 GREEN；删除被新契约替代的重复测试，但保留严格存储、失败重试、周锁、真实分页和逐账号账本测试。

## 文件结构

### 新建

- `trendradar/ai/module_contract.py`：集中定义三个 AI 分类值、两个可持久化模块和验证辅助函数。
- `tests/test_ai_filter_module_contract.py`：分类完整性、统一阈值、Prompt/兴趣/政策主题静态契约。
- `tests/test_ai_filter_module_storage.py`：SQLite 迁移、Local/Remote 严格读写、回滚和旧缓存失效。
- `tests/test_weekly_three_module.py`：政策优先去重、双 TOP20、报告字段透传、主链三模块一致性。

### 修改

- `config/ai_interests.txt`、`config/ai_filter/prompt.txt`、`config/ai_filter/extract_prompt.txt`、`config/ai_filter/update_tags_prompt.txt`：广义政策、政策优先、穷尽式模块输出并移除载体形式硬排除。
- `config/ai_analysis_prompt.txt`：政策趋势、科研进展和气象风险分别叙述。
- `config/config.yaml`、`config/config.en.yaml`、`trendradar/core/loader.py`：统一 `0.5`，扩展现有政策搜索 topics。
- `trendradar/ai/filter.py`：严格模块解析、全 ID 覆盖、repair 协议，删除标题分数钳制。
- `trendradar/ai/filter_pipeline.py`：模块字段贯穿、统一分数过滤、严格写后读回。
- `trendradar/ai/analyzer.py`：模块化周报叙事输入和结果字段。
- `trendradar/storage/ai_filter_schema.sql`、`trendradar/storage/sqlite_mixin.py`、`trendradar/storage/base.py`：持久化、迁移、失效和严格读写 `module_type`。
- `trendradar/core/weekly.py`：模块感知选择器、政策优先去重和确定排序。
- `trendradar/__main__.py`：同一双模块选择结果贯穿叙事、PDF、投递；weekly 不再生成通用 dashboard。
- `trendradar/report/weekly_pdf.py`、`trendradar/report/pdf.py`：三模块模板、两个 TOP5、原子 HTML/PDF 替换。
- `README.md`、`README-EN.md`、`docs/news-push-technical-implementation.md`：只描述当前每周三模块 PDF 主线。
- 与新字段直接相关的既有 fixture：`tests/test_daily_delivery.py`、`tests/test_daily_delivery_review3.py`、`tests/test_daily_delivery_review4.py`、`tests/test_daily_delivery_review5.py`、`tests/test_daily_delivery_review6.py`、`tests/test_weekly_time_rule.py`、`tests/test_news_search_pipeline.py`、`tests/test_weekly_digest.py`、`tests/test_weekly_pdf_report.py`、`tests/test_weekly_pdf_delivery.py`、`tests/test_weekly_schedule.py`、`tests/test_wework_pdf.py`。

### 删除

- `tests/test_ai_filter_title_only_score_band.py`：旧 `0.70–0.78` 标题分数钳制专用测试。
- `tests/test_crop_breeding_filter_rules.py`：内容迁入模块契约测试后删除，避免两套 Prompt/阈值静态契约。
- `tests/test_weekly_report_output.py`：旧 weekly 通用 HTML 与文字通知契约已经不属于产品主线。
- 上述既有测试文件中被新三模块测试完全覆盖的全局 Top20、topic round-robin、重复 20MB/Chromium 缺失、重复多账号 mock 状态机用例。

## 任务 1：建立模块分类、Prompt 与统一阈值契约

**文件：**
- 创建：`trendradar/ai/module_contract.py`
- 创建：`tests/test_ai_filter_module_contract.py`
- 修改：`trendradar/ai/filter.py`
- 修改：`trendradar/core/loader.py`
- 修改：`config/config.yaml`
- 修改：`config/config.en.yaml`
- 修改：`config/ai_interests.txt`
- 修改：`config/ai_filter/prompt.txt`
- 修改：`config/ai_filter/extract_prompt.txt`
- 修改：`config/ai_filter/update_tags_prompt.txt`
- 删除：`tests/test_ai_filter_title_only_score_band.py`
- 删除：`tests/test_crop_breeding_filter_rules.py`
- 测试：`tests/test_ai_filter_classification_resilience.py`
- 测试：`tests/test_daily_delivery_review3.py`
- 测试：`tests/test_daily_delivery_review4.py`

- [ ] **步骤 1：用失败测试锁定唯一配置和广义政策 Prompt**

在 `tests/test_ai_filter_module_contract.py` 解析两份 YAML，并直接读取 Prompt/兴趣文件：

```python
def test_global_min_score_and_policy_topics_are_single_source_of_truth(self):
    self.assertEqual(self.zh_config["ai_filter"]["min_score"], 0.5)
    self.assertEqual(self.en_config["ai_filter"]["min_score"], 0.5)
    self.assertEqual(self.loaded["AI_FILTER"]["MIN_SCORE"], 0.5)
    topic_ids = {topic["id"] for topic in self.zh_config["rss"]["news_search"]["topics"]}
    self.assertTrue({
        "seed-policy",
        "breeding-policy-support",
        "breeding-policy-implementation",
    }.issubset(topic_ids))

def test_publicity_meetings_and_inspections_are_not_hard_exclusions(self):
    combined = self.interests + self.classify_prompt + self.extract_prompt + self.update_prompt
    self.assertIn("政策优先", combined)
    self.assertIn("领导调研", combined)
    self.assertNotIn("会议宣传、培训招生和纯营销内容", combined)
```

- [ ] **步骤 2：用失败测试锁定穷尽式严格分类**

构造三个输入和合法响应，断言只返回两条可持久化结果但三个输入全部被接受；再覆盖遗漏 ID、重复/未知 ID、未知模块、缺标签、NaN/布尔分数和二次 repair 失败：

```python
response = json.dumps([
    {"id": 1, "module_type": "policy", "tag_id": 11,
     "score": 0.50, "importance_score": 0.80, "summary": "政策部署"},
    {"id": 2, "module_type": "research", "tag_id": 12,
     "score": 0.49, "importance_score": 0.90, "summary": "科研成果"},
    {"id": 3, "module_type": "exclude",
     "score": 0.10, "importance_score": 0.10, "summary": "内容无关"},
])
results = ai_filter._parse_classify_response(response, titles, tags, strict=True)
self.assertEqual([row["module_type"] for row in results], ["policy", "research"])
self.assertEqual([row["relevance_score"] for row in results], [0.50, 0.49])
```

- [ ] **步骤 3：运行 RED 并保存失败证据**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_ai_filter_module_contract tests.test_ai_filter_classification_resilience tests.test_daily_delivery_review3 tests.test_daily_delivery_review4 -v
```

预期：新增模块字段、全 ID 覆盖、统一 `0.5`、旧硬排除和旧标题钳制断言失败；已有数值严格校验继续通过。

- [ ] **步骤 4：实现集中模块常量和严格解析**

`trendradar/ai/module_contract.py` 使用唯一常量：

```python
POLICY = "policy"
RESEARCH = "research"
EXCLUDE = "exclude"
CLASSIFICATION_MODULE_TYPES = frozenset({POLICY, RESEARCH, EXCLUDE})
PERSISTED_MODULE_TYPES = frozenset({POLICY, RESEARCH})
```

在 `_parse_classify_response()` 中对严格响应逐条验证后执行：

```python
if seen_news_ids != title_ids:
    missing = sorted(title_ids - seen_news_ids)
    raise _InvalidClassificationResponse(f"严格模式分类遗漏 news id: {missing}")

if module_type == EXCLUDE:
    continue

best_per_news[news_id] = {
    "news_item_id": news_id,
    "module_type": module_type,
    "tag_id": tag_id,
    "relevance_score": score,
    "importance_score": importance,
    "ai_summary": normalized_summary,
}
```

删除 `TITLE_ONLY_SCORE_MIN/MAX` 和所有证据层级分数钳制。修复 Prompt 必须仍返回与输入等长的数组，不能把非空批次的 `[]` 当成功。

- [ ] **步骤 5：更新兴趣、Prompt、阈值与政策搜索主题**

三组政策 topic 沿用现有 `{id, zh, en}` 结构；`seed-policy` 覆盖审定/登记/PVP/法规，`breeding-policy-support` 覆盖振兴/种质/项目/财政补贴，`breeding-policy-implementation` 覆盖会议部署/调研/行业倡议/企业响应。将 loader 缺省改为：

```python
"MIN_SCORE": float(ai_filter.get("min_score", 0.5)),
```

分类 Prompt 明确每个输入恰好返回一条记录；`policy/research` 必须有标签；`exclude` 可以没有标签。`extract/update` 只禁止把“宣传稿”这种载体本身生成为标签，不禁止提取其中真实政策或科研主题。

- [ ] **步骤 6：迁移测试并删除旧 TDD 文件**

将“原始 0.1/0.5/0.9 分数不按 `content_level` 改写”移入模块契约测试；把 review3 的两个输入合法 fixture 改为 `policy + exclude` 全覆盖；给 review4 数值 fixture 补合法 `module_type`。删除两个旧测试文件，不复制其过时断言。

- [ ] **步骤 7：运行 GREEN 并提交**

运行步骤 3 同一命令，预期全部 `OK`；再运行：

```bash
git diff --check
git add trendradar/ai/module_contract.py trendradar/ai/filter.py trendradar/core/loader.py config/config.yaml config/config.en.yaml config/ai_interests.txt config/ai_filter/prompt.txt config/ai_filter/extract_prompt.txt config/ai_filter/update_tags_prompt.txt tests/test_ai_filter_module_contract.py tests/test_ai_filter_classification_resilience.py tests/test_daily_delivery_review3.py tests/test_daily_delivery_review4.py tests/test_ai_filter_title_only_score_band.py tests/test_crop_breeding_filter_rules.py
git commit -m "feat(ai): 统一周报模块分类与相关性标准"
```

## 任务 2：持久化模块并使旧分类完整失效

**文件：**
- 创建：`tests/test_ai_filter_module_storage.py`
- 修改：`trendradar/storage/ai_filter_schema.sql`
- 修改：`trendradar/storage/sqlite_mixin.py`
- 修改：`trendradar/storage/base.py`
- 修改：`tests/test_ai_filter_rule_invalidation.py`
- 修改：`tests/test_daily_delivery.py`
- 修改：`tests/test_daily_delivery_review5.py`
- 修改：`tests/test_daily_delivery_review6.py`
- 修改：`tests/test_weekly_time_rule.py`

- [ ] **步骤 1：编写旧库迁移和 Local 严格存储 RED**

测试直接创建缺少 `module_type` 的旧 `ai_filter_results` 和一条对应 `ai_filter_analyzed_news`，打开新 backend 后断言：列已存在、两张旧缓存表均为空、第二次迁移幂等；随后写入 policy/research 并读回模块。另用 SQLite trigger 让第二条 INSERT 失败，断言 results 与 analyzed 同事务回滚。

- [ ] **步骤 2：编写严格坏值与 Remote CAS RED**

覆盖普通/严格写拒绝 `exclude`、未知值和 NULL；strict read 遇坏模块 fail closed。Remote fake S3 覆盖成功 CAS 后新 observer 读回模块，以及 CAS 冲突、PUT 失败、batch end 失败恢复 before-image 且不残留 analyzed 状态。

- [ ] **步骤 3：运行存储 RED**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_ai_filter_module_storage tests.test_ai_filter_rule_invalidation tests.test_daily_delivery_review5 tests.test_daily_delivery_review6 tests.test_weekly_time_rule -v
```

预期：缺列、旧 analyzed 未清、写入/读回不带模块和 Remote observer 字段缺失导致失败。

- [ ] **步骤 4：实现 schema、幂等迁移和严格读写**

新库 schema 增加：

```sql
module_type TEXT NOT NULL
    CHECK(module_type IN ('policy', 'research')),
```

旧库迁移使用 nullable CHECK 列，随后在同一迁移事务中清理该库的 `ai_filter_results` 与 `ai_filter_analyzed_news`；绝不使用 `DEFAULT 'research'`。所有 INSERT、hotlist/RSS SELECT 映射和严格写后读回 key 均加入 `module_type`：

```python
key = (
    result["news_item_id"],
    result.get("source_type", "hotlist"),
    result["tag_id"],
    result["module_type"],
)
```

strict read 对 NULL/非法值抛 `RuntimeError`，普通写遇非法值返回失败而不是用旧 `IntegrityError: pass` 保留错误结果。

- [ ] **步骤 5：删除旧 Prompt 哈希的全部复用状态**

当 active Prompt/tag hash 变化时，在对应 `interests_file` 范围内同时废弃结果并删除全部 analyzed 记录；删除“matched 旧结果继续复用、只重跑 unmatched”的实现和测试。`_get_analyzed_news_ids_impl` 只返回当前 Prompt hash 的记录，matched=1 还必须能关联到合法模块结果。

- [ ] **步骤 6：机械更新直接构造结果的 fixture**

给任务文件中所有有效结果加 `module_type: "policy"` 或 `"research"`；需要表示未匹配时使用 analyzed `matched=0`，不得插入 `exclude` result row。只迁移被字段契约影响的 fixture，不新增重复行为测试。

- [ ] **步骤 7：运行 GREEN 并提交**

运行步骤 3 同一命令，预期全部 `OK`；然后：

```bash
git diff --check
git add trendradar/storage/ai_filter_schema.sql trendradar/storage/sqlite_mixin.py trendradar/storage/base.py tests/test_ai_filter_module_storage.py tests/test_ai_filter_rule_invalidation.py tests/test_daily_delivery.py tests/test_daily_delivery_review5.py tests/test_daily_delivery_review6.py tests/test_weekly_time_rule.py
git commit -m "feat(storage): 持久化周报模块并失效旧分类"
```

## 任务 3：贯穿 pipeline 并实现政策优先双 TOP20

**文件：**
- 创建：`tests/test_weekly_three_module.py`
- 修改：`trendradar/ai/filter_pipeline.py`
- 修改：`trendradar/core/weekly.py`
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_digest.py`
- 修改：`tests/test_news_search_pipeline.py`

- [ ] **步骤 1：编写字段贯穿和统一准入 RED**

测试 parser 结果经过 `_build_filter_result()` 与 `convert_to_report_data()` 后仍含 `module_type`、`relevance_score`、`importance_score`、`content_level`、`news_item_id`、`guid` 和稳定来源字段；`0.499` 排除、`0.5` 接纳，两模块行为一致。

- [ ] **步骤 2：编写选择器 RED**

在 `tests/test_weekly_three_module.py` 构造政策 25 条、科研 25 条和 URL/GUID/标题三类重复，断言：

```python
selection = select_weekly_modules(items, min_score=0.5)
self.assertEqual(len(selection.policy), 20)
self.assertEqual(len(selection.research), 20)
self.assertEqual([row["module_rank"] for row in selection.policy], list(range(1, 21)))
self.assertEqual([row["highlight_rank"] for row in selection.policy[:5]], list(range(1, 6)))
self.assertTrue(policy_identities.isdisjoint(research_identities))
```

排序 fixture 专门让 importance、relevance、full_text/summary/title_only、发布时间和稳定字段依次打破平局；另断言政策第 21 名占用的跨模块身份也不会落入科研。

- [ ] **步骤 3：运行 pipeline/selector RED**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_weekly_three_module tests.test_weekly_digest tests.test_news_search_pipeline -v
```

预期：字段在 report conversion 丢失，旧选择器仍是 topic round-robin 单 TOP20。

- [ ] **步骤 4：实现 module-aware pipeline**

严格写后读回比较 `(news_item_id, source_type, tag_id, module_type)`；低于 MIN_SCORE 的 policy/research 可以存储，但不得进入报告 tag groups、matched 计数和 highlights。`title_entry` 显式透传本步骤测试要求的字段，ordinary 模式继续使用同一配置阈值。

- [ ] **步骤 5：替换旧选择器**

在 `core/weekly.py` 定义：

```python
@dataclass
class WeeklyNewsSelection:
    policy: list[dict]
    research: list[dict]

def select_weekly_modules(
    items: list[dict], *, min_score: float,
    limit_per_module: int = 20, highlight_count: int = 5,
) -> WeeklyNewsSelection:
    ...
```

身份顺序为 canonical URL → GUID → normalized title；证据等级为 full_text > summary > title_only；先构造所有达线 policy 身份集合，再排除 research，分别排序截断并写 `module_rank`/`highlight_rank`。删除 `deque`、topic round-robin 和旧全局 highlight 排序依赖。

- [ ] **步骤 6：让主流程只选择一次**

`NewsAnalyzer._select_weekly_rss_items()` 改为生成并保存 `self._weekly_news_modules`，再把两个模块合并后按主题组织给摘要模型；MIN_SCORE 只从 `self.ctx.config["AI_FILTER"]["MIN_SCORE"]` 传入。PDF 后续直接使用该对象，禁止第二次独立选择。

- [ ] **步骤 7：删除旧选择器 TDD、运行 GREEN 并提交**

删除 `test_weekly_digest.py` 中全局20+主题轮询测试，以新文件的排序/去重/空模块测试取代；保留自然周8库、严格缺日、来源失败、幂等和 allowed IDs 测试。运行步骤 3 同一命令后：

```bash
git diff --check
git add trendradar/ai/filter_pipeline.py trendradar/core/weekly.py trendradar/__main__.py tests/test_weekly_three_module.py tests/test_weekly_digest.py tests/test_news_search_pipeline.py
git commit -m "feat(weekly): 独立选择政策与科研双榜"
```

## 任务 4：生成政策、科研和气象可追溯叙事

**文件：**
- 修改：`config/ai_analysis_prompt.txt`
- 修改：`trendradar/ai/analyzer.py`
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_three_module.py`
- 修改：`tests/test_ai_analyzer_response.py`
- 修改：`tests/test_daily_delivery_schedule.py`

- [ ] **步骤 1：编写三段叙事和 grounding RED**

新增合法响应字段 `policy_trends`、`research_trends`、`weather_risks`，断言 weekly 输入中的每条新闻带 `module_type`，气象证据单独传入；缺任一必需周报字段、引用不存在的新闻/气象事实或 repair 后仍 malformed 时严格失败，PDF/checkpoint 不推进。

- [ ] **步骤 2：运行 RED**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_weekly_three_module tests.test_ai_analyzer_response tests.test_daily_delivery_schedule -v
```

预期：当前模型输入没有模块/气象字段，`AIAnalysisResult` 也没有三个结果字段。

- [ ] **步骤 3：扩展分析结果且保持普通模式兼容**

在 `AIAnalysisResult` 增加三个字符串字段，默认空值保证 ordinary/current 调用兼容；weekly strict 的 `has_required_narrative(report_mode="weekly")` 必须要求三字段均有可追溯内容。解析器只接受字符串或既有规范化列表格式，不把缺失字段回退成泛化 `core_trends`。

- [ ] **步骤 4：更新 Prompt 和输入**

周报 user prompt 显式序列化 `module_type`、模块内排名、逐条摘要/证据，以及独立官方气象报告；system prompt 要求分别输出政策趋势、科研进展、气象风险，会议/调研/企业稿先判断核心政策或科研事实，纯宣传不得编造成事实。

- [ ] **步骤 5：运行 GREEN 并提交**

运行步骤 2 同一命令，预期全部 `OK`；然后：

```bash
git diff --check
git add config/ai_analysis_prompt.txt trendradar/ai/analyzer.py trendradar/__main__.py tests/test_weekly_three_module.py tests/test_ai_analyzer_response.py tests/test_daily_delivery_schedule.py
git commit -m "feat(ai): 分模块生成周报政策科研气象叙事"
```

## 任务 5：新增三模块 PDF 并原子替换正式文件

**文件：**
- 修改：`trendradar/report/weekly_pdf.py`
- 修改：`trendradar/report/pdf.py`
- 修改：`trendradar/__main__.py`
- 修改：`tests/test_weekly_pdf_report.py`
- 修改：`tests/test_weekly_pdf_delivery.py`
- 修改：`tests/test_wework_pdf.py`

- [ ] **步骤 1：编写三模块模板 RED**

将 renderer 测试改为显式 `policy_items` 和 `research_items`：每类最多20、最多总40、身份全局唯一；每条只渲染一次，前5只显示内联“重点政策/重点文献”；空态精确为“本周暂无符合条件的政策新闻”和“本周暂无符合条件的科研文献”；气象为第三个一级模块。

- [ ] **步骤 2：编写原子生成 RED**

预置一个正式 PDF/HTML，模拟 Chromium 失败、PDF header 错误和 >20MB，断言旧正式件字节不变且临时文件消失；成功时断言 `os.replace()` 后 HTML/PDF 同时为新内容。部分账号账本已存在时，resume 必须复用原 PDF digest，不能重新生成覆盖。

- [ ] **步骤 3：运行 PDF RED**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_weekly_pdf_report tests.test_weekly_pdf_delivery tests.test_wework_pdf -v
```

预期：旧 renderer 仍接收单列表并重复“重点新闻/入选新闻”，生成失败会先 unlink 正式件。

- [ ] **步骤 4：实现三模块模板**

renderer 新签名：

```python
def render_weekly_pdf_html(
    *, policy_items: list[dict], research_items: list[dict],
    ai_analysis: Any, agro_weather: Any,
    period_label: str, generated_at: datetime,
) -> str:
    ...
```

删除 `flatten_unique_news()`、全局 highlights 和 topic 重复列表。卡片接收模块内 `module_rank`，仅在 rank≤5 时展示对应重点 marker；封面/指标分别计数；分模块观点读取任务4字段。保留安全 URL、HTML 转义、A4 页眉页脚和长气象 block 分页 CSS。

- [ ] **步骤 5：实现临时文件验证后原子替换**

把新正式 stem 固定为 `农业育种新闻周报_三模块_<开始日>至<结束日>`，使升级补跑不会命中旧单列表 PDF 的逐账号摘要账本。新路径同目录创建唯一 `.tmp` HTML/PDF；Chromium 成功后验证 `%PDF`、大小≤20MB、pdfinfo A4/页数和 pdftotext 可提取中文，再依次 `os.replace(temp_html, final_html)`、`os.replace(temp_pdf, final_pdf)`。异常只删除本轮临时文件，不动原正式件。若新三模块 PDF 的 partial delivery ledger 已存在，主流程直接 resume，不进入生成函数。

- [ ] **步骤 6：删除重复 PDF TDD 并运行 GREEN**

删除旧全局 Top20/重复版块测试；保留并迁移 HTML 转义、安全链接、确定路径、失败残留清理、气象-only、真实 40 条多页 A4/中文/页眉页脚、20MB 底层拒绝和逐账号账本。底层与端到端完全重复的 Chromium 缺失/20MB mock 只留一份。

- [ ] **步骤 7：提交**

运行步骤 3 同一命令，预期全部 `OK`；然后：

```bash
git diff --check
git add trendradar/report/weekly_pdf.py trendradar/report/pdf.py trendradar/__main__.py tests/test_weekly_pdf_report.py tests/test_weekly_pdf_delivery.py tests/test_wework_pdf.py
git commit -m "feat(pdf): 原子生成三模块农业育种周报"
```

## 任务 6：收敛 weekly 主流程并保留严格重试语义

**文件：**
- 修改：`trendradar/__main__.py`
- 修改：`trendradar/context.py`
- 修改：`tests/test_weekly_schedule.py`
- 修改：`tests/test_weekly_pdf_delivery.py`
- 删除：`tests/test_weekly_report_output.py`

- [ ] **步骤 1：编写主链 RED**

覆盖四条真实调用链：有双模块+气象生成一个 PDF；任一新闻模块为空仍成功；两新闻模块均空但气象有效仍成功；三类内容均空严格失败。断言 weekly 不调用 `ctx.generate_html()`，不写 `output/html/latest/weekly.html` 或通用 `output/index.html`，企业微信只收到一个 file 消息。

- [ ] **步骤 2：锁定失败与续投契约**

保留并适配以下测试：严格分类/叙事/存储/PDF 任一失败不写 analyze/push；部分账号成功后重试只发失败账号；全部账号已发送但 global checkpoint 失败时零外呼只补 checkpoint；同周锁覆盖完整发送事务。

- [ ] **步骤 3：运行主链 RED**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_weekly_schedule tests.test_weekly_pdf_delivery tests.test_weekly_three_module -v
```

预期：weekly 仍生成通用 dashboard 或 renderer 参数/模块状态不一致。

- [ ] **步骤 4：让同一选择对象贯穿主链**

`_run_analysis_pipeline()` 成功后只保存一个 `WeeklyNewsSelection`；叙事与 PDF 读取同一对象。`_generate_weekly_pdf_report()` 对两新闻模块均空的判断只结合气象，不以旧单列表 truthiness 误判。weekly 分支跳过通用 `ctx.generate_html()`，ordinary/daily 路径保持原样。

- [ ] **步骤 5：精简重复 schedule/delivery TDD**

删除 `tests/test_weekly_report_output.py`。逐账号 partial/global retry 状态机集中保留在 `test_weekly_pdf_delivery.py`；`test_weekly_schedule.py` 只保留 run 返回值、周锁、气象先验、严格失败不推进 checkpoint 的端到端断言，删除相同 mock 状态机的重复副本。

- [ ] **步骤 6：运行 GREEN 并提交**

运行步骤 3 同一命令，预期全部 `OK`；然后：

```bash
git diff --check
git add trendradar/__main__.py trendradar/context.py tests/test_weekly_schedule.py tests/test_weekly_pdf_delivery.py tests/test_weekly_report_output.py
git commit -m "refactor(weekly): 收敛三模块PDF唯一交付主线"
```

## 任务 7：清理旧文档、旧术语和冗余测试契约

**文件：**
- 修改：`README.md`
- 修改：`README-EN.md`
- 修改：`docs/news-push-technical-implementation.md`
- 修改：`tests/test_weekly_configuration.py`
- 修改：`tests/test_portable_deployment.sh`
- 保留：`docs/superpowers/specs/2026-08-11-weekly-three-module-design.md`
- 保留：`docs/superpowers/plans/2026-08-11-weekly-three-module.md`

- [ ] **步骤 1：编写静态清理 RED**

在 `test_weekly_configuration.py` 扫描生产配置/Prompt/文档，断言存在“三模块、政策最多20、科研最多20、统一0.5、只发PDF”，并禁止旧生产短语：`AI 严格筛选最多 20 条`、`strict AI up to 20 items`、`重点新闻`、`入选新闻`、`min_score: 0.7`、weekly 文字预览/回退。扫描不能误伤标签变化比例 `0.7~1.0`、普通测试样本分数或 CSS。

- [ ] **步骤 2：运行静态 RED**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_weekly_configuration -v
bash tests/test_portable_deployment.sh
```

预期：README/技术文档仍描述旧单列表或旧阈值。

- [ ] **步骤 3：只保留当前用户可见主线**

中英文 README 和技术文档统一描述：周二至周日静默采集；周一汇总上一自然周；政策/科研各 TOP20；气象独立；全局 `0.5`；企业微信只发送一个 PDF。删除旧日报/weekly 文字预览和单 TOP20 示例，不删除 ordinary/current 兼容入口或 MCP/安装说明。

- [ ] **步骤 4：核对 docs/superpowers 与测试文件清单**

`docs/superpowers` 最终只含本规格与本计划。运行 `rg --files tests`，确认三个旧冗余测试文件已删除，新契约集中在三个新测试文件；严禁删除 strict storage/CAS、周锁、真实 Chromium、逐账号 delivery、自然周8库和农业气象测试。

- [ ] **步骤 5：运行 GREEN 并提交**

运行步骤 2 同一命令，预期 unittest `OK`、portable `PASS`；然后：

```bash
git diff --check
git add README.md README-EN.md docs/news-push-technical-implementation.md docs/superpowers/specs/2026-08-11-weekly-three-module-design.md docs/superpowers/plans/2026-08-11-weekly-three-module.md tests/test_weekly_configuration.py tests/test_portable_deployment.sh
git commit -m "docs(weekly): 清理旧周报规则与重复测试契约"
```

## 任务 8：最终验证、生成投递并精确清理旧结果

**文件：**
- 修改：`.superpowers/sdd/final-fix-report.md`（忽略文件，仅作执行证据）
- 运行时目标：`output/` 中当前自然周正式 HTML/PDF 与对应 news DB 的旧 AI 缓存/本周 checkpoint；不提交运行时产物。

- [ ] **步骤 1：执行一次聚焦集成回归**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_ai_filter_module_contract tests.test_ai_filter_module_storage tests.test_weekly_three_module tests.test_news_search tests.test_news_search_pipeline tests.test_weekly_digest tests.test_weekly_schedule tests.test_weekly_pdf_report tests.test_weekly_pdf_delivery tests.test_wework_pdf tests.test_agro_weather tests.test_weekly_time_rule -v
```

预期：所有目标测试 `OK`，无网络访问。

- [ ] **步骤 2：执行一次普通模式兼容回归**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest tests.test_elsevier_full_text tests.test_direct_first_proxy tests.test_email_delivery -v
```

预期：全部 `OK`。

- [ ] **步骤 3：只执行一次最终全量 discovery**

运行：

```bash
docker run --rm --network none -v /mnt/d/project/trendradar/.worktrees/weekly-three-module:/workspace -w /workspace trendradar-task8-verify:7b97a5d0 /app/.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

预期：`OK`、exit 0。若仅出现已知只读挂载写文件错误，先修测试隔离后只补验该用例；不机械重复整套。若出现生产断言失败，修复后必须再跑一次完整全量。

- [ ] **步骤 4：真实 headless PDF 与静态门禁**

运行真实 Chromium/Poppler 多页测试，随后执行：

```bash
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
git diff --check
rg -n "min_score:\s*0\.7|重点新闻|入选新闻|AI 严格筛选最多 20 条|strict AI up to 20 items" config trendradar docs README.md README-EN.md
```

预期：测试 `OK`；三个 bash/diff 命令 exit 0；`rg` 对生产旧语义无匹配。

- [ ] **步骤 5：最终宽范围代码审查**

以分支起点 `4866e19c` 和当前 HEAD 生成 review package，审查三模块分类完整性、旧缓存失效、政策优先去重、Remote CAS、PDF 原子替换、partial retry 和 ordinary 模式兼容。Critical/Important 必须修复并定向复验；只在审查 clean 后进入运行时操作。

- [ ] **步骤 6：只读盘点生产状态并停止服务**

记录主仓库 HEAD/status、compose 状态、当前自然周窗口、`output` 文件清单与摘要、news DB 的 AI/period 行数、逐账号 action 数；输出秘密变量时只能显示 `SET/UNSET`。停止 TrendRadar 与 MCP，确认没有 Python 周报进程和 SQLite sidecar 写入。禁止 `git clean`、`rm -rf`、`docker compose down -v`。

- [ ] **步骤 7：使当前周期旧 AI 结果和全局周 checkpoint 精确失效**

先把待改 news DB 和旧正式 HTML/PDF复制到 `/tmp/trendradar-weekly-three-module-20260811/`，核对文件数和 SHA-256。对 `previous_natural_week(2026-08-11 Asia/Shanghai)` 的 `window.end=2026-08-10` 所属 news DB 执行单一 SQLite 事务：删除该周期旧 `ai_filter_results`、`ai_filter_analyzed_news`，以及 `monday_weekly` 的 analyze/push 全局 checkpoint；保留 tags、RSS 日库、来源状态、first-seen/outbox、provenance 和旧逐账号 delivery action。提交后运行 `PRAGMA quick_check`。

- [ ] **步骤 8：部署新提交并执行一次受控补跑**

构建并启动新容器，先确认 cron/HTTP/配置和密钥 `SET/UNSET`；使用 `--force-weekly` 对上一自然周执行一次前台受控补跑。持续到任务明确退出，验证：候选全部在自然周窗；每个输入均有新 Prompt hash analyzed；active result 都有合法 module；policy/research 分别≤20、都≥0.5、身份互斥；气象周期正确；PDF `%PDF`、≤20MB、A4/中文/页眉页脚；企业微信仅 file 消息；账号账本和 global push checkpoint 均存在。

- [ ] **步骤 9：成功投递后清理旧布局产物**

精确删除步骤 6 已列出的旧版 weekly PDF/HTML、旧通用 weekly dashboard、临时 `.tmp`、PNG/文字坐标/回滚副本；保留刚验证过的三模块正式 HTML/PDF。再次列出正式目录并核对只剩当前自然周一套正式结果。确认成功后删除 `/tmp/trendradar-weekly-three-module-20260811/` 临时备份；若补跑失败，停止服务、恢复该临时备份和原 checkpoint，不做清理。

- [ ] **步骤 10：提交报告并收尾分支**

将测试数字、真实 PDF 页数/摘要、投递时间、账号账本、清理目标与保留状态写入 `.superpowers/sdd/final-fix-report.md`。确认功能分支工作树 clean、主工作树只有用户原有改动与运行时结果；不提交 `.env`、output、PDF、数据库或密钥。使用 `finishing-a-development-branch` 给出 fast-forward 合并/保留分支选项，并且只有用户授权时才合并或推送 Git 远端。

## 计划自检

- 规格覆盖：三模块、广义政策、不硬排除宣传载体、政策优先、统一0.5、双TOP20、空模块、气象独立、Prompt/兴趣、存储迁移、Remote CAS、PDF-only、原子产物、重试账本、旧缓存/文档/TDD清理均有对应任务。
- 类型一致：分类使用 `module_type`；可持久化值只有 `policy/research`；周选择统一返回 `WeeklyNewsSelection(policy, research)`；renderer 只接收 `policy_items/research_items`。
- 测试卫生：删除三份与新产品冲突的测试文件及具体重复 mock；保留自然周、严格存储、CAS、周锁、气象、真实 Chromium 和逐账号投递关键证据。
- 运行次数：任务级只跑受影响模块；最终只跑一次聚焦、一次兼容、一次 full 和一次真实 PDF，不重复已经 GREEN 的大套件。
- 安全：先验证/投递新结果，再删除旧布局；清理目标精确，权威 RSS/账本/provenance 永不删除；秘密不输出。
