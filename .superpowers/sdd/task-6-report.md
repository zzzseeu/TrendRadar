# 任务 6 实施报告

## 结果

- weekly 主流程不再调用 `ctx.generate_html()`；即使 HTML storage 开启，也只生成三模块专用 PDF，不写 `output/html/latest/weekly.html`、通用 `output/index.html` 或根 `index.html`。`AppContext.generate_html(mode="weekly")` 同时 fail-closed，普通模式的 HTML 能力保持不变。
- 严格 AI 分类只产生一个 `WeeklyNewsSelection`；叙事输入的条目与 PDF renderer 的政策/科研条目保持对象身份一致，没有第二次选择或旧单列表回退。
- 政策或科研任一模块为空仍可生成；双新闻模块为空且气象有效仍可生成；政策、科研、气象三类内容均空时严格失败。
- weekly 的 `analyze` checkpoint 从 AI 叙事成功时延后到专用 PDF 成功落盘后。分类、叙事、PDF 任一失败不写 `analyze`/`push`；`analyze` checkpoint 存储失败时在投递前终止。
- 企业微信继续只收到一个 `file` 消息。首轮零账号成功会重新分类、重新建立 selection 并重建 PDF；部分账号成功后只续投失败账号；全部账号已发送而 global checkpoint 失败时零外呼补写 checkpoint。
- 同周锁覆盖 checkpoint 检查、气象获取、采集、分析、PDF 生成和投递完整事务，并在失败路径释放。

## TDD 与验证

- RED：主链新增测试精确出现 4 个失败：weekly 仍调用通用 HTML 并返回 HTML 路径；三空失败前仍调用通用 HTML；`AppContext` 仍允许 weekly HTML；PDF 失败前已写入 `analyze`。顺序测试观察到 `analyze -> PDF`，期望为 `PDF -> analyze`。
- GREEN：无网络验证镜像中 brief 指定的 `test_weekly_schedule`、`test_weekly_pdf_delivery`、`test_weekly_three_module` 共 51 项全部通过（3.569 秒）。
- 扩展验证：`test_weekly_pdf_report` 25 项全部通过（21.201 秒），覆盖真实 Chromium PDF、多页 A4、中文提取、原子生成与回滚。
- 验证镜像原入口会先检查配置并退出，因此保持同一镜像、`--network none` 和 `/app/.venv/bin/python`，显式覆盖 entrypoint 执行测试。
- `git diff --check` 通过；未修改 `output`、`.env`、服务配置或依赖。

## 删除重复测试的理由

- 删除整个 `tests/test_weekly_report_output.py`。其中 weekly 通用 HTML、飞书/钉钉文本渲染断言与“weekly 唯一 WeCom PDF 主线”冲突，不再代表可达生产行为。
- 该文件唯一仍有效的显式 `report_type="上周周报"` 断言迁移到 `test_weekly_pdf_delivery.py`；普通模式 HTML 仍可生成的回归也在 delivery 测试中保留。
- `test_weekly_schedule.py` 从重复的逐账号/全局账本 mock 状态机收敛为 run 返回值、跨平台周锁、气象先验、成功 checkpoint 前置跳过和完整事务边界。partial/global/rebuild 状态机集中在 `test_weekly_pdf_delivery.py`。
- 严格分类、叙事、storage checkpoint、PDF 失败测试均通过真实 `run -> strategy -> pipeline` 编排执行；没有用同一个 mock 仅替换错误字符串来冒充四个阶段。

## 自审

- 主链测试通过真实 `run -> _execute_mode_strategy -> _run_analysis_pipeline -> PDF -> delivery`，只替换 AI/PDF 引擎/网络边界，并在临时目录检查三个禁止 HTML/index 路径均不存在。
- selection 复用测试使用对象身份断言；零账号成功重试测试观察到两次不同 selection、两次 renderer 和两个不同 PDF 路径，不只统计 pipeline mock。
- partial retry 断言发送顺序为 A、B、B；resume 测试断言复用原 PDF 字节/digest 且 weather/crawl/renderer/builder 均不调用；global checkpoint retry 断言外呼总数不增加。
- 独立审查最初提出三个 Important 测试证据问题（主链拆段、伪阶段 mock、重建未观察 build），均已按上述真实 run 测试修正；生产代码未发现 Critical 或明确功能回归。

## 独立审查后续修复

- 权威周快照没有新闻 ID 时，主链直接保留空 `WeeklyNewsSelection`，不调用新闻 AI filter、标签提取、分类转换或相关存储；官方气象叙事、PDF 和 push 仍继续执行。
- weekly 已存在 `analyze` 但 global `push` 尚未成功时，无论 `once_push` 是否开启都允许重新分类、叙事和构建 PDF。partial account ledger 的已有 PDF 续投仍在天气、抓取和分析之前短路。
- 恢复独立兼容回归，覆盖 daily 的 once-analyze 跳过语义、`run()==False` 时 CLI 退出码 1、`--force-weekly` 透传，以及 scheduler preset/force 解析；未恢复重复的逐账号账本 mock。
- 后续定向验证共 8 项通过（0.027 秒）；最终 brief 三模块集合 51/51 通过（4.084 秒），恢复兼容项 7/7 通过（0.009 秒）。

## 外部副作用边界

- 企业微信文件发送成功与账号 delivery ledger 写入是两个无法组成原子事务的外部操作。如果进程在外呼成功后、账本成功写入前崩溃，或账本写入失败，下一次重试可能再次向该账号发送同一 PDF。
- 因此逐账号投递语义是 **at-least-once**，不能宣称绝对 exactly-once。账本只保证已成功持久化的账号在后续重试中被跳过；系统无法从企业微信侧反向确认“发送成功但本地未记账”的窗口。
