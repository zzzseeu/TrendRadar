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

## 第三次最终复审修复（versioned ledger / strict AI protocol）

### 1. 版本化 RSS identity first-seen 账本与远端一致性

根因：第二次实现虽然按候选 identity 查询历史，但每次仍需枚举/打开历史日库；Remote 的严格读取也没有把本地 SQLite 连接绑定到 ETag/VersionId/LastModified，缓存 404、对象更新和并发覆盖无法形成可靠契约。

RED 使用真实 Local SQLite、版本化 S3 fake 和 RemoteStorageBackend 路径，共 7 项：首次回填性能、保存失败重试、不可变最早时间、checkpoint 版本刷新、404 后出现、ledger 版本刷新和并发覆盖。首次结果 `Ran 7`，5 FAIL、1 ERROR、1 通过，exit 1。

修复：

- 新增固定 `rss/first-seen-v1.db` 与 `first_seen_schema.sql`，以公共 canonical URL / feed+标准化标题 identity 为主键，`first_seen_at` 建索引，只允许更早时间覆盖。
- 首次升级严格枚举截至窗口结束的所有现存 RSS 日库并写入 `schema_version=1`、`backfill_complete=1`；完成后只对本轮候选 identity 查询账本，不再打开历史日库。
- 每次 RSS 保存成功后同步账本；日库或账本本地提交/远程上传任一失败均使 save 失败，重试可补齐，不写 delivered 标记。
- Remote strict 读取每次 head 对比 provenance；版本变化时关闭连接、临时下载、下载后二次 head 并原子替换。真实 404 可空，AccessDenied、网络、坏库和下载期版本变化上抛。
- Remote 上传前检查基线版本，上传后要求 provenance 变化；并发变化拒绝覆盖。

GREEN：原 7 项 `Ran 7 in 36.916s, OK`。提交前 diff 自审又发现 raw RSS 日库已有远端版本时，本地修改可能被 ledger backfill 的 strict refresh 覆盖，且 raw 日库上传未启用版本冲突检查；新增 2 项真实 Remote RED，结果 `Ran 2`、2 failures、exit 1。保存前绑定 day DB provenance、显式标记本轮本地修改为 authoritative、上传前/后校验版本后，`Ran 2 in 7.214s, OK`；Remote/review3 相关组最终 `Ran 24 in 79.782s, OK`。

### 2. strict 分类响应协议

根因：strict 标志此前只控制批次失败传播，解析层仍沿用普通模式的静默跳过/默认值逻辑；未知 news/tag ID、畸形项、缺字段和重复 ID 可能被转成部分合法结果。

RED：2 个测试以 7 个失败子用例覆盖未知 news ID、未知 tag ID、缺字段、非法元素、重复 ID、空摘要及修复后仍非法；结果 7 个子用例失败。修复后 strict 解析要求每个非空项具备完整 flat schema、ID 属本批、tag 属 active、ID 唯一、分数范围有效、摘要非空；任何违规只允许一次低温 repair，第二次失败整批返回 None；合法无匹配只接受精确 `[]`。与普通 fail-soft 兼容用例合跑 `Ran 8, OK`。

### 3. grounding 后最终叙事校验

根因：strict 非空检查只发生在首轮 JSON 解析后；grounding 可返回合法 `{}`，include_rss/include_standalone 裁剪也可能移除唯一叙事字段，最终仍被视为 success。

RED：真实 AIAnalyzer + NewsAnalyzer 链路构造“首轮非空、grounding 合法空对象”，确认会继续推进；1 项失败。新增 `has_required_narrative()` 并在 grounding 替换和配置裁剪后再次校验最终可交付对象，strict 全空 `success=False`。与 strict AI 相关组合跑 `Ran 9, OK`，调度链路确认不 dispatch、不写 checkpoint。

### 4. strict 标签生命周期

根因：strict pipeline 仍通过多个 fail-soft 标签 API 分步读取 hash、更新 active tags；中途写失败、读失败、保存 0、旧 active 残留及 Remote 上传未验证都可能形成混合版本标签快照。

RED：5 项真实 SQLite/存储 fake 覆盖中途失败、读失败、保存 0、混合旧标签与快照不一致；初次 4 ERROR + 1 FAIL。新增 Base/Local/Remote/Manager `get_ai_filter_tag_snapshot_strict`、`replace_ai_filter_tags_strict`：SQLite 使用 `BEGIN IMMEDIATE` 全量 deprecate/replace/清理 analyzed cache，并在同事务读回；pipeline 精确验证 active 集合、顺序、描述、priority、prompt_hash 与 version。Remote `end_batch_strict` 上传后要求对象版本变化。5 项 GREEN；Remote 版本前进/不前进 2 项 `OK`。

### 5. 第三方 RSS strict capability 默认失败

根因：StorageBackend 的 strict RSS 默认方法转发普通 fail-soft API，使不支持严格错误区分的第三方后端在 daily_delivery 中被误认为安全。

RED：假第三方仅实现普通 RSS 读取时，daily aggregator 未明确失败。Base 的 `get_all_rss_ids_strict`、`get_rss_data_strict`、`get_rss_feed_statuses_strict` 改为默认抛 `NotImplementedError`；daily aggregator 只调用 strict API。GREEN 证明 daily fail-closed，普通 API 仍可用。

### 6. 文档、weekly 与兼容修复

- design spec 与 implementation plan 已改为固定版本化 first-seen ledger、一次性严格 backfill、后续候选索引查询、Remote provenance/版本冲突、strict tag snapshot 和 strict 分类/grounding 协议；不再声称每日扫描历史日库。
- 93 项兼容首跑发现 1 个既有 weekly title-fallback 用例 ERROR：周快照字面 crawl_time=`weekly` 被新账本当作首次时间。保留该 RED 后，将源日库分钟时间正规化为带源日期 ISO first_time，并让 SQLite 新 RSS 行优先保存 item.first_time；定向 `Ran 1 in 19.052s, OK`，93 项重跑 `Ran 93 in 183.806s, OK`。
- 普通与 weekly 继续走原 fail-soft AI/通知语义；没有改变用户可见调度、窗口、freshness 或密钥持久化要求。

## 第三次复审最终验证

所有 Python 命令均使用断网 Docker 与 `/app/.venv/bin/python`。

- 第三次新增测试：`tests.test_daily_delivery_review3` 最终 20 项；与既有 Remote strict 4 项合跑 `Ran 24 in 79.782s, OK`。
- 相关模块修复后：`Ran 143 in 312.353s, OK`。
- 聚焦：`tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report tests.test_news_search_pipeline`，`Ran 163 in 268.128s, OK`。
- 固定兼容：`Ran 93 in 183.806s, OK`。
- 最终全量：`python -m unittest discover -s /workspace/tests`，`Ran 380 in 493.225s, OK`，exit 0。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、`bash tests/test_portable_deployment.sh`、`git diff --check` 均 exit 0；portable 输出 `PASS: 本地部署路径可移植性检查通过`。

## 第三次复审 Diff 自审

- first-seen 写入与 raw RSS 保存同成败，不与 delivered/checkpoint 耦合；失败重试可通过 earliest-only upsert 补齐。
- ledger 完成元数据阻止后续查询重新打开日库；查询 SQL 仅使用本轮 candidate identity 的分块 IN 集合。
- Remote strict 对 404、权限/网络错误、对象出现、版本更新、下载期竞争、上传前竞争和上传后版本未前进均有显式区分。
- strict 标签替换与 strict 分类结果写入均为事务式提交、结构化读回验证；普通模式保留增量标签与 fail-soft 解析。
- strict 最终摘要在 grounding 与配置裁剪后检查，合法 JSON 空对象不能推进 once/checkpoint。
- 第三方 strict capability 不再隐式降级；Local、Remote、Manager 均显式实现。
- 未合并 main、未部署、未真实发送或推送，未发现遗留架构冲突。

## 第四次最终复审修复（outbox recovery / conditional CAS）

### 1. raw RSS 与 first-seen ledger 的崩溃恢复

