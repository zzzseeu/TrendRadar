# 每日成功检查点最终修复报告

日期：2026-08-09
工作树：`daily-success-checkpoint`
范围：最终审查清单 1–6；未合并 `main`、未部署、未真实推送。

## 执行方法

- 先逐项沿生产数据流确认根因和正常参考路径。
- 每项先增加真实链路测试并确认有效 RED，再做最小实现转 GREEN。
- 所有 Python 测试均通过断网 Docker 容器内的 `/app/.venv/bin/python` 执行。
- Docker 断网时 LiteLLM 无法下载远程价格表、转用本地备份的警告符合预期，不影响测试结果。

下文命令中的公共 Docker 前缀为：

```bash
docker run --rm --network none \
  --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/daily-success-checkpoint:/workspace \
  -w /tmp docker-trendradar
```

## 1. authoritative RSS-only

### 根因

`rss_ids_authoritative=True` 只约束了 RSS 范围判断，但 AI 筛选流水线仍无条件读取热榜候选、分类热榜，并保留历史 active 热榜结果；报告转换也继续输出非 RSS 结果。主流程随后又用原始热榜条数覆盖统计，因此每日交付可能被快照外热榜内容触发。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryAIScopeTests.test_authoritative_pipeline_never_classifies_or_returns_hotlist \
  tests.test_daily_delivery_schedule.DailyDeliveryScheduleTests.test_authoritative_no_rss_match_cannot_report_hotlist_payload -v
```

有效失败：真实 pipeline 的 AI 分类输入同时出现热榜和允许 RSS；预期只含 RSS，命令 exit 1。

### 修复

- authoritative 模式不读取、不构造、不分类热榜候选。
- active 结果层明确丢弃全部非 RSS 结果。
- 报告转换层再次拒绝非 RSS 结果。
- authoritative 主流程的热榜总数固定为 0，避免原始热榜统计回流。
- authoritative AI 路径改用严格 RSS ID 读取。

### GREEN 与兼容影响

上述 2 项定向测试均通过。普通模式和 weekly 未设置 `rss_ids_authoritative` 时继续读取、分类和展示热榜，原行为不变。

## 2. strict AI stages

### 根因

每日交付只检查最外层 `AIAnalysisResult.success`：JSON 解析失败后的文本兜底仍可能标为成功；grounding review 返回 `None` 被忽略；逐条分类摘要校审没有可消费的状态；翻译缺失编号被记作成功，且部分批次失败仍继续交付。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryStrictAIStageTests \
  tests.test_daily_delivery.DailyDeliveryStrictTranslationTests \
  tests.test_daily_delivery_schedule.DailyDeliveryScheduleTests.test_daily_delivery_passes_strict_contract_to_ai_analyzer \
  tests.test_daily_delivery_schedule.DailyDeliveryScheduleTests.test_strict_translation_failure_aborts_without_checkpoint -v
```

有效 RED 共 7 项：接口尚无 `strict`/`require_all` 状态，出现 `TypeError`/缺失参数断言；JSON 修复失败、grounding `None`、逐条校审失败和翻译部分/全部失败都未形成显式 fail-closed，命令 exit 1。

### 修复

- `AIAnalyzer.analyze(strict=False)`：严格模式拒绝解析/修复后的降级结果；启用 grounding 时，校审失败或返回 `None` 明确失败。
- `AIFilter.classify_batch(strict=False)`：逐条摘要校审返回完整状态；异常、空缺或只校审部分结果时，严格批次返回失败。
- `AIFilterPipeline(strict=False)` 将严格状态传播到每个分类批次。
- `AITranslator` 将非空原文对应的缺失翻译标为失败，不再伪造成功。
- `NotificationDispatcher.translate_content(require_all=False)` 在严格模式下对翻译部分失败和全部失败抛出显式异常。
- 每日交付将 strict 状态传播到筛选、摘要、上游标题翻译和 HTML 热榜翻译；失败在 once_analyze、once_push 和 push 检查点之前终止。

### GREEN 与兼容影响

上述 7 项定向测试全部通过。strict 参数默认 `False`，普通模式仍保留解析、校审和翻译的 fail-soft 行为；既有 weekly 严格交付继续沿用项目已有 `_is_strict_delivery_mode` 契约。

