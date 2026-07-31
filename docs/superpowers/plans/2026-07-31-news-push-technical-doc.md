# 新闻推送技术展示文档实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 创建一份 1～2 页的中文技术文档，简要展示新闻采集、AI 总结、企业微信推送及新增监控网站的方法。

**架构：** 文档以一张 Mermaid 流程图串联现有执行链路，再分别说明内容总结和网站扩展。所有表述均来自当前代码与配置，不写工具介绍、不展开运维细节、不包含真实密钥。

**技术栈：** Markdown、Mermaid、YAML 配置示例

---

## 文件结构

- 创建：`docs/news-push-technical-implementation.md`
  项目汇报使用的新闻推送技术实现说明。

### 任务 1：编写并验证技术展示文档

**文件：**

- 创建：`docs/news-push-technical-implementation.md`

- [ ] **步骤 1：确认目标文件尚未存在**

运行：

```bash
test ! -e docs/news-push-technical-implementation.md
```

预期：退出状态为 0，避免覆盖现有文档。

- [ ] **步骤 2：编写精简文档**

文档固定包含以下 4 个二级标题：

```markdown
# 新闻监控、内容总结与自动推送技术实现

## 推送流程

## 内容总结

## 推送内容

## 增加监控网站
```

具体要求：

- 「推送流程」包含 1 张 Mermaid 流程图，节点为定时任务、新闻采集、正文提取、AI 筛选与总结、HTML 报告、企业微信。
- 「内容总结」说明正文、摘要、标题三级降级，相关度筛选、育种价值排序、逐条摘要和二次证据校审。
- 「推送内容」只说明简短摘要、前 5 条重点新闻、原文链接，以及 HTML 报告留档。
- 「增加监控网站」分别提供：
  - 一个 `rss.feeds` 的 RSS 配置示例；
  - 一个 `source_type: web_news` 配置示例；
  - 特殊 JSON 接口需要修改 `trendradar/crawler/rss/web_news.py` 和 `trendradar/crawler/rss/fetcher.py` 的说明。
- 全文控制在 120 行以内。
- 不出现真实 API Key、Webhook、代理地址或内部模型网关地址。
- 不介绍 TrendRadar 的背景、功能列表或安装方法。

- [ ] **步骤 3：核对当前实现依据**

运行：

```bash
rg -n "source_type.*web_news|source_type.*corteva_news" config/config.yaml trendradar/crawler/rss/fetcher.py
rg -n "full_text|summary|title_only|risk_warning" trendradar/crawler/article_content.py
rg -n "highlight_top_n|summary_grounding_review_enabled" config/config.yaml trendradar/ai
```

预期：能够定位网页来源分支、三级证据降级、前 5 条重点新闻和摘要校审配置。

- [ ] **步骤 4：验证结构、篇幅和敏感信息**

运行：

```bash
rg -n "^# |^## " docs/news-push-technical-implementation.md
wc -l docs/news-push-technical-implementation.md
rg -n "```mermaid|config/config.yaml|trendradar/crawler/rss/web_news.py|trendradar/crawler/rss/fetcher.py" docs/news-push-technical-implementation.md
rg -n -i "api[_ -]?key[[:space:]]*[:=]|webhook.*https?://|hnaicc|host.docker.internal|7892" docs/news-push-technical-implementation.md
git diff --check -- docs/news-push-technical-implementation.md
```

预期：

- 标题结构与步骤 2 完全一致。
- 文件不超过 120 行。
- Mermaid、配置路径和两个扩展模块路径均存在。
- 敏感信息扫描无输出。
- `git diff --check` 退出状态为 0。

- [ ] **步骤 5：提交文档**

```bash
git add docs/news-push-technical-implementation.md
git commit -m "docs(推送): 添加新闻推送技术实现说明"
```

预期：提交只包含目标文档。