根因：第三次实现仍在 raw 保存后从内存 payload 更新账本。raw 日库已经提交而 ledger 上传失败时，新进程无法从 durable 状态知道哪些 identity 尚未入账；同时无 URL/GUID 的 title-only 条目没有可持久化身份，单条 SQLite 错误会被吞掉而形成部分批次。

RED 先以 4 项真实 Local SQLite 测试覆盖 raw 已提交后关闭进程并以空 payload 恢复、title-only 持久化、单条错误整批 rollback、稳定二次查询不打开历史日库；首跑 3 项失败。随后补充同 feed 多个 title-only 条目，旧的空 URL 唯一索引再次产生 1 项有效 RED。

修复：

- `rss_first_seen_outbox` 与 raw 条目、crawl record、单调 generation 在同一 `BEGIN IMMEDIATE` 事务提交；outbox 只读取已经持久化的行，不遍历输入 payload。
- title-only 使用 `feed_id + normalize_title(title)` 的稳定 SHA-256 GUID，与账本 title fallback identity 保持一致；空 URL 唯一索引迁移为 partial index。
- 任一条目失败回滚整批 raw、crawl record、generation 与 outbox；既有条目更新仍保留不可变 `first_crawl_time`。
- ledger 新增每源 provenance、watermark 与 processed write 表；save 前后和候选查询前幂等消费未处理 outbox。Local 以 DB/WAL 文件 provenance 判断变化，稳定查询不再打开历史库；旧库没有 outbox 时仅在首次或 provenance 变化时兼容回填。

原 4 项复跑 `Ran 4 in 22.540s, OK`，同 feed title-only 定向测试也转为 GREEN。新 backend、空 payload 的恢复路径证明不依赖 feed 重返旧条目。

### 2. Remote conditional CAS、dirty 状态与 strict period

根因：旧 strict 上传只是 `HEAD -> PUT -> HEAD`，竞争可发生在任意间隙；dirty 本地 SQLite 可能被 strict read 刷新覆盖；analyze/push period execution 仍走普通上传，CAS 失败也可能让本地看似成功。

RED 分两组建立：raw 已 CAS 成功而 ledger 上传失败后由全新 Remote backend 从远端 outbox 恢复，以及 precheck→PUT、PUT→postcheck、创建竞争；首组 `Ran 3` 为 exit 1。dirty strict read、strict period CAS/rollback 的 3 项首跑为 1 FAIL、2 ERROR。自审又新增“同 baseline 连续 strict read 必须继续读本地 dirty”用例，首跑 1 FAIL。

修复：

- 统一 `_conditional_put_strict`：更新对象传 `IfMatch=<baseline ETag>`，创建传 `IfNoneMatch=*`；服务端不支持条件参数、412/竞争、PUT 不返回 ETag/VersionId、最终 HEAD 与 PUT provenance 不一致均显式失败。
- strict 首次修改立即标记 remote key 为 local-authoritative；同 baseline 的 strict read 保留本地 dirty，provenance 改变或 404 则报冲突。仅 CAS 成功或显式 rollback 清除 dirty。
- raw RSS 先提交带 outbox 的日库 CAS，再消费并 CAS ledger；因此 ledger 失败后新进程可从远端 raw/outbox 恢复。
- Base/Local/Remote/Manager/Scheduler 增加 strict period API；daily_delivery 与 weekly 严格交付自动使用。Remote CAS 失败关闭、恢复修改前本地镜像，不记录本地成功。
- strict tag snapshot 与 strict classification result 也复用同一 conditional CAS，批次末统一以 strict CAS 上传。

两组原 RED 及自审用例全部 GREEN；`tests.test_daily_delivery_review4` 最终 `Ran 13 in 50.066s, OK`。跨进程外部通知 exactly-once/分布式推送租约仍明确不在本次范围，失败重试可能重复端点发送的既有说明保持不变。

### 3. strict 分类 flat schema 类型

根因：strict parser 验证了字段和集合，却仍用 `float()` 接受数值字符串，也会把 bool 当整数/数值；NaN/Infinity、null、对象和 list 未形成统一非法响应。

参数化 RED 的 7 个子用例覆盖 news/tag ID 的 bool/非整数、score 与 importance 的 bool/字符串/非有限值及 summary 非字符串。修复后 ID 必须为精确非 bool `int`，分数必须为非 bool、有限 JSON `int|float` 且在 `[0,1]`，summary 必须是非空字符串；所有非法项共用一次 repair，repair 后仍非法整批 `None`。类型与 repair 测试合跑 `Ran 3, OK`，普通 fail-soft parser 未改变。

### 4. 回归中发现并修复的兼容问题

- 既有 legacy 行 `first_crawl_time` 为空时，outbox 必须继续回退 `last_crawl_time` 和本轮 crawl time；否则 canonical 跨检查点测试会把合法历史误判为存储错误。
- 跨自然日运行测试暴露 Local/Remote 的 `date=None` 仍取宿主机 wall clock；改为统一使用已注入的配置时钟，避免采集日库与后续读取日库不一致。
- Scheduler 测试中的未知稳定 period key 不能直接索引 timeline；strict 自动判断对 `weekly`/`daily_delivery` 保留严格语义，其余回退既有 report mode。
- weekly 与第三方普通模式仍保留原 fail-soft 接口；仅 weekly/daily_delivery 的执行记录走 strict API。

## 第四次复审最终验证

所有 Python 命令均使用 `docker run --network none ... /app/.venv/bin/python`，并取得明确 exit code。

- 新增第四次复审：`tests.test_daily_delivery_review4`，`Ran 13 in 50.066s`，`OK`，exit 0。
- Remote/第三次复审：`tests.test_daily_delivery_review3`，`Ran 20 in 75.393s`，`OK`，exit 0。
- 调度相关：DailyDeliverySchedule `Ran 36`、StorageContract `Ran 10 in 62.479s`、WeeklySchedule `Ran 20`，均 `OK`、exit 0。
- 聚焦回归：`tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report tests.test_news_search_pipeline`，`Ran 163 in 351.137s`，`OK`，exit 0。
- 固定兼容：weekly digest/schedule/report、Elsevier、proxy、email 共 `Ran 93 in 216.378s`，`OK`，exit 0。
- 最终全量：`python -m unittest discover -s /workspace/tests -q`，`Ran 393 in 627.768s`，`OK`，exit 0。
- 当前镜像 botocore `PutObject` service model 实测同时包含 `IfMatch` 与 `IfNoneMatch`，断网 Docker 检查输出 `True True ['IfMatch', 'IfNoneMatch']`，exit 0；strict CAS 不依赖假设中的客户端参数。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、`bash tests/test_portable_deployment.sh`、`git diff --check` 均 exit 0；portable 输出 `PASS: 本地部署路径可移植性检查通过`。

## 第四次复审 Diff 自审

- raw/outbox/crawl record/generation 同一事务；ledger 不会产生 raw 中不存在的 identity，失败进程无需相同 payload 即可恢复。
- source provenance 与 watermark 只打开新增/变化日库，既有无 outbox 日库仍可一次性严格升级；稳定查询满足不再打开历史连接的性能契约。
- Remote strict mutation 只使用真实服务端 conditional PUT，创建、更新与 post-PUT 竞争全部失败关闭；无可靠条件写能力时没有普通 PUT 降级。
- dirty 生命周期覆盖 strict tag/result、raw、ledger 与 period execution；CAS 失败不会把本地 period 标成成功。
- strict scalar schema 拒绝 Python/JSON 容易混淆的 bool、字符串数值与非有限数，repair 预算仍严格为一次。
- 设计规格与实现计划已同步 outbox、source watermark/provenance、conditional CAS、dirty、strict period 和通知 exactly-once 边界。
- 未改变 10:00、`(last_success, now]`、首次 24h、合法空推进、同日成功跳过交付、freshness、通知目标或密钥持久化；未合并 main、未部署、未真实推送。

