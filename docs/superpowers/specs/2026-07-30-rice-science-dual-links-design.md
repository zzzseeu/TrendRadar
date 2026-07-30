# Rice Science 双链接设计

## 背景

TrendRadar 当前通过 ScienceDirect RSS 获取 Rice Science 文章。2026 年 7 月 30 日的实际探测结果如下：

- RSS 地址可正常返回，定时任务成功获取 30 条记录。
- ScienceDirect 论文页在容器内直连和使用现有新闻代理时均返回 HTTP 403。
- Jina Reader 首次抽查返回 HTTP 401；重试虽返回 HTTP 200，但正文只是 ScienceDirect 的 `Are you a robot?` 反爬页，不包含目标论文标题、摘要或正文，因此不可作为备用阅读入口。
- 使用论文完整标题访问 Semantic Scholar 检索页时，容器直连返回 HTTP 200，响应包含目标论文标题。

因此，本次改动只改善推送后的阅读入口，不修改 RSS 抓取、存储、去重、AI 筛选或原文正文抓取逻辑。

## 目标

- Rice Science 条目同时保留 ScienceDirect 官方原文链接和备用检索链接。
- 企业微信消息和 HTML 报告均展示双链接。
- 默认点击标题仍进入官方原文，旁边提供明确标记的「🔎 备用检索」链接。
- 备用服务异常时不影响抓取、分析、报告生成和消息推送。
- 其他 RSS 来源的链接与展示保持不变。

## 非目标

- 不绕过付费墙或访问控制。
- 不将 Semantic Scholar 用作后台元数据或正文抓取器。
- 不替换数据库中保存的官方原文 URL。
- 不为全部 ScienceDirect 期刊启用备用链接。
- 不新增 Semantic Scholar API Key 或运行时 API 请求。

## 链接生成

新增一个无网络请求的纯函数，根据条目来源、官方 URL 和完整标题生成备用检索 URL。

只有同时满足以下条件时才生成备用链接：

1. 条目的来源 ID 为 `rice-science`。
2. URL 使用 `http` 或 `https` 协议。
3. 主机名为 `www.sciencedirect.com`。
4. 路径符合 `/science/article/pii/<PII>`。
5. 标题不是空字符串或纯空白。

生成规则：

```text
官方原文：
https://www.sciencedirect.com/science/article/pii/<PII>?dgcid=rss_sd_all

备用检索：
https://www.semanticscholar.org/search?q=<完整标题的 URL 编码>
```

ScienceDirect URL 只用于校验来源、规范主机、文章路径和 PII；RSS 跟踪参数不会进入备用链接。检索参数使用去除首尾空白后的完整标题并进行 URL 编码。任何校验失败均返回空字符串，由调用方只展示官方链接。

## 数据流

RSS 条目从存储转换为推送字典时，保留现有 `url` 字段，并新增可选的 `reader_url` 字段。该字段继续传递到：

- RSS 关键词统计结果；
- AI 筛选后的标签和重点新闻；
- 企业微信 Markdown 渲染；
- HTML 报告 RSS 区域。

`reader_url` 不参与条目标识、去重、新增判断、排序或 AI 提示词构建。

## 展示规则

### 企业微信

Rice Science 条目显示为：

```text
[文章标题](官方原文) [🔎 备用检索](reader_url)
```

没有 `reader_url` 时维持原有单链接格式。备用链接会增加消息长度，仍由现有分批逻辑处理。

### HTML 报告

文章标题继续链接官方原文，并在标题后显示「🔎 备用检索」。备用链接使用新标签页打开，并沿用现有 RSS 链接样式，不引入新的脚本。

## 隐私与依赖

- TrendRadar 后台不会请求 Semantic Scholar。
- 只有用户主动点击备用链接时，URL 编码后的论文标题才会发送给 Semantic Scholar。
- Semantic Scholar 是第三方服务，可能出现限流、检索缺失或服务不可用。
- 第三方故障不会触发自动回退请求，也不会影响官方链接。

## 测试与验收

自动化测试覆盖：

1. Rice Science 的标准 ScienceDirect PII 链接与非空标题生成正确的 Semantic Scholar 检索 URL。
2. 标题正确 URL 编码，空标题或纯空白标题不生成备用 URL。
3. RSS 跟踪参数不会进入备用 URL。
4. 非 Rice Science 来源不生成备用 URL。
5. 非 ScienceDirect、非 PII 路径和异常 URL 不生成备用 URL。
6. 企业微信 Markdown 同时包含官方和备用链接。
7. 没有备用链接时，企业微信输出与原行为一致。
8. HTML 报告正确转义并展示双链接，包括独立 RSS 区域的特殊字符 URL。
9. 其他来源的 HTML 输出不变。

验收标准：

- 现有单元测试全部通过。
- 新增测试按 TDD 流程先失败、实现后通过。
- 容器项目 `.venv` 中完成测试，不修改本地损坏的 `.venv`。
- 使用当前 Rice Science 文章完整标题抽查 Semantic Scholar 检索页时，响应包含目标论文标题。
