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