第四次最终测试计数：聚焦 163、兼容 93、全量 393；三套最终命令合计执行 649 个测试（聚焦与兼容也包含于全量发现），全部明确 exit 0。未发现需要继续扩大范围的架构冲突。

## 第五次最终复审修复（consistent consumption / shared news CAS / frozen run date）

### A. 共享 news DB 的全写者 CAS 与主流程保存检查

根因：`news/{date}.db` 同时保存热榜、strict 标签/结果和 period checkpoint，但普通 `RemoteStorageBackend.save_news_data()` 仍可能从旧本地快照写入并无条件 PUT；同时 `_crawl_data()` 只打印保存结果，daily_delivery 即使热榜保存失败也会继续分析和通知。

RED：真实双 Remote backend 先共同读取 v1，A 写入 checkpoint 后再让 B 保存热榜；另以 NewsAnalyzer 主链制造 `save_news_data=False`。与 strict period 用例合跑 6 项，初次 3 FAIL、3 ERROR，exit 1。

修复：Remote 对所有 news 连接先严格刷新并绑定 baseline，对 news 的所有上传统一使用 conditional CAS；热榜保存保留 mutation 前 SQLite 镜像，CAS 失败恢复本地快照并返回 false。daily_delivery 检查热榜保存返回值，失败立即终止且不写 TXT、不进入分析/通知。普通模式仍可在业务层 fail-soft，但共享远端对象不存在无条件 PUT 后门。

GREEN：A 与 C 的原 6 项全部 `OK`；stale writer 测试进一步从第三个 backend 验证 checkpoint、AI tag 和新热榜同时存在。

### B / D. listed-version 一致快照与真正增量 watermark

根因：第四次 ledger 消费在读取日库后又取得 `actual_version` 并将其记为已消费；若 v2 读取期间远端变成 v3，v3 可能被错误登记但其 outbox 尚未读取。消费端也会在每次 source 变化时全量读取 outbox，再依赖 processed write 去重，没有把 per-source watermark 用作查询下界。

RED：4 项真实 Local/Remote SQLite 测试覆盖 Remote v2 读取中变 v3、Local WAL/provenance 变化、同日库第二 generation 不重放旧 outbox/fallback、ledger 写失败不推进 watermark。首跑 `Ran 4`，3 failures、1 pass，exit 1。

修复：

- inventory 的 `listed_version` 在读取前、连接绑定后及只读事务完成后均需一致；ledger 只记录实际读取的 listed version、该快照的 generation 和 watermark，绝不记录事后观察值。
- Local 以 DB + 非空 WAL 的稳定 provenance 检测并发变化；Remote 以 inventory ETag/VersionId 绑定下载快照，变化即失败，下一轮重新发现。
- outbox SQL 直接使用 `source_generation > watermark`；`processed_writes` 只负责崩溃重试幂等，失败事务不推进 watermark。`rss_items` fallback 仅用于首次迁移、watermark=0 且没有 outbox 的旧库。
- source version 与 identity/processed/watermark 在同一 ledger 事务提交；generation 回退显式失败。

GREEN：原 4 项 `OK`。最终 diff 自审把两个并发测试进一步强化为在 SQLite 事务读完后的 final provenance check 才制造变化；定向复验 `Ran 2 in 9.007s, OK`，证明该版本不被标成已消费、下一次查询会处理新 outbox。

### C. strict period 读取

根因：第四次只收紧了 period 写入；同日 analyze/push 判断仍通过 `has_period_executed()` 吞掉 SQLite、权限、网络和坏库异常，陈旧/不可读 checkpoint 会被解释为“未执行”并先发送。

修复：Base 新增默认抛 `NotImplementedError` 的 `has_period_executed_strict`；Local、Remote、Manager 显式实现。SQLite strict 查询使用 strict news connection 并传播异常；Remote 每次按 provenance 严格刷新，404 可初始化合法空库，AccessDenied/下载/坏库失败关闭。Scheduler 根据最终 report mode 对 has/record 同时路由 strict，daily_delivery 与 weekly 的所有 once 判断共用该契约。

GREEN：真实坏表、远端更新缓存、AccessDenied 和 scheduler 路由均通过；主流程在 checkpoint 不可读时不会进入通知。

### E. 单次运行冻结 run_at / run_date

根因：NewsAnalyzer 在 scheduler、checkpoint、aggregator、RSSFetcher 结果、AI pipeline 和输出阶段重复取 wall clock，并有 strict storage 调用使用 `date=None`。跨午夜时，同一交付可能从 N 日采集却读写 N+1 日标签/结果/checkpoint，甚至撞到 N+1 相同 RSS row ID。

RED：2 项跨层测试分别覆盖 23:59→00:01 的 NewsAnalyzer 全链与 N/N+1 相同 ID 的真实 strict pipeline，初次 1 FAIL、1 ERROR；随后将真实 RSSFetcher 返回 N+1 日期接入 `_crawl_rss_data()`，新增用例再次以日期仍为 N+1 形成有效 RED。

修复：`run()` 入口按配置时区只取一次 `run_at`，冻结 `run_date` 与文件时间；scheduler resolve、has/latest/record、daily/weekly aggregator、AIAnalyzer、HTML/report 和 raw RSS 保存均消费该快照。Context/AIFilterPipeline 增加兼容的可选 `operation_date`，daily strict tag/result/analyzed/RSS ID 的每次读写都显式传 N 日。若 RSSFetcher 跨午夜返回，保存前统一归一化 snapshot date/crawl_time 及由 fetcher snapshot clock 产生的 item 时间。普通/weekly AI 调用继续传 `None`，保留原兼容接口。

GREEN：跨午夜 2 项与真实 fetcher 保存断言均通过；`ctx.get_time` 在单次 run 主链只调用一次，通知发生且 checkpoint、AI、RSS 日库全部落在 N 日。

### F. 文档与兼容收敛

- design spec 与 implementation plan 已同步：外部 provenance 只发现变化，listed SQLite snapshot 与 generation/watermark 绑定消费；共享 news 的所有远端写者使用 CAS；period 严格读写一致；run_at/run_date 单次冻结并贯穿 strict API。
- 相关回归首跑发现一个旧测试夹具先通过新 API 创建 generation/watermark、再直接 SQL 插入“legacy”行，违反真实旧库前提；改为直接创建无 outbox 的旧库后定向通过。另有 direct helper fixture 缺少 `ctx.get_time`，以及 weekly fake 对新增 `operation_date` 参数不兼容；生产代码仅在完整 `run()` 已冻结时归一化 fetcher 时间、仅 daily_delivery 传 operation date，普通/weekly语义不变。
- 没有扩大到跨进程通知 exactly-once/分布式租约；既有“端点可能因失败重试而重复”边界保持不变。

## 第五次复审最终验证

所有 Python 测试均使用 `docker run --network none`、工作树只读源码挂载和镜像内 `/app/.venv/bin/python`，并取得明确 exit code。

- 第五次新增/相关模块：`tests.test_daily_delivery_review5`（因复用调度基类共发现 48 项）最终 `Ran 48 in 64.513s, OK`；第三/第四次 Remote 相关 `Ran 33 in 125.557s, OK`。
- 聚焦：`tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report tests.test_news_search_pipeline`，`Ran 163 in 337.626s`，`OK`，exit 0。
- 固定兼容：weekly digest/schedule/report、Elsevier、proxy、email 共 `Ran 93 in 212.165s`，`OK`，exit 0。
- 最终全量：`python -m unittest discover -s /workspace/tests -q`，`Ran 441 in 706.669s`，`OK`，exit 0，无 FAIL/ERROR。
- 最终自审增强的事务后版本变化定向：`Ran 2 in 9.007s`，`OK`，exit 0。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、`bash tests/test_portable_deployment.sh`、`git diff --check` 均 exit 0；portable 输出 `PASS: 本地部署路径可移植性检查通过`。

## 第五次复审 Diff 自审

