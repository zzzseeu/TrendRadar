# 每日成功检查点推送设计

## 目标

TrendRadar 每天北京时间 10:00 执行一次采集、分析和推送。推送范围不是固定自然日，
而是“上次完整成功周期之后首次发现的内容”。采集、AI 或通知失败时不推进成功检查点，
后续运行继续处理未成功交付的积压内容。

自然周聚合能力继续保留，但当前运行时间线不再使用 weekly 模式。

## 运行语义

- Docker Cron 保持 `0 10 * * *`。
- 时间线每天使用同一个 `daily_delivery` 周期，采集、分析和推送均启用。
- 单次运行在入口按配置时区只读取一次 `run_at`，并冻结对应 `run_date`；即使执行跨过
  午夜，调度解析、RSS 原始快照、AI 严格数据、通知与检查点仍全部归属该运行日期。
- `RSSFetcher.fetch_all` 将同一个冻结 `run_at` 传给每个 feed；URL 年份、新鲜度边界、
  item 的 crawl/first/last 时间和批次元数据不得在源之间重新读取 wall clock。HTML
  文件路径、页面生成时间、通知更新时间和邮件逻辑主题/正文也复用该时钟；SMTP 的物理
  传输头仍使用实际发送时间。
- 每个日期只成功记录一次；同日人工补跑只在前一次未完整成功时继续执行。
- 首次没有历史成功检查点时，窗口起点为当前运行时间前 24 小时。
- 后续窗口为 `(last_success_at, current_run_at]`。
- 合法无匹配内容时不发送空消息，但周期视为成功并更新检查点。
- 数据源、快照、AI、报告或任一配置通知端点失败时，周期失败且检查点保持不变。

## 数据聚合

新增每日交付聚合器，读取检查点日期至当前日期的 RSS 数据库，并按条目的首次发现时间
筛选。首次发现时间优先使用 `first_time`，其次使用 `crawl_time`；发布日期只用于展示，
不作为是否补推的唯一依据，因此晚索引的旧文章仍可进入下一次推送。

聚合器沿用周快照的稳定身份和去重规则：

1. 优先使用 canonical URL；
2. 无可用 URL 时使用 `feed_id + normalize_title(title)`；
3. 合并重复搜索来源和覆盖度，保留信息更完整的记录；
4. 将交付快照写入当前日期数据库并逐项校验 SQLite ID；
5. AI 只处理本次快照允许的 ID，不能读取窗口外历史内容。

系统级首次发现不通过每次请求重新扫描历史日库计算。存储层维护固定的版本化
`rss/first-seen-v1.db` 账本，以相同 canonical URL/标题 fallback identity 为主键，
只允许用更早时间更新。每个 RSS 日库在保存原始条目的同一 SQLite 事务中写入
`rss_first_seen_outbox`；条目、crawl record 与 outbox 要么全部提交，要么全部回滚。
无 URL/GUID 的条目使用与账本 identity 一致的稳定标题 GUID，不能被静默跳过。

账本记录各源日库的 durable generation/远端对象版本、已消费 generation watermark 与
write ID。每次保存前后和每次 strict 查询前，仅查询
`source_generation > watermark` 的 outbox 并幂等消费；兼容 `rss_items` 回填只允许发生在
初次迁移、watermark 为 0 且没有 outbox 时。因此 raw 日库已提交而账本同步失败时，
新进程即使本轮没有再次抓到旧 payload，也可从 durable outbox 恢复。账本持久化失败仍
等同于 RSS 保存失败，且账本不承担 delivered 标记。

