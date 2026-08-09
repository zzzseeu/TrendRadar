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