- source provenance 读取前/绑定后/事务读后保持同一 listed version；并发变化不会被登记为消费完成，下一轮 inventory 必然重试。
- watermark 是 outbox SQL 的实际下界；processed write、identity earliest、source version 与 watermark 同事务，失败不会越过未消费 generation。
- Remote shared news 的普通热榜、strict tags/results 和 period 全部复用 conditional CAS；CAS 冲突不会保留本地假成功。
- strict period 的读与写都按 report mode 路由；网络/权限/坏库不能被解释为未执行。
- 同一 run 的采集、权威快照、AI、通知与 checkpoint 使用一个 N 日 operation date；N+1 相同 row ID 不会串库。
- 未改变 10:00、`(last_success, now]`、首次 24h、合法空推进、同日成功跳过交付、freshness、所有通知目标或密钥持久化；未合并 main、未部署、未真实推送。

第五次最终测试计数：聚焦 163、兼容 93、全量 441；三套最终命令合计执行 697 个测试（聚焦与兼容也包含于全量发现），全部明确 exit 0。最终自审没有发现新的生产缺陷或未解决架构冲突。

## 第六次最终复审修复（frozen fetch/output / strict mutation recovery）

### 1. 真实 RSSFetcher 跨午夜统一时钟

根因：`NewsAnalyzer.run()` 已冻结 `run_at/run_date`，但 `RSSFetcher.fetch_all()` 和每个
`fetch_feed()` 仍分别读取 wall clock；item 新鲜度又逐条读取时间。因此慢抓取跨午夜时，
批次属于 N 日而 item 的 first/crawl/last 属于 N+1 日，边界文章甚至在保存前被过滤；
主流程原有“只修空值或等于批次 crawl_time”的防御无法覆盖真实 feed 时钟。

RED：在 `tests.test_daily_delivery_review6.RSSFetcherFrozenRunClockTests` 增加两个真实双 feed
用例，一个直接覆盖 freshness、URL 年份和 item 三个发现时间，一个经过
`NewsAnalyzer._crawl_rss_data`、真实 Local SQLite、first-seen ledger 与
`DailyDeliveryAggregator`。首跑 `Ran 2`，2 failures，exit 1：边界条目全部被过滤，且主链
把 N+1 first_time 写入 N 日库而排除本轮交付。

修复：`RSSFetcher` 接受兼容的可选 clock；`fetch_all` 只捕获一次 run_at 并传给所有
`fetch_feed`，年份替换、freshness reference time、item crawl/first/last 与 RSSData
date/crawl_time 全部使用同一对象。主流程构造 fetcher 时注入入口 frozen run_at；直接调用
fetch_feed/fetch_all 未注入时仍保留 live fallback。GREEN：`Ran 2 in 6.524s, OK`，exit 0。

### 2. Remote AI mutation 失败恢复

根因：Remote strict tag/result 的 non-batch CAS/上传失败会留下 local-authoritative dirty
状态；batch 只保存最后一次局部镜像，无法恢复第一次 mutation 前状态；普通 wrapper 还会
忽略远端上传 False。只读 diff 自审进一步发现 baseline HEAD/下载异常发生在统一 try 外，
普通 API 会意外上抛；strict 批次第二 mutation 返回 False 时仍会上传第一项，而普通批次
合法的 0 no-op 又不能被误判为整批失败。

RED 分组：

- 初始 4 项覆盖 non-batch 412、batch 上传失败、classification 上传失败、普通上传 False，
  首跑 2 FAIL + 2 ERROR，exit 1；
- baseline AccessDenied 的普通 wrapper 首跑 1 ERROR，exit 1；
- 两 mutation strict batch 的底层 False 首跑 1 FAIL（`end_batch_strict` 未失败而上传第一项）；
- 普通 batch 的 0 no-op 兼容首跑 1 FAIL（错误回滚前一成功修改）。

修复：统一 `_run_news_mutation` 在修改前严格刷新/bind baseline，保存 before-image，首次
dirty 即标 local-authoritative；False、异常、CAS/上传失败均关闭连接、原子恢复并清 dirty。
batch 保存每个对象第一次 mutation 的镜像，strict 底层 False 标记整批失败并由 end 恢复
首镜像；普通 0 no-op 只撤销本次空操作，不撤销同批先前成功修改。baseline 或上传异常在
strict 时继续上抛，普通 wrapper 返回约定 failure value。

GREEN：初始 4 项 `Ran 4 in 29.077s, OK`；普通 baseline/upload 两项 `Ran 2 in 14.883s,
OK`；strict False 与普通 no-op 两项 `Ran 2 in 11.978s, OK`，均 exit 0。

### 3. strict period mutation 与 strict latest 读取

根因：SQLite period commit 异常未 rollback；Remote strict wrapper 对底层 False 未恢复，
且拍镜像后底层再次取连接形成刷新窗口。另一方面 Base 的 latest-period 弱默认返回 None，
使第三方后端在 daily/weekly 被解释为“无历史执行”而 fail-open。

RED：period mutation 3 项覆盖 commit failure、Remote False 和单次 bound connection，首跑
3 failures、exit 1。latest capability 4 项覆盖第三方缺能力、Local 正常/坏表、Remote
正常/AccessDenied，首跑 2 failures + 2 errors、exit 1。

修复：period mixin 复用显式 bound connection 并在异常时 rollback；Remote ordinary/strict
record 都走统一 mutation 恢复。Base 新增默认抛 `NotImplementedError` 的
`get_latest_period_execution_strict`，Local/Remote/Manager 显式实现，Scheduler 根据最终
report mode 对 latest/has/record 成对 strict 路由。GREEN：mutation `Ran 3 in 13.610s,
OK`；latest `Ran 4 in 13.863s, OK`，均 exit 0。

### 4. daily_delivery 摘要保持权威 RSS-only

根因：公开 `AI_ANALYSIS.MODE=daily/current/incremental` 会在 daily_delivery 内启用独立历史
热榜读取，重新准备 hotlist 数据并注入摘要，绕过权威 RSS 快照范围。

RED：真实 `_run_ai_analysis` 三个配置 mode 用例均读入独立热榜路径并失败，`Ran 3`、3
failures、exit 1。修复后 daily_delivery 无条件使用传入的权威 RSS stats/current snapshot，
强制 analyzer 的 mode/report_mode 为 daily_delivery，不调用历史热榜准备函数；普通模式仍
尊重原配置。GREEN：`Ran 3, OK`，exit 0。

### 5. 冻结 HTML 与通知输出

根因：HTML factory 会重新读取日期/时间并把 live clock 传给 renderer；通知 dispatcher
虽然持有 clock，但所有 webhook sender 经 bound `AppContext.split_content` 再次使用 live
clock，邮件主题/正文也从 live factory 取时。跨午夜会出现 N 日数据库配 N+1 文件名、页面
生成时间和通知时间。

RED：4 项覆盖主流程参数传播、HTML 路径/renderer clock、真实飞书+钉钉 split/send payload
及真实邮件 MIME Subject/body，首跑 4 ERROR（缺少 operation_at），exit 1。

修复：`AppContext.generate_html` 与 `create_notification_dispatcher` 增加可选 operation_at；
HTML 路径和 renderer、dispatcher clock 与 wrapped split_content 全部绑定同一 frozen
datetime。NewsAnalyzer 完整 run 传 `_run_at`；旧 direct helper 没有 run state 时传 None 并
保留 live fallback。SMTP RFC Date/Message-ID 继续表示物理发送时间。GREEN：`Ran 4,
OK`，exit 0；weekly direct-call 兼容的 2 个旧测试定向复验也 `OK`。

### 6. 文档与兼容收敛

- design spec 与 implementation plan 已同步真实 fetcher frozen clock、权威 RSS-only 摘要、
  Remote before-image/batch 失败恢复、strict latest capability 和冻结展示输出。
- 聚焦首跑唯一失败是旧测试用 daily_delivery 验证弱 latest 转发；该断言与本轮批准的默认
  strict 路由冲突。测试显式传 `strict=False` 后继续验证弱 API 兼容，而默认 strict 行为由
  第三方 fail-closed 测试覆盖。