## 3. ntfy/Bark 多批严格成功

### 根因

两个 sender 都在至少一批成功后返回 `True`，dispatcher 即使要求所有目标成功也无法识别同一端点内的部分批次失败。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryStrictSenderBatchTests -v
```

有效失败：真实 dispatcher/sender 路径中 ntfy 与 Bark 均为一批成功、一批 HTTP 500，但严格调用仍返回 `True`；2 项失败，命令 exit 1。

### 修复

- ntfy/Bark sender 增加默认关闭的 `require_all_batches`。
- dispatcher 将既有 `require_all_targets` 传播到两个 sender 的批次级契约。
- 严格时必须 `success_count == total_batches`；非严格时仍保持至少一批成功即成功。

### GREEN 与兼容影响

2/2 定向测试通过，并在同一测试中确认普通模式对相同一成一败响应仍返回 `True`。

## 4. 秒级首次发现与历史分钟桶

### 根因

RSS fetcher 新写入的 `crawl_time`/`first_time` 只有 `HH:MM`，无法表达秒级检查点。若直接把历史分钟记录解释成一个精确时刻，检查点位于同一分钟中间时，可能永久漏掉该分钟后半段的旧记录。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryWindowTests.test_new_rss_fetch_writes_full_local_datetime_with_seconds \
  tests.test_daily_delivery.DailyDeliveryAggregatorTests.test_legacy_minute_bucket_overlapping_checkpoint_is_conservatively_kept -v
```

有效失败：新写入值实际为 `10:00` 而非完整本地日期时间；历史 `10:00` 分钟桶在 checkpoint `10:00:30` 后被错误排除，命令 exit 1。

### 修复

- fetcher 的 item 首次时间与批次 crawl time 均写为本地 `YYYY-MM-DD HH:MM:SS`。
- 完整时间继续严格执行 `(start, end]`。
- 历史 `HH:MM`/`HH-MM` 作为 `[minute, minute+1m)` 精度桶；桶与 `(start,end]` 相交即保守纳入，再由既有身份去重消除重叠。
- 原左开边界测试改用完整秒级 fixture，避免把历史分钟值误当精确时刻。

### GREEN 与兼容影响

2/2 定向测试通过；显示层仍可从完整时间中抽取 `HH:MM`，旧分钟格式解析保持兼容。

## 5. strict RSS ID reads

### 根因

`get_all_rss_ids` 捕获任意异常后返回 `[]`，合法空快照和坏库不可区分；daily aggregator 的 canonical 预读、保存后 ID resolve 与 authoritative AI 读取因此都可能把读取故障当空数据。远程弱读取还会在对象检查阶段吞掉部分错误。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryStrictRSSIDReadTests -v
```

有效 RED 共 6 项：严格接口/转发尚不存在，authoritative 路径未传播读取异常；本地坏空库、远程 AccessDenied、下载异常/坏库与真实 404 无法形成一致可辨契约，命令 exit 1。

### 修复

- `StorageBackend`、Local、Remote、Manager 增加一致的 `get_all_rss_ids_strict` 接口。
- SQLite mixin 的严格实现不吞查询/坏库异常。
- daily aggregator canonical 预读和 ID resolve 使用严格接口；authoritative AI 也使用严格接口。
- 远程严格连接对对象存在性做严格检查：真实 404 建立合法空本地库；AccessDenied、网络/下载错误和坏库均上抛。
- 弱接口保留原来的 `[]` fail-soft 兼容行为。

### GREEN 与兼容影响

6/6 严格读取定向测试通过；并复跑 item 1 的 2 项跨层测试通过。普通调用方继续使用弱接口，已有行为不变。

## 6. HOTLIST disabled 保留 period_label

### 根因

dispatcher 在 `HOTLIST=false` 时用一个只含四个热榜字段的新字典替换整个 `report_data`，连带丢失 `period_label`、计数等报告元数据。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery_report.DailyDeliveryReportTests.test_hotlist_disabled_keeps_period_in_real_feishu_and_dingtalk_payloads -v
```

