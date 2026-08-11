# 任务 5 实施报告

## 结果

- PDF 模板改为政策、科研、农业气象三个一级模块；政策和科研均严格按 `module_rank` 单列表展示，每条只渲染一次，前五条分别内联“重点政策”或“重点文献”。
- 删除 `flatten_unique_news()`、独立重点区、独立入选区和 topic 二次分组；保留 URL 安全校验、HTML 转义、A4 页眉页脚及长气象分页样式。
- 政策、科研空态使用需求指定文案；任务 4 的 `policy_trends`、`research_trends`、`weather_risks` 分别进入对应模块，封面和指标分别统计政策与科研数量。
- 正式 stem 更新为 `农业育种新闻周报_三模块_<开始日>至<结束日>`；主流程的 partial ledger resume 继续通过 `weekly_pdf_output_path()` 命中新路径，先复用既有 PDF digest，不进入渲染或生成。
- Chromium 先输出到同目录唯一临时文件；通过 `%PDF`、20MB、`pdfinfo` A4/页数和 `pdftotext` 中文校验后才替换正式件。底层 PDF 生成同样不再预删目标文件。
- 双正式件替换前创建同目录恢复副本；第二次 `os.replace()` 失败会恢复旧 HTML，旧 PDF 保持不变，最后清理本轮临时文件与恢复副本。

## TDD 与验证

- RED：三组 43 项测试中出现 6 个失败、2 个错误，均对应旧重复版块/marker/空态/stem、缺少全局唯一校验和原子验证函数；新增第二次 replace 失败回滚测试单独 RED。
- GREEN：最终无网络验证镜像中三组 43 项全部通过（30.764 秒），包含真实 Chromium 40 条多页 A4/中文/页眉页脚、长气象、20MB 底层拒绝和逐账号账本复用。
- 验证镜像原入口会先检查配置并退出，因此在保持同一镜像、无网络和 `/app/.venv/bin/python` 的前提下显式覆盖 entrypoint 执行测试。
- `git diff --check` 通过；生产 Dockerfile 已包含 Chromium、CJK 字体和 `poppler-utils`。

## 自审

- renderer 会拒绝任一模块超过 20 条或跨模块/模块内身份重复的数据；策略与科研合计自然不超过 40 条。
- partial ledger 测试断言原 PDF 字节与 digest 不变，并断言 `render_weekly_pdf_html()`、`build_weekly_pdf()` 均未调用。
- 生成失败覆盖 Chromium 异常、PDF header 非法、超过 20MB；三种场景均断言两份旧正式件字节不变、临时件清空。
- 成功路径覆盖两份正式件同时更新；第二次 replace 异常覆盖恢复路径真实执行，而不是让同一个 mock 持续阻断恢复。
- 独立代码审查未发现 Critical 或 Important 实现缺陷；审查确认各项功能与任务简报对齐。

## 疑虑

- 两个文件无法获得文件系统级跨文件事务；当前方案保证所有可捕获异常下回滚一致，且恢复副本能覆盖第二次 replace 失败。进程在两个原子 replace 之间被强制终止仍存在极短的不一致窗口，这是双文件顺序替换的固有限制。
- `pdfinfo` 和 `pdftotext` 现在是正式生成前的强校验依赖；项目生产 Dockerfile 已安装 `poppler-utils`，非 Docker 部署也需要提供这两个命令。