- 普通 AI batch 的 0 rowcount 保持合法 no-op；普通/weekly direct presentation 调用保留 live
  fallback。没有改动跨进程通知 exactly-once/分布式租约边界。

## 第六次复审最终验证

所有 Python 测试均使用 `docker run --network none` 和镜像内
`/app/.venv/bin/python`，并取得明确 exit code。

- 第六次模块最终：`tests.test_daily_delivery_review6`（因复用调度基类共发现 59 项），
  `Ran 59 in 107.492s, OK`，exit 0。
- 前轮 Remote 相关：review4 + review5，`Ran 61 in 112.462s, OK`，exit 0。
- 报告/邮件/freshness/weekly 相关：`Ran 31 in 0.068s, OK`，exit 0。
- 聚焦：`tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report
  tests.test_news_search_pipeline`，`Ran 163 in 327.609s, OK`，exit 0。
- 固定兼容：weekly digest/schedule/report、Elsevier、proxy、email，`Ran 93 in 210.637s,
  OK`，exit 0。
- 最终全量：`python -m unittest discover -s /workspace/tests -q`，`Ran 500 in 800.135s,
  OK`，exit 0，无 FAIL/ERROR。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、
  `bash tests/test_portable_deployment.sh`、`git diff --check` 均 exit 0；portable 输出
  `PASS: 本地部署路径可移植性检查通过`。

## 第六次复审 Diff 自审

- 一次 fetch_all 的批次、feed、freshness、item 时间与 `{year}` 共享一个 run_at，跨午夜不再
  在保存前丢条目，也不会让 checkpoint 越过错误的 future first_seen。
- Remote AI/period mutation 的 before-image、dirty、连接与 CAS 结果同成败；strict batch
  False 恢复首镜像，普通 zero no-op 不回滚先前成功修改。
- daily_delivery 的三种公开 AI analysis mode 都无法读入热榜；普通模式配置语义未改。
- strict latest 默认不允许第三方弱实现，Local/Remote 异常不能伪装成“未执行”。
- HTML/通知逻辑展示时间冻结，period_label 保留；没有冻结物理发送时间和 retry clock。
- 独立只读 diff 审计在补充两个 Remote 边界后未发现剩余 Critical/Important/Minor。
- 未改变 10:00、`(last_success, now]`、首次 24h、合法空推进、同日成功跳过交付、所有通知
  目标、freshness=2d 或密钥持久化；未合并 main、未部署、未真实推送。

第六次最终测试计数：聚焦 163、兼容 93、全量 500；三套最终命令合计执行 756 个测试
（聚焦与兼容也包含于全量发现），全部明确 exit 0。最终自审无遗留架构冲突。

## 第七次最终复审修复

### 1. strict batch 错误统一 abort

根因：`AIFilterPipeline` 已开启 strict batch 且第一项标签 mutation 已写入本地 dirty 后，
caller-side 标签快照校验/读取失败仍调用 `end_batch_strict()`，从而可能把本应失败的第一项
提交到远端；第二次 `_begin_news_mutation()` 的 HEAD、刷新、连接或 before-image 获取异常也
没有主动把批次标记为失败。

RED：新增真实 Remote + pipeline 两项，分别令第一项标签 mutation 后的 strict 校验失败和
第二次 begin 失败；两项都观察到远端 prompt hash 从批次前值变为新值。第七轮模块首跑
`Ran 5`，其中这两项及普通提交结果两项共 4 failures、exit 1。

修复：Base/Local/Remote/Manager 增加显式 `abort_batch` 契约。Remote abort 不上传，恢复每个
news 对象第一次 mutation 前的 SQLite 镜像，关闭连接并清理 WAL/SHM、dirty、snapshot 与
batch 状态；strict `_begin_news_mutation` 异常在批次内先自保护标记 `_batch_failed`。
pipeline 的 strict storage/read/validation 错误统一走 abort，只有完整成功路径才执行
`end_batch_strict()`。GREEN：两项 `Ran 2 in 11.767s, OK`，exit 0；远端保持批次前 hash，
本地连接、WAL/SHM 与 dirty 状态均已清理。

### 2. ordinary batch 最终提交结果传播

根因：普通 pipeline 的 mutation rowcount 只表示本地 SQLite 写入；Remote CAS/PUT 在
`end_batch()` 才发生。Remote 已能在上传失败时返回 False 并恢复镜像，但 Manager 丢弃返回值，
pipeline 也不检查，最终错误报告 `success=True`。

RED：新增 Manager 传播与真实 ordinary begin→mutation→上传失败两项，均观察到 False 被
吞掉/pipeline success=True；同时第三方 backend 的 legacy `end_batch() -> None` 用例首跑即
通过，用于锁定兼容边界。

修复：Base 将 `end_batch()` 明确为可返回最终持久化结果；Local/Remote/Manager 保留并传播
返回值。ordinary pipeline 只把恒等于 `False` 的结果视为最终持久化失败；第三方旧实现的
`None` 继续兼容，mutation 的 rowcount 0 仍是合法 no-op。GREEN：三项
`Ran 3 in 11.721s, OK`，exit 0；第七轮完整新增模块 `Ran 5 in 22.769s, OK`，exit 0。

### 3. 文档与兼容收敛

- design spec 与 implementation plan 已同步 strict abort、首 mutation 前镜像恢复、backend
  begin 自保护、ordinary 最终提交结果传播，以及 `None`/rowcount 0 的兼容语义。
- 第六轮 Remote failure recovery、strict CAS/dirty、strict period 与 frozen run clock 的契约
  均未放宽；未扩展跨进程通知 exactly-once 范围。
- 普通/weekly 单实例、第三方旧 backend 和合法 zero no-op 行为保持不变。

## 第七次复审最终验证

所有 Python 测试均使用 `docker run --network none` 和镜像内
`/app/.venv/bin/python`，并取得明确 exit code。

- 第七轮新增：`tests.test_daily_delivery_review7`，`Ran 5 in 22.769s, OK`，exit 0。
- 第六轮 + 第七轮：`Ran 64 in 118.873s, OK`，exit 0。
- 第三至第五轮：`Ran 81 in 174.832s, OK`，exit 0。
- 聚焦：`tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report
  tests.test_news_search_pipeline`，`Ran 163 in 310.582s, OK`，exit 0。
- 固定兼容：weekly digest/schedule/report、Elsevier、proxy、email，`Ran 93 in 212.707s,
  OK`，exit 0。
- 最终全量：`python -m unittest discover -s /workspace/tests -q`，`Ran 505 in 874.087s,
  OK`，exit 0，无 FAIL/ERROR。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、
  `bash tests/test_portable_deployment.sh`、`git diff --check` 均 exit 0；portable 输出
  `PASS: 本地部署路径可移植性检查通过`。

## 第七次复审 Diff 自审

- strict 错误路径不再借用 commit cleanup；pipeline 与 Remote begin 各自提供一层 abort 防护。
- abort 恢复整批第一 mutation 前状态，而非只恢复最后一次局部 before-image；连接及 SQLite
  sidecar 一并清理，失败状态不能在下一次 strict 读取时被误提交。
- ordinary commit 结果穿透 backend→Manager→pipeline；只有显式 False 失败，None 与 zero
  no-op 不被误伤。
- 未改变 daily delivery 的时间窗口、RSS-only、checkpoint、通知全目标、strict CAS、标签与
  AI 完整性契约；未合并 main、未部署、未真实推送。

第七次最终测试计数：聚焦 163、兼容 93、全量 505；三套最终命令合计执行 761 个测试
（聚焦与兼容也包含于全量发现），全部明确 exit 0。最终自审无新增遗留问题。

## 第八次最终复审修复

### 1. Remote SQLite 一致快照与 WAL

根因：Remote 的 mutation before-image 和最终上传都直接读取主 `.db`；SQLite 使用持久 WAL
时，`commit()` 只保证连接视图已提交，不保证 WAL 已 checkpoint 回主库。于是当前进程通过
开放连接能读到新状态，而新 Remote observer 下载的对象仍可能是旧状态；普通批次中第二个
0 no-op 的局部回滚也可能把第一个已成功 mutation 的 WAL 状态丢掉。

