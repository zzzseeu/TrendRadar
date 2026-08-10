# 任务 6：企业微信严格 PDF-only 投递

## 实现内容

- 将企业微信 PDF 模块收敛为 PDF 上传和 `file` 消息发送；删除 Markdown
  预览、重点新闻收集、HTML 转 PDF 和文字回退逻辑。
- 新增 `NotificationDispatcher.dispatch_weekly_pdf()`：仅解析企业微信
  Webhook；每个账号只上传一次并发送一次 `file` 消息；任何账号失败即失败。
- 周报通知主链改为 `_deliver_weekly_pdf()`，不再调用 `dispatch_all()` 或
  普通企业微信 sender，因此不会向飞书、钉钉、邮件、Telegram、普通 Webhook
  或企业微信文字通道投递周报。
- PDF 缺失、无效、超过 20 MB、缺少 Webhook、上传失败或文件发送失败均返回失败，
  不写 push checkpoint；所有账号发送成功后才以自然周 `window.end` 写入。
- 专用交付助手也重新检查同周 push checkpoint，避免直接调用场景重复发送。
- 删除 `WEWORK_PDF_ENABLED`、`WEWORK_PDF_TOP_N`、`pdf_enabled`、`pdf_top_n`
  的 loader、YAML、Compose、示例环境变量和部署测试残留；保留普通非周报
  `WEWORK_MSG_TYPE` 行为。

## 测试

RED：新增测试在实现前因 `send_wework_pdf_file` 不存在而导入失败；原有 sender
测试同时展示了 Markdown 预览失败后回退分片文字的旧行为。

GREEN：执行以下命令通过 54 项测试：

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_wework_pdf tests.test_weekly_pdf_delivery \
  tests.test_weekly_schedule -v
```

另执行 `git diff --check`，并搜索确认代码、测试、配置与 Docker 文件均无旧的
预览函数和 PDF 开关标识残留。

## 变更范围

未改动 `docker/.env`、`uv.lock`、本地 `.venv` 或 `output`。

## 审查修复：weekly 失败传播

正式审查发现 `_execute_mode_strategy()` 曾仅在有普通新闻、通知总开关开启且
存在任意普通通知渠道时，才将 weekly 交付失败返回给 `run()`。这会让气象-only
周报在无企业微信 Webhook、PDF 上传或文件发送失败、PDF 缺失/无效，以及通知总
开关关闭时错误返回成功。

现已将 weekly 主线收敛为：当 `schedule.push` 为真且专用 PDF 交付返回失败时，
无条件返回 `False`。成功交付仍返回 `True` 并仅以 `window.end` 写入 checkpoint；
daily/current 分支未改变。

新增生产链 RED 测试覆盖：

- 气象-only 周报无企业微信 Webhook；
- 气象-only 周报企业微信文件投递失败；
- `ENABLE_NOTIFICATION=false` 的计划周报；
- 气象-only 周报文件投递成功并写入 `window.end` checkpoint。

修复前前三项均稳定失败（`run` 链末端错误返回 `True`）；修复后执行以下扩展
回归全部通过，共 77 项：

```bash
docker run --rm --network none --entrypoint /app/.venv/bin/python \
  -e PYTHONPATH=/workspace \
  -v /mnt/d/project/trendradar/.worktrees/previous-day-window:/workspace:ro \
  -w /tmp docker-trendradar \
  -m unittest tests.test_wework_pdf tests.test_weekly_pdf_delivery \
  tests.test_weekly_schedule tests.test_weekly_pdf_report \
  tests.test_weekly_report_output -v
```