有效失败：真实 dispatcher 分别经过飞书、钉钉 splitter/sender 并成功捕获两份 payload，但其中缺少精确周期；命令 exit 1。

### 修复

复制原 `report_data`，只清空 `stats`、`failed_ids`、`new_titles`、`id_to_name` 四个热榜字段，保留 `period_label` 和其余元数据。

### GREEN 与兼容影响

1/1 定向测试通过；飞书和钉钉真实 payload 均保留“每日新增”和精确周期，且不含被禁用的热榜内容。

## 最终验证

### 聚焦回归

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery \
  tests.test_daily_delivery_schedule \
  tests.test_daily_delivery_report \
  tests.test_news_search_pipeline -v
```

结果：`Ran 144 tests in 183.061s`，`OK`（任务 7 原聚焦 124 + 本轮新增 20）。

### 兼容回归

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_weekly_digest \
  tests.test_weekly_schedule \
  tests.test_weekly_report_output \
  tests.test_elsevier_full_text \
  tests.test_direct_first_proxy \
  tests.test_email_delivery -v
```

结果：`Ran 93 tests in 162.089s`，`OK`。

### 全量回归

```bash
<公共 Docker 前缀> -m unittest discover -s /workspace/tests -v
```

结果：`Ran 341 tests in 358.166s`，`OK`。

### 静态与便携性

```bash
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
git diff --check
```

结果：四项均 exit 0；portable 输出 `PASS: 本地部署路径可移植性检查通过`。

## Diff 自审

- authoritative RSS-only 在候选收集、active 结果、报告转换和主统计四层均有防线，不依赖单一调用方约定。
- strict 状态为显式参数，不通过日志字符串推断；默认值保持普通模式兼容。
- 翻译缺失编号现在具有结构化失败状态；普通 dispatcher 仍可用原文降级。
- ntfy/Bark 的普通至少一批成功语义保持不变，严格模式才要求全部批次。
- 新写入时间不改变显示格式；历史分钟记录仅在窗口相交时保守重叠。
- 远程 404、权限、下载、坏库测试均经过真实 Remote backend 连接路径。
- HOTLIST 过滤复制输入字典，不原地污染调用方数据。
- 未修改调度时间、窗口首次 24 小时、RSS/GDELT/Google freshness、密钥持久化和部署配置。
- `git diff --check` 无空白错误；未发现清单中不成立的建议，也未发现需要扩大范围的架构冲突。

最终测试计数：聚焦 144、兼容 93、全量 341；三套命令合计执行 578 个测试（其中聚焦与兼容测试也包含于全量发现）。

---

# 第二次最终复审修复

日期：2026-08-09
范围：第二次最终复审 A–D；继续未合并 `main`、未部署、未真实推送。

## A. 跨检查点 canonical 全局首次发现

### 根因

aggregator 先按各日库、各 feed 行的 `first_time` 做窗口过滤，再对本轮结果做 canonical identity 去重。因此同一 canonical URL 在 feed A 于检查点前首次出现、在 feed B 于检查点后再次出现时，后者会被错误视为本轮新增。窗口内读取也无法发现任意更早日库中的首次记录。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryAggregatorTests.test_canonical_first_seen_before_checkpoint_on_start_day_is_excluded \
  tests.test_daily_delivery.DailyDeliveryAggregatorTests.test_canonical_history_before_window_dates_is_excluded \
  tests.test_daily_delivery.DailyDeliveryAggregatorTests.test_canonical_first_seen_only_inside_window_is_included \
  tests.test_daily_delivery.DailyDeliveryRemoteStrictReadTests.test_remote_history_listing_failure_is_fail_closed -v