RED：新增持久 WAL 三条真实 Remote 测试。ordinary mutation + 0 no-op 和 strict end 均由
全新 observer 读到旧 hash，`Ran 3` 中 2 failures、exit 1；strict abort 的旧状态基线用例
首跑通过。

修复：新增基于当前 bound `sqlite3.Connection.backup()` 的一致快照 helper。before-image、
news/RSS/first-seen SQLite 上传都从同一连接生成独立单文件镜像，包含已提交 WAL；不切换
journal mode，不引入既有库迁移风险。Remote mutation 将已绑定连接显式传到最终上传，CAS
payload 与本地 mutation 视图一致。GREEN：WAL 三项 `Ran 3 in 18.991s, OK`，exit 0；三个
场景均由全新 backend observer 验证远端内容。

### 2. abort/rollback 失败的安全状态机

根因：旧 abort 在 `finally` 无条件清除 snapshot、dirty 与 batch failure，即使恢复本身失败；
dirty 但没有 before-image 的 token 只清 sidecar，不删除主库/provenance。失败后的本地脏库
因此可能继续被 strict read 当成权威内容，甚至在后续批次提交。

RED：新增 replace、connection close、WAL sidecar 三类恢复故障、安全失效也失败、以及
dirty 无 before-image 共五项。首跑 `Ran 5`，5 failures、exit 1：失败 token 被清空，本地
strict read 仍看到 aborted hash，且后续 begin 未被 poison 拒绝。

修复：每个 `(date, db_type)` token 只有进入以下终态才从 pending 集合移除：一致镜像恢复
成功；关闭连接并删除主库/WAL/SHM/journal/temp、清 authoritative/provenance 的安全失效
成功；或安全失效也失败后写入独立、跨 batch 保留的 poison。poison token 的 strict refresh、
dirty 标记和 mutation 均确定性拒绝；dirty 无 snapshot 直接安全失效并在下次 strict read
重新下载。pipeline 不再吞掉 abort failure，而把原始阶段错误与回滚/关闭错误合并到失败
结果。GREEN：恢复状态五项 `Ran 5 in 28.386s, OK`，exit 0；pipeline 错误传播与单次 end
两项 `Ran 2 in 0.002s, OK`，exit 0。

### 3. ordinary batch 只终结一次

根因：ordinary `end_batch()` 明确返回 False 或抛错后，pipeline 的通用错误 cleanup 又调用
一次 `end_batch()`。Remote 的第二次调用通常恰好为空，但第三方后端没有幂等保证，可能
发生重复提交或掩盖首次失败。

RED：第三方 fake 令 `end_batch.side_effect=[False, True]`，观察到 call count 为 2，测试
失败。修复：pipeline 在调用提交前记录 `batch_commit_attempted`；ordinary cleanup 仅在尚未
尝试最终提交时关闭一次，strict 错误仍走 abort。显式 False 仍是失败，第三方 `None` 与
rowcount 0 no-op 语义不变。GREEN：测试观察到 call count 精确为 1。

### 4. 文档与兼容收敛

- design spec 与 implementation plan 已同步 bound connection backup、安全失效/poison、
  dirty 无 snapshot 和 ordinary 单次终结契约。
- 原子保证仍限定为单个 Remote SQLite 对象；未声称多对象分布式事务或跨进程通知
  exactly-once，也没有强制切换 SQLite journal mode。
- review4/review5 的 Remote raw RSS、ledger、共享 news CAS 与 provenance 契约均未放宽；
  普通 0 no-op、第三方 `end_batch() -> None` 和 weekly 行为保持兼容。

## 第八次复审最终验证

所有 Python 测试均使用 `docker run --network none` 和镜像内
`/app/.venv/bin/python`，并取得明确 exit code。

- 第八轮新增：`tests.test_daily_delivery_review8`，`Ran 10 in 48.753s, OK`，exit 0。
- 第六至第八轮：`Ran 74 in 168.357s, OK`，exit 0。
- Remote/raw RSS/ledger 相关 review4 + review5：`Ran 61 in 110.256s, OK`，exit 0。
- 聚焦：`tests.test_daily_delivery tests.test_daily_delivery_schedule tests.test_daily_delivery_report
  tests.test_news_search_pipeline`，`Ran 163 in 332.378s, OK`，exit 0。
- 固定兼容：weekly digest/schedule/report、Elsevier、proxy、email，`Ran 93 in 211.989s,
  OK`，exit 0。
- 最终全量：`python -m unittest discover -s /workspace/tests -q`，`Ran 515 in 864.236s,
  OK`，exit 0，无 FAIL/ERROR。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、
  `bash tests/test_portable_deployment.sh`、`git diff --check` 均 exit 0；portable 输出
  `PASS: 本地部署路径可移植性检查通过`。

## 第八次复审 Diff 自审

- WAL 一致性不再依赖主库 checkpoint；before、CAS payload 与 mutation 共用连接视图。
- abort token 不会在恢复失败时静默失踪；安全失效可恢复时下一次 strict read 必须重下，
  无法安全失效时 poison 跨 begin 保留并拒绝 strict 访问。
- strict pipeline 报告原始错误和 rollback failure；ordinary 最终提交只尝试一次。
- review6/7、Remote/raw RSS/ledger、聚焦、固定兼容及全量均明确 exit 0；静态 diff 审计未
  发现新的直接缺陷。
- 未改变 10:00、`(last_success, now]`、首次 24h、合法空推进、RSS-only、所有通知目标、
  strict period 或密钥持久化边界；未合并 main、未部署、未真实推送。

第八次最终测试计数：聚焦 163、兼容 93、全量 515；三套最终命令合计执行 771 个测试
（聚焦与兼容也包含于全量发现），全部明确 exit 0。最终自审无遗留架构冲突。

## 第八次复审迟到审计补充修复

提交 `89bc9d53` 后收到已在途的独立只读审计结果；其中两项 Important 经生产数据流复核
成立，因此保留原提交并以 follow-up 修复，不隐瞒或忽略迟到反馈。

### 1. 普通热榜保存统一恢复状态机

根因：`Remote.save_news_data()` 的本地保存 False 与 CAS/upload 异常分支仍直接调用
`_restore_local_sqlite_snapshot()`，绕过新建的 restore→safe invalidate→poison 状态机；第一次
raw restore 失败还会落入外层 except 再次 raw restore。持续 restore/close/sidecar 故障会
让异常逃出普通 fail-soft API，且没有 poison，后续 strict read 可能复用已变异本地库。

RED：真实 Remote baseline 保存一条热榜，令 conditional PUT 返回 ServiceUnavailable，并同时
注入 restore 与 invalidate 失败。调用直接抛出 `OSError: restore unavailable`，没有返回 False，
也没有 poison。该项与后述两项合跑 `Ran 3 in 15.013s`，2 failures + 1 error，exit 1。

修复：热榜本地事务 False 也进入唯一异常出口；凡已取得 before-image 的失败统一调用
`_restore_or_invalidate_sqlite_token()`。恢复成功则回到 baseline；恢复失败但安全失效成功则
后续 strict read 重下；二者都失败则 poison。普通热榜 API 记录组合错误并稳定返回 False，
而 poison 令后续 strict read 确定性拒绝，远端 observer 始终保持旧状态。

### 2. backend 原错误与 rollback 错误同时保留

根因：`_run_news_mutation()` 的 mutation 和 upload except 先直接调用 rollback；一旦 rollback
本身抛错，Python 用 rollback 异常替换原始 mutation/CAS 异常。pipeline 虽会报告 cleanup
错误，却已拿不到 `mutation exploded` 或 PUT ServiceUnavailable 的首因。

RED：新增真实 strict pipeline mutation 和真实 conditional PUT 两条。restore 首次失败而
safe invalidation 成功时，结果分别只含 `restore exploded`，丢失 `mutation exploded`；以及只
含 `restore after upload exploded`，丢失 ServiceUnavailable。

