# Elsevier API 三次重试设计

## 目标

降低 Elsevier Article Retrieval API 偶发网络失败导致科研论文永久降级为摘要的概率。每篇论文优先使用 API；只有瞬时失败累计三次，或遇到明确不可重试的响应后，才进入现有 ScienceDirect 网页抓取与摘要/标题降级链。

## 重试规则

- 每篇论文 API 最多请求 3 次，包含首次请求。
- `timeout`、连接/传输异常、HTTP 429、HTTP 5xx 为可重试状态。
- 第 1 次可重试失败后等待 0.5 秒；第 2 次失败后等待 1 秒；第 3 次失败后停止 API 请求。
- HTTP 400、401、403、404 等其他 4xx，以及响应成功但明确无正文或 XML 无效，不进行重复请求，直接进入现有网页抓取链。
- 任意一次返回合法全文后立即结束重试，不访问 ScienceDirect 网页。
- 日志只记录 PII、尝试次数和失败状态，不输出 API Key 或机构 Token。

## 降级链

API 三次瞬时失败或遇到不可重试结果后，保持当前顺序：

1. 尝试读取 ScienceDirect HTML；
2. HTML 不可用时使用 RSS 摘要；
3. 摘要也不存在时仅使用标题。

## 改动范围

- `trendradar/crawler/article_content.py`：在正文协调层实现最多三次 API 尝试和退避，再决定是否进入网页链。
- `tests/test_elsevier_full_text.py`：覆盖前两次失败第三次成功、三次失败后网页降级、不可重试状态不重复请求。
- `trendradar/crawler/elsevier.py` 的单次请求职责保持不变。
- 不修改数据库结构、AI 分类、证据等级定义或 PDF 模板。

## 验收标准

- 前两次为瞬时失败、第三次成功时返回 `elsevier_full_text`，网页请求次数为 0。
- 三次瞬时失败后才请求 ScienceDirect 网页。
- 明确不可重试状态只调用 API 一次。
- 重试次数、退避间隔和日志内容可由测试确定验证。
- 现有 Elsevier 全文与正文降级测试继续通过。