```

结果：运行 4 项，3 项按预期失败（两个重复推送、一个远程异常未传播），合法的窗口内首次出现用例通过；命令 exit 1。

### 修复与 GREEN

- StorageBackend、Local、Remote、Manager 增加批量 `get_earliest_rss_discoveries_strict` 契约。
- 只查询本轮候选 identity，但枚举并严格读取截至 `window.end` 的全部现存 RSS 日库；同一 `window.start` 日期中早于 start 的行也参与最早时间判断。
- identity 规则与快照完全一致：优先 canonical URL；无 URL 时为 feed + 标准化标题。
- aggregator 在候选收集后按系统全局最早发现时间执行 `(start,end]`，且没有写 delivered 标记。
- Remote 分页枚举历史日库；真实 404 是合法缺日，列表、下载、权限或坏库错误均上抛。

同一 4 项测试复跑 `4/4 OK`，exit 0（19.585s）。正常窗口内首次内容仍纳入；普通/weekly 快照逻辑未改。

## B. strict AI 完整性与缓存

### 根因

- strict authoritative daily 仍读取普通模式的 `analyzed_rss` 缓存，可能跳过严格逐条分类与摘要校审。
- `_review_item_summaries` 逐项覆盖结果，却没有验证 review ID 是待校审 ID 的唯一精确集合，重复/未知 ID 可掩盖遗漏。
- `AIAnalyzer` 只拒绝 JSON 解析失败；合法 `{}` 会以所有叙事字段为空的成功结果继续推进。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryAIScopeTests.test_strict_authoritative_run_reclassifies_ordinary_cached_rss \
  tests.test_daily_delivery.DailyDeliveryStrictAIStageTests.test_item_summary_review_rejects_duplicate_ids \
  tests.test_daily_delivery.DailyDeliveryStrictAIStageTests.test_item_summary_review_rejects_unknown_ids \
  tests.test_daily_delivery.DailyDeliveryStrictAIStageTests.test_item_summary_review_rejects_missing_id \
  tests.test_daily_delivery.DailyDeliveryStrictAIStageTests.test_item_summary_review_rejects_empty_summary \
  tests.test_daily_delivery.DailyDeliveryStrictAIStageTests.test_strict_analysis_rejects_empty_json_summary \
  tests.test_daily_delivery_schedule.DailyDeliveryScheduleTests.test_empty_json_ai_summary_does_not_advance_checkpoint -v
```

结果：运行 7 项，4 项失败（普通缓存被复用、`{}` 被接受、重复 ID 与未知 ID 被接受），其余缺失/空摘要与主流程检查已有部分防线；命令 exit 1。

### 修复与 GREEN

- strict + authoritative 对全部 allowed RSS 重新分类校审，不消费普通 analyzed 缓存；仍先严格读取 analyzed 表以排除存储故障。
- review 结果逐项验证类型、已知 ID、唯一 ID、非空摘要，最终 ID 集合必须精确等于待校审集合；完整验证后才更新摘要。
- strict analyzer 要求核心趋势、情感、信号、RSS 洞察、展望或 standalone 摘要中至少一个具有非空叙事内容；空 `{}` 明确失败。
- 重试通过后由 C 的事务 replace 清理并替换本轮旧结果，只消费本轮成功 ID。

同一 7 项测试复跑 `7/7 OK`，exit 0（9.024s）。普通模式继续复用缓存并保留 fail-soft 行为。

## C. strict AI 存储 fail-closed

### 根因

普通 `_save_filter_results_impl` 和 `_save_analyzed_news_impl` 会逐项吞写入错误并只返回计数；pipeline 不校验计数。active 结果和 analyzed ID 读取也会把异常转换成空列表/集合，导致坏库与合法无匹配不可区分。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryStrictAIStorageTests -v
```

结果：5/5 按预期失败，exit 1（16.949s）：写入计数不一致、strict analyzed 读取异常、strict active 读取异常均未被调用/传播；真实 SQLite 的 analyzed 写 trigger 异常被逐项吞掉，损坏的 `ai_filter_results` 表也被当作合法空结果。

### 修复与 GREEN

- Base 对新 strict API 默认抛 `NotImplementedError`；Local、Remote、Manager 显式实现一致契约。
- `replace_ai_filter_batch_strict` 在一个 SQLite `BEGIN IMMEDIATE` 事务中，仅清理当前 interests file、本轮成功 ID 的旧结果，写入全部分类结果及 matched/unmatched 状态。
- 同一事务内读回并精确校验 `(news_item_id, source_type, tag_id)`、matched、prompt hash；任一写入/数量/读回差异回滚并上抛。
- strict analyzed/active 读取不再吞异常；pipeline 捕获后返回 `AIFilterResult.success=False`，并校验最终 active 结果唯一集合精确等于本轮 matched keys。
- 部分分类批次失败时不保存部分 strict 结果；Remote `end_batch_strict` 要求所有脏数据库上传且远程验证成功。

同一 5 项测试复跑 `5/5 OK`，exit 0（16.015s），其中两项使用真实 SQLite 损坏路径。既有 daily 主流程失败测试继续证明 `success=False` 不写 once/checkpoint；普通/weekly 仍走原 fail-soft API。

## D. 混合 crawl_time 升级兼容

### 根因

新完整日期秒值与历史 `HH:MM` 在 `ORDER BY crawl_time` 和 Python 字符串 `<` 中不可比较。例如历史 `20:00` 会在字典序上被误判为晚于 `2026-08-09 21:00:05`，使 current 选错批次、incremental 把旧条目重新当新增。

### RED

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery.DailyDeliveryMixedRSSCrawlTimeTests -v
```