修复：mutation、upload exception 与显式 upload False 三条路径分别保留原异常，再单独捕获
rollback 异常并生成包含两者类型/消息的组合错误，以原异常作为 exception cause。strict 向
pipeline/调用方上抛组合错误；ordinary 在本地已恢复、失效或 poison 后仍按旧 fail-soft 返回
failure value。GREEN：三项 `Ran 3 in 16.007s, OK`，exit 0。

### 3. 补充验证与文档

- review4 至 review8 的 Remote/raw RSS/ledger/CAS/rollback 组合：`Ran 138 in 283.014s,
  OK`，exit 0。
- 聚焦：`Ran 163 in 357.167s, OK`，exit 0。
- 固定兼容：`Ran 93 in 225.306s, OK`，exit 0。
- 最终全量：`python -m unittest discover -s /workspace/tests -q`，`Ran 518 in 873.187s,
  OK`，exit 0，无 FAIL/ERROR。
- `bash -n docker/entrypoint.sh`、`bash -n config/daily.crontab`、
  `bash tests/test_portable_deployment.sh` 与 `git diff --check` 均 exit 0；portable 输出
  `PASS: 本地部署路径可移植性检查通过`。
- design spec/implementation plan 已明确普通热榜保存不得有 raw restore 后门，backend
  mutation/PUT 原始异常与 rollback failure 必须同时可诊断。

补充后的第八次最终计数：聚焦 163、兼容 93、全量 518；三套最终命令合计执行 774 个测试
（聚焦与兼容也包含于全量发现），全部明确 exit 0。未合并 main、未部署、未真实推送。

---

# 每周 PDF 最终整分支审查 A–F 修复

日期：2026-08-10
待审基线：`0d703bcd`
工作树：`previous-day-window`
范围：`.superpowers/sdd/final-fix-brief.md` 的 A–F 全部项。

本轮只修改代码、测试和文档；未部署，未读取或改写真实
`docker/.env`，未移动 `output`，未真实调用企业微信，未修改 `uv.lock`
或本地 `.venv`，未改写历史。

## 执行方法与验证环境

- 依次沿 `run -> 采集 -> weekly snapshot -> strict AI -> PDF -> dispatcher -> Scheduler/storage`
  生产调用链核对 A→B→C→D→E→F，每项先取得有效 RED，再做最小 GREEN。
- 从当前未变更的 `docker/Dockerfile` 新建临时验证镜像
  `trendradar-final-fix:0d703bcd`，构建 exit 0。
- 所有 Python 测试均由镜像内 `/app/.venv/bin/python` 执行，测试容器使用
  `--network none`。最终全量另加 `PYTHONDONTWRITEBYTECODE=1` 和工作树只读挂载。
- LiteLLM 断网下下载价格表失败后使用本地备份的 warning 符合预期，不是测试失败。

公共 Python 前缀：

```bash
docker run --rm --network none \
  --entrypoint /app/.venv/bin/python \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /workspace trendradar-final-fix:0d703bcd
```

## A. 权威 weekly 快照

### 根因

`_process_rss_data_by_mode()` 在空周时早退，未设置 `_rss_window`、空的
`_allowed_rss_ids` 和 `_rss_ids_authoritative=True`；后续 AI 还会解释当天无界 RSS ID。
另一处为 weekly 严格 AI 过滤和结果转换未显式传 `operation_date`，补跑时
默认落到 wall-clock 日库，会丢失或串用周一快照的本地整数 ID/分类结果。

### RED

- 增强 `test_weekly_snapshot_scope_flows_from_aggregator_to_both_ai_steps` 和
  `test_empty_week_does_not_fall_back_to_current_rss`；首跑两项均失败：空周早退丢失
  authority，weekly filter/conversion 的 `operation_date` 为 `None`，exit 1。
- 补入真实 Local SQLite 生产链用例：运行日有窗口外 RSS 的空周 + 气象 PDF；
  同周失败重试与周三 `--force-weekly` 重建的 snapshot ID 一致性。

### GREEN

- 聚合完立即先写入 window/allowed IDs/authority/period label，空周返回权威空列表。
- weekly filter 和 conversion 均使用 `window.end.strftime("%Y-%m-%d")`。
- 两项实库链测试：`Ran 2 in 80.801s, OK`，exit 0。

文件：`trendradar/__main__.py`、`tests/test_weekly_schedule.py`。

## B. 严格 weekly 摘要

### 根因

`_run_ai_analysis()` 仍允许公开 `AI_ANALYSIS.MODE=daily/current/incremental` 切换到历史热榜，
并且 `AIAnalyzer` 只在 daily delivery 传 `strict=True`。因此 weekly 可绕过已选快照，
且 JSON 修复失败、缺叙事或 grounding 失败可继续到 PDF/checkpoint。

### RED / GREEN

- RED：`test_weekly_summary_ignores_public_mode_and_runs_strict_on_selected_data`
  的 3 个公开 mode 子场景均进入错误数据源；
  `test_weekly_summary_failure_aborts_before_pdf_and_checkpoints` 也未在 PDF 前按严格失败终止，exit 1。
- GREEN：weekly 与 daily delivery 共用唯一严格分支，强制使用调用方传入的 selected
  stats/RSS，不调用历史热榜准备；两项全部 `OK`。
- 复用严格 AI engine 的 JSON 修复、grounding 和空 JSON 用例：`Ran 3, OK`。

文件：`trendradar/__main__.py`、`tests/test_weekly_schedule.py`。

## C. 按账号 PDF 幂等投递

### 根因与存储契约

原 dispatcher 每次都向全部企业微信账号重发，只有最后的全局 `push`
检查点；A 成功/B 失败或全账号成功后全局 checkpoint 写失败，都会确定性重发。

现有 `period_executions` 支持 `(execution_date, period_key, action)` 唯一键与 strict
Scheduler 读写，与需求无冲突，因此未建第二个 DB。账号 action 为：

```text
execution_date = weekly window end
period_key     = monday_weekly
action         = weekly-pdf:<PDF SHA256>:<namespaced webhook SHA256>
```

Webhook 原文只在内存中用于计算命名空间化 SHA256 和真实外呼，不写账本、
不写 action、不输出日志。

### RED

- 新增部分成功、全局 checkpoint 失败、账本读失败 3 项；首跑观察到 A/B 都重发、
  外呼总数 4 且 dispatcher 无 ledger callback，3 项失败，exit 1。
- 真实 `run()` 部分投递恢复用例首跑因不存在恢复入口而 RED，exit 1。

### GREEN

- dispatcher 先完成全部账本读再做第一次外呼；只发未成功账号，每个成功外呼后
  立即写对应 action，任一读/写失败均 fail-closed。
- 专用 PDF 路径变为可确定计算；`run()` 在气象和采集前检测部分账本，
  复用同一份已投递 PDF 的 digest 续投。
- 新增 4 项全部 GREEN；`tests.test_weekly_pdf_delivery` 及两个受影响 schedule 用例
  合跑 `Ran 20 in 0.144s, OK`。

文件：`trendradar/__main__.py`、`trendradar/notification/dispatcher.py`、
`trendradar/report/weekly_pdf.py`、`tests/test_weekly_pdf_delivery.py`、`tests/test_weekly_schedule.py`。

## D. weekly 来源/历史存储失败关闭

### 根因与空日库契约

- 当前固定 RSS 和 news-search 部分失败只对 daily delivery 严格，weekly 会带着不完整当日记录继续。
- `WeeklyRSSAggregator` 使用弱 `get_rss_data`，把缺库、坏库与读取异常吞成 `None`，
  并且只记录历史 `failed_ids` 而不终止。
- 真实 SQLite 存储已有可用区分：“保存过的成功空抓取”含 `rss_crawl_records`，
  `get_rss_data_strict()` 返回空 `RSSData`；缺库或无 crawl record 返回 `None`，坏库严格
  读取抛错。weekly 聚合器必须解释这个既有契约，而不能把 daily 依赖的 `None` 语义改掉。

### RED