升级时，账本仅在缺失或版本不兼容时严格枚举并回填截止当前窗口的全部既有 RSS 日库，
写入 `backfill_complete` 和各 source version/watermark；以后只打开 generation 或远端
provenance 新增/变化的日库，稳定查询不再打开或下载历史库，并只按本轮候选 identity
查询索引。外部 provenance 只负责发现变化：消费方必须在读取前绑定 inventory 返回的
`listed_version`，在同一 SQLite 只读事务读取 generation/outbox/兼容数据，并在读取后
再次确认 provenance 未变；账本只记录实际读取的 listed version、generation 与
watermark，不能把读取后观察到的新版本误记为已消费。Local 的 DB/WAL provenance 同样
执行前后校验。远程账本、RSS 日库、strict AI 数据库和成功检查点的 strict 读取以
VersionId/ETag 绑定对象来源；远端对象新增或版本变化时关闭旧连接、临时下载并原子替换。
本地 strict mutation 从首次 dirty 起即为 authoritative，版本变化或 404 不得刷新覆盖。
strict 上传必须使用服务端 `If-Match` 或 `If-None-Match: *` 条件 PUT，并校验 PUT 返回的
ETag/VersionId 与最终 HEAD 一致；不支持条件写的远端后端明确失败关闭。由于
`news/{date}.db` 同时承载热榜、AI 标签/结果与 period 状态，Remote 对该共享对象的所有
生产写者（包括普通热榜保存）都必须从严格刷新并绑定的 baseline 派生，再通过同一 CAS
提交；普通业务可对冲突 fail-soft，但不得用无条件 PUT 覆盖严格状态。Remote 批次必须
提供独立于提交的 abort：任一 strict caller 校验、读取或后续 mutation 建立 baseline 失败时，
恢复该批第一次 mutation 前镜像并清理连接、WAL/SHM 与 dirty，不能用提交型 end 做错误清理。
普通批次的 end 必须把最终 CAS/PUT 结果传播到流水线；仅显式 `False` 表示失败，第三方旧
后端返回 `None` 保持兼容。before-image 与上传 payload 必须由对应的当前 SQLite connection
通过 `backup()` 生成一致单文件快照，不能直接读取可能落后于已提交 WAL 的主 `.db`。恢复
失败时先安全失效本地 DB/sidecar/provenance 并强制重下；安全失效也失败则保留 poison token，
后续 strict 读写确定性拒绝。共享 news 的普通热榜保存也必须复用同一恢复入口并返回失败，
不能保留 raw restore 后门；mutation/CAS 原始异常与 rollback 异常必须同时保留供流水线诊断。
流水线已尝试 ordinary end 后不得在 cleanup 中再次提交。

首次运行只读取最近 24 小时；已有成功检查点后不设置静默丢弃上限，积压内容保留到
完整成功为止，并交给现有通知分批机制处理。

## 成功检查点

在现有调度执行记录之上增加“最近成功交付时间”读取能力，使用周期 key
`daily_delivery` 和 action `push`。执行记录必须保留准确的 `executed_at`，并可跨日期
查找最新一条成功记录。

处理顺序为：

1. 读取最近成功检查点；
2. 抓取并保存当天数据；
3. 构建交付快照；
4. AI 筛选、摘要和报告生成；
5. 向所有已配置端点发送；
6. 全部成功后记录 push，并以其 `executed_at` 作为下一次窗口起点。

`daily_delivery` 与保留的 `weekly` 的 analyze/push 状态读取和记录都使用 explicit strict
period API；任何权限、网络、坏库或陈旧缓存错误必须在发送前 fail-closed。
Remote 与 raw RSS、first-seen、strict AI 标签/结果共用同一 conditional-CAS 提交协议。
strict API 同时包含 has、latest 和 record；基类默认抛 `NotImplementedError`，不能用
返回 `None` 的弱默认把“不支持/读取失败”解释为未执行。period 本地 commit 异常必须
rollback；Remote 的 False、异常或 CAS 失败必须恢复 mutation 前镜像并清理 dirty 状态。

无内容周期跳过第 4、5 步，但仍记录成功检查点。任何失败都返回非零退出码。

## 重试和兼容性

- 已记录 analyze、但尚未记录 push 时，补跑必须重新生成或取得有效 AI 结果，不能被
  `once_analyze` 阻断。
- push 已成功时，同日再次运行不得重复分析或发送。
- 普通 `daily`、`current`、`incremental` 和保留的 `weekly` 代码路径保持可用；只把
  当前 `custom` 时间线切换到每日交付模式。
- 多端点发送仍要求全部成功。部分端点成功后整体失败时，下次可能向已成功端点重复
  发送；避免这种重复需要逐端点投递账本，不在本次范围内。
- `daily_delivery` 的 AI 分类协议、最终 grounding 摘要和标签生命周期全部 fail-closed。
  分类响应中的未知/重复 ID、未知标签、缺字段、非法元素或空摘要仅允许修复一次；只有
  精确 `[]` 表示合法无匹配。grounding 和显示配置裁剪后，最终对象仍须有可交付叙事。
- `daily_delivery` 始终把本轮权威 RSS 快照作为 AI 摘要唯一输入；公开配置中的
  `AI_ANALYSIS.MODE=daily/current/incremental` 不得触发历史热榜读取或把热榜摘要注入
  交付。普通报告模式继续尊重这些配置。
- strict flat schema 的 news/tag ID 必须是非布尔整数，score/importance 必须是有限的
  JSON 数值且位于 `[0,1]`，summary 必须是非空字符串；数值字符串、NaN/Infinity 和
  null/object/list/bool 均进入同一次 repair，repair 后仍非法则整批失败。
- strict 标签使用同一事务快照读取 active 集合、顺序、描述、priority、version 和
  prompt hash；hash 变化时全量提取并原子替换、读回校验。普通模式继续使用现有增量、
  fail-soft 标签更新和缓存语义。
