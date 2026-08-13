# `--force-weekly` 人工重跑实现计划

## 任务 1：建立失败契约

- 新增聚焦测试，证明旧实现会被已有 push/analyze/逐账号账本短路。
- 新增自动调度静态测试，禁止自动 cron 使用 `--force-weekly`。
- 保留普通周报成功后跳过和部分账号续投的回归断言。

## 任务 2：实现人工强制重跑

- 在 `NewsAnalyzer.run()` 中让人工强制周报越过已完成 push 检查。
- 禁止人工强制周报复用旧 PDF 的 partial-resume 路径。
- 在 `_run_ai_analysis()` 中让人工强制周报越过 analyze 检查点。
- 在 `_deliver_weekly_pdf()` 中让人工强制周报向全部账号发送，不读取或写入逐账号续投账本。
- 保留周锁、上一自然周窗口、PDF 校验和全局成功记录。

## 任务 3：清理自动入口与文案

- 从 `config/daily.crontab` 的周一自动重试中移除 `--force-weekly`。
- 更新 CLI 帮助，明确人工强制会重新采集、生成和发送。

## 任务 4：验证

- 使用项目 `.venv/bin/python` 运行新增测试及周报兼容/投递相关测试。
- 运行 `git diff --check`。
- 检查工作树，确保没有纳入用户现有的无关修改和运行缓存。