结果：2/2 失败，exit 1（4.971s）；current 返回历史 `20:00` 批次，incremental 同时返回旧条目和真正新增条目。

### 修复与 GREEN

- current 与 RSSData 的最新 crawl time 改为按 `rss_crawl_records.id DESC` 获取，抓取状态也直接绑定最新 record ID。
- incremental 将完整时间与历史 `HH:MM`/`HH-MM` 统一解析为日库日期、配置时区的 datetime 后比较；只有不可解析的异常旧值保留原 fail-soft fallback。

同一 2 项真实 SQLite 测试复跑 `2/2 OK`，exit 0（5.110s）。

## 第二次复审最终验证

### 相关模块

```bash
<公共 Docker 前缀> -m unittest tests.test_daily_delivery -v
```

结果：`Ran 70 tests in 183.679s`，`OK`，exit 0。

### 聚焦回归

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_daily_delivery \
  tests.test_daily_delivery_schedule \
  tests.test_daily_delivery_report \
  tests.test_news_search_pipeline -q
```

结果：`Ran 162 tests in 241.399s`，`OK`，exit 0。

### 兼容回归

```bash
<公共 Docker 前缀> -m unittest \
  tests.test_weekly_digest \
  tests.test_weekly_schedule \
  tests.test_weekly_report_output \
  tests.test_elsevier_full_text \
  tests.test_direct_first_proxy \
  tests.test_email_delivery -q
```

结果：`Ran 93 tests in 155.011s`，`OK`，exit 0。

### 全量回归

```bash
<公共 Docker 前缀> -m unittest discover -s /workspace/tests -q
```

结果：`Ran 359 tests in 408.664s`，`OK`，exit 0。

### 静态、便携性与 diff

```bash
bash -n docker/entrypoint.sh
bash -n config/daily.crontab
bash tests/test_portable_deployment.sh
git diff --check
```

结果：四项均 exit 0；portable 输出 `PASS: 本地部署路径可移植性检查通过`。

## 第二次复审 Diff 自审

- A 的历史查询只扫描候选 identity，但覆盖截至窗口结束的全部现存 RSS 日库；同日起点前记录不会漏判，且没有提前持久化 delivered 状态。
- strict cache 语义为每次 authoritative daily 全量重校审 allowed RSS；事务 replace 使失败后的重试可覆盖/清理旧 matched 与 unmatched 状态。
- strict review、分析叙事、存储写入、存储读回和远程持久化均以结构化状态或异常判定，不依赖日志文本。
- strict active 结果只消费本轮成功 ID，并验证其唯一 key 集合；普通与 weekly 继续使用原 active 缓存和 fail-soft API。
- 混合时间格式不再参与字符串排序/比较；显示字段与历史数据库 schema 均无需迁移。
- 未改变 10:00 调度、`(last_success, now]`、首次 24h、合法空推进、同日成功跳过交付、freshness、通知目标或密钥持久化契约。
- 未发现第二次清单中不成立的建议，也未出现需要扩大范围的架构冲突。

第二次最终测试计数：聚焦 162、兼容 93、全量 359；三套最终命令合计执行 614 个测试（聚焦与兼容也包含于全量发现），全部为明确 exit 0。