- 真实 SQLite 三项：8 个已保存空日首跑已通过；缺失/坏库两个子场景和历史
  failed source 均未抛错，形成有效 RED，exit 1。
- 当前固定 RSS 失败和 news-search 部分失败两项均未在 snapshot 前抛错，exit 1。

### GREEN

- weekly aggregator 对全部 8 个 expected storage date 使用 strict data/ID 读取；任一
  `None`、strict 异常或最新 `failed_ids` 立即失败，合法空 `RSSData` 继续。
- 最终全量首轮暴露：若在 Local backend 全局把 strict 缺库改成直接抛错，会破坏 daily
  aggregator 的既有职责边界。该 overbroad 改动已撤回；fail-closed 只留在 weekly
  aggregator。daily、weekly strict 三状态与进程锁组合补验 `Ran 26 in 269.511s, OK`。
- 当前固定/news-search 两项：`Ran 2 in 0.081s, OK`。
- 历史 strict 真库三项：`Ran 3 in 110.919s, OK`。
- 整个 `tests.test_weekly_digest` 将旧的“未保存日=可忽略”夹具迁移为显式完整空日；
  `Ran 42 in 378.072s, OK`。

文件：`trendradar/__main__.py`、`trendradar/core/weekly.py`、
`tests/test_news_search_pipeline.py`、`tests/test_weekly_digest.py`。

## E. 本地 cron 对齐

### RED / GREEN

- RED：`tests/test_portable_deployment.sh` 改为解析活动 cron 行并校验精确数量/时刻/force 数；
  首跑“期望 5，实际 1”，exit 1。
- GREEN：`config/daily.crontab` 保留每天 10:00，新增周一 10:30、11:00、11:30、12:00
  四个 `--force-weekly` 短任务；portable 输出
  `PASS: 本地部署路径可移植性检查通过`，exit 0。

文件：`config/daily.crontab`、`tests/test_portable_deployment.sh`。

## F. 清理和 README 可发现性

### RED / GREEN

- RED：天气 config/loader 精确集合与 README 链接用例首跑 `Ran 3`，共 9 个失败断言，
  exit 1：旧 optional 字段仍在 loader/YAML/计划/测试，README 没有兼容入口。
- GREEN：从 loader、中英文 YAML、实现计划和旧测试契约完全移除 optional 字段；
  天气仍是 weekly 生产路径中固定必须项。
- README/README-EN 增加简短链接式兼容章节，恢复 `current`/`daily`、安装、MCP 和普通
  `notification` 的入口；未恢复旧 freshness、rolling-time、PDF preview 或 text fallback 周报路径。
- 聚焦复跑：`Ran 3 in 0.051s, OK`。产品树静态搜索旧 optional 字段为 0。

文件：`trendradar/core/loader.py`、`config/config.yaml`、`config/config.en.yaml`、
`docs/superpowers/plans/2026-08-10-weekly-pdf-delivery.md`、`README.md`、`README-EN.md`、
`tests/test_agro_weather.py`、`tests/test_weekly_configuration.py`。

## 最终验证

### 聚焦组合

```bash
<公共 Python 前缀> -m unittest \
  tests.test_weekly_time_rule \
  tests.test_sciencedirect_rss_dates \
  tests.test_news_search \
  tests.test_news_search_pipeline \
  tests.test_weekly_digest \
  tests.test_agro_weather \
  tests.test_weekly_schedule \
  tests.test_weekly_pdf_report \
  tests.test_wework_pdf \
  tests.test_weekly_pdf_delivery -q
```

首跑：`Ran 253 in 421.652s`，1 failure，exit 1。唯一失败是旧
`test_weekly_snapshot_exception_is_not_swallowed` 夹具本身带 `fixed-failure`；D 的新契约正确在
snapshot 前失败，使用例无法到达其原本要验证的 snapshot 异常。将该用例的“当前抓取”
明确设为成功后，定向 `Ran 1 in 0.062s, OK`，exit 0。根据审查协调不再重复运行
253 项；随后唯一次全量 discovery 覆盖修正后的全部聚焦用例。

### 普通模式兼容

```bash
<公共 Python 前缀> -m unittest \
  tests.test_elsevier_full_text \
  tests.test_direct_first_proxy \
  tests.test_email_delivery -q
```

结果：`Ran 36 in 0.033s, OK`，exit 0。

### 全量、真实 PDF 与静态

第一次 full discovery 为 `Ran 618 in 1082.742s`，5 failures + 12 errors，exit 1。
其中 16 项同源于上述 Local strict overbroad 改动；另 1 项为 multiprocessing SemLock
单次启动异常。撤回该改动后，daily aggregator 全类、review6 真实双 feed、weekly strict
三项及 nonblocking attempt lock 合跑 `Ran 26 in 269.511s, OK`，exit 0，既验证契约兼容，
也确认 SemLock 为一次性测试环境异常。

修复后的 full discovery：

```bash
<公共 Python 前缀> -m unittest discover -s tests -q
```

结果为 `Ran 618 in 1097.090s`，无 failure、1 error，exit 1。唯一 error 是测试
`test_html_generator_forwards_period_metadata` 尝试写仓库根 `index.html`，而最终 full
刻意使用只读源码挂载，触发 `OSError: [Errno 30] Read-only file system`；其余 617 项无
FAIL/ERROR。这不是产品代码或断言失败。按最终验证协调没有第三次重复全量，而是保留只读
仓库，并把唯一目标文件覆盖到 `/tmp` 可写隔离文件后补验该单项：
`Ran 1 in 0.030s, OK`，exit 0。

真实 Chromium PDF：

```bash
<公共 Python 前缀> -m unittest \
  tests.test_weekly_pdf_report.WeeklyPdfGenerationValidationTests.test_actual_chromium_output_is_a4_multipage_with_repeated_chinese_furniture -q
```

结果：`Ran 1 in 11.810s, OK`，exit 0；覆盖真实 A4、多页、中文字体和重复页眉页脚。

静态/便携验证：

- 镜像内 `bash -n docker/entrypoint.sh`：exit 0。
- 镜像内 `bash -n config/daily.crontab`：exit 0。
- 镜像内 `bash tests/test_portable_deployment.sh`：
  `PASS: 本地部署路径可移植性检查通过`，exit 0。
- 产品树 `required_for_weekly` 静态搜索：0 命中；weekly 主线的 preview、text fallback、
  rolling、freshness 静态搜索：0 命中。
- `git diff --check`：exit 0。

## Diff 自审

- weekly 普通新闻仍只在 `NaturalWeekWindow.contains(item.published_at)` 以
  `[previous Monday 00:00, current Monday 00:00)` 判定；first/crawl 时间只用于保存元数据。
- 气象仍是唯一产品周期例外且为强制专栏；未发布、结构错误或周期不符均失败关闭。
- 空 weekly snapshot 也携带权威 window/ID 集；AI filter、conversion、summary、20 条选择和
  PDF 沿同一条 weekly 主线，无第二个 hotlist/文字回退分支。
- 按账号账本复用 strict period store，不存 webhook 原文；已关闭部分成功和全局
  checkpoint 失败的所有确定性重发路径。
- strict 历史读取保留了“已保存成功空日”，不会把合法空日与缺失/坏库一律处理。
- 当前固定 RSS、news-search、任一历史日库、strict AI、PDF、账号 ledger、
  企业微信文件和全局 checkpoint 任一失败均不将本周标为成功。
- 没有 `.env`、Webhook/API key、`output`、cache、`uv.lock` 或 `.venv` 进入 diff；
  不部署、不真实发送。

## 已知边界/疑虑

外部企业微信文件调用与本地/远端 period ledger 无法参与同一个分布式事务。
若进程在企业微信已成功、但对应账号 ledger 写成功前崩溃（或 ledger 写确定失败），
重试时该账号仍可能收到一次重复 PDF。这是无企业微信幂等键/查询回执 API 时不可避免的
外部调用/崩溃歧义；本轮已关闭其他所有可确定恢复的部分成功和全局 checkpoint 失败路径。