- 第三方存储只有显式实现 strict RSS、标签和持久化接口后才能用于 `daily_delivery`；
  未实现时明确失败，普通模式的弱接口保持兼容。

## 配置调整

- `config/timeline.yaml` 的 `custom` 改为每天 `daily_delivery`。
- 报告类型显示为“每日新增”，并展示“上次成功时间—本次运行时间”的范围。
- 新闻搜索保留 48 小时重叠窗口，用于发现延迟索引内容；是否进入推送由首次发现时间
  和成功检查点决定。
- RSS `max_age_days` 保持 2 天，避免采集层提前丢弃延迟发现的内容。

## 测试与验收

- 首次运行只包含最近 24 小时首次发现的内容。
- 成功检查点之后的新增内容进入下一次推送，旧内容不重复。
- 前一次通知失败后，下一次包含前次积压和本次新增。
- 合法空周期不发送消息但推进检查点。
- 数据源、AI、快照或通知失败时检查点不变、进程退出码为 1。
- 同日成功后补跑不重复分析和发送。
- 延迟发现但发布日期较旧的内容仍可推送。
- SQLite 快照混合 URL/标题身份时保持幂等且 ID 完整。
- 多历史库只回填 first-seen 账本一次，后续候选查询不再打开历史日库；保存失败重试
  能由新进程从 durable outbox 补齐账本，跨 feed canonical 重现仍以系统最早时间裁决。
- title-only 原始 RSS 持久化与 outbox 原子提交；批次任一 SQLite 错误整批回滚。
- 远程 strict 读取覆盖缓存 404 后对象出现、旧版本更新、坏库/权限/网络错误和并发上传；
  checkpoint 与 first-seen 账本均不得使用陈旧连接。
- 远程 strict 写覆盖 existing/create 条件 PUT、pre-PUT/PUT 后/创建竞争、dirty read 冲突
  和 strict period CAS 失败本地回滚；不承诺跨进程通知 exactly-once，端点仍可能重复。
- Remote AI 标签/分类的 batch 与 non-batch mutation 覆盖 412、上传失败和 strict 底层
  False：严格批次恢复首次 mutation 前镜像、关闭旧连接并清除 dirty；caller 校验或第二次
  mutation 的 HEAD/refresh/connection/before-image 失败必须显式 abort，backend 也要把
  strict begin 失败标记为整批失败，禁止错误 cleanup 提交首项。普通 wrapper 和最终
  `end_batch` 都必须向调用方反映远端提交失败，但第三方 `None` 与普通批次返回 0 的合法
  no-op 保持兼容，不回滚先前成功修改。后续 strict 读取只能看到权威远端状态。
- Remote 持久 WAL 覆盖 ordinary 成功 mutation + 0 no-op、strict end 与 strict abort，并由
  全新 observer 验证 connection backup 上传/恢复内容；restore replace、connection close、
  WAL sidecar 清理失败必须安全重下，restore 与失效双失败必须 poison，dirty 无 before-image
  也不得复用本地主库；热榜 save 失败同样走 safe invalidate/poison。strict mutation 与真实
  PUT 失败后必须同时报告原始错误和 rollback 错误。ordinary end 返回 False 或抛出后
  cleanup 只能观察结果，不能二次 end。
- first-seen 消费覆盖 Remote 读取 v2 期间变为 v3、Local DB/WAL 读取期间变化，以及同一
  日库多 generation 的真正增量 watermark；失败不得推进水位，下一轮仍须处理新 outbox。
- Remote 共享 news DB 覆盖旧热榜写者不能覆盖新 checkpoint/AI 状态；热榜保存失败必须
  在主流程继续生成 TXT、分析或发送前中止。
- strict period 读取覆盖 404、权限/网络/坏库和远端版本刷新；调度器必须按 report mode
  对 has/record 成对路由严格接口。
- 跨午夜主链覆盖冻结 N 日的 RSS 保存、快照、AI 标签/结果/ID、通知和检查点；N+1 日的
  同 ID 数据不得被误读；两个真实 feed 必须共用 N 日 freshness/发现时钟，HTML 路径、
  飞书/钉钉 payload 与邮件主题/正文也保持 N 日。
- strict latest-period 覆盖第三方缺能力、Local 坏表及 Remote 404/版本变化/AccessDenied；
  daily_delivery 与 weekly 在读取 latest/has 和记录 record 时必须成对严格路由。
- strict 分类覆盖未知 ID/tag、缺字段、非法元素、重复 ID、修复成功/失败；标签替换覆盖
  SQLite 中途失败回滚、保存 0、读失败、旧 active 残留和远端上传版本验证。
- 现有 weekly、Elsevier、代理、邮件多收件人和普通报告模式回归测试继续通过。
- 容器启动日志仍显示 `0 10 * * *`，并且不执行测试中的真实通知。
