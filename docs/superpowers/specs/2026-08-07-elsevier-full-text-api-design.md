# Elsevier Institutional Token 全文接入设计

## 背景

当前正文增强流程只请求论文网页并解析 HTML。ScienceDirect 页面经常返回付费墙或反爬页面，因此即使 RSS 已发现论文，AI 仍可能只能依据摘要或标题判断。

现有 `ELSEVIER_API_KEY` 与新签发的 institutional token 已通过真实请求验证：二者同时作为请求头提交时，Article Retrieval API 能返回开放获取与非开放获取论文的结构化全文 XML。

## 目标

- 对带有 ScienceDirect PII 的论文链接优先调用 Article Retrieval API。
- 将获得的正文沿用现有内容增强流程，交给 AI 分类、摘要与证据校审。
- 凭据或 API 不可用时自动回退到现有网页、RSS 摘要、标题链路。
- institutional token 仅保存在服务端环境变量中，不进入 URL、日志、报告或 Git。

## 非目标

- 不下载或发送 PDF、图片和补充材料。
- 不永久保存原始全文 XML，也不新增全文数据库。
- 不扩大 RSS 时间窗口、修改 AI 筛选规则或改变推送频率。
- 不尝试绕过 Elsevier API 的机构授权范围。

## 方案

### 组件边界

新增独立的 Elsevier 全文客户端，负责：

1. 从 `https://www.sciencedirect.com/science/article/pii/<PII>` 链接提取 PII。
2. 使用固定 HTTPS 端点 `https://api.elsevier.com/content/article/pii/<PII>` 请求 `FULL` 视图。
3. 在请求头中发送 `X-ELS-APIKey`、`X-ELS-Insttoken` 和 `Accept: text/xml`。
4. 从 XML 的文章正文节点按原顺序提取纯文本段落。
5. 返回结构化成功、不可用或失败结果，不向调用方暴露凭据。

现有 `ArticleContentFetcher` 继续负责内容优先级和安全降级。它只在 URL 可识别为 ScienceDirect PII 且两项凭据均存在时调用 Elsevier 客户端。API 返回有效正文后，直接生成 `full_text` 内容；否则继续执行原有 HTML 抓取。

### 数据流

```text
RSS 条目
  → ArticleContentFetcher
  → 识别 ScienceDirect PII
  → Elsevier Article Retrieval API（直连）
  → XML 正文解析
  → 按 max_content_chars 截断
  → AI 分类、摘要和证据校审

API 不可用
  → 原有网页 HTML
  → RSS 摘要
  → 标题
```

## 配置与凭据

新增两个环境变量：

- `ELSEVIER_API_KEY`：现有开发者 API Key。
- `ELSEVIER_INST_TOKEN`：Elsevier 签发的 institutional token。

`docker/.env` 保存本机真实值，该文件已被 Git 忽略。`docker/.env.example` 仅增加空变量示例。Docker Compose 将两项变量注入 `trendradar` 容器；配置加载器把它们放入 `AI_FILTER.CONTENT_ENRICHMENT`，再显式传给正文抓取器，避免正文组件隐式读取全局环境。

缺少任一变量时，Elsevier API 功能视为未启用，现有行为保持不变。

## 网络与安全

- Elsevier API 使用独立的 `requests.Session`，设置 `trust_env = False`，确保不读取 HTTP(S) 代理环境变量。
- 不使用新闻抓取代理进行失败重试，以满足 Elsevier 邮件中的直连要求。
- 凭据只放在请求头，禁止使用 `apiKey` 或 `insttoken` 查询参数。
- 日志最多记录 PII、HTTP 状态和降级原因，不记录请求头、响应正文或凭据。
- 原始 XML 只在内存中处理；传给 AI 的文本继续受现有 `max_content_chars` 限制。

## 正文判定与降级

满足以下条件才将 API 结果标记为 `full_text`：

- HTTP 状态为 200。
- 响应是可解析的 XML。
- XML 包含文章正文区域。
- 清洗后的正文长度达到现有 `min_body_chars`。

认证失败、无授权、记录不存在、请求超时、XML 异常或仅返回元数据时，不终止任务，也不把元数据误标为全文，而是继续尝试网页 HTML。网页仍不可用时，再按现有顺序回退到 RSS 摘要和标题。

成功通过 API 获取的内容使用 `fetch_status = "elsevier_full_text"`。风险提示说明正文来自 Elsevier API，若发生字符截断则沿用现有截断警告。

## 测试

自动化测试覆盖：

- 只接受合法 ScienceDirect PII 链接，拒绝其他域名和异常 PII。
- 请求使用正确端点、`FULL` 视图和三个请求头。
- API 会话不读取环境代理，也不回退到代理。
- XML 正文按顺序解析，并避免父子段落重复。
- 非 200、超时、无正文和损坏 XML 均返回不可用结果。
- API 成功时 `ArticleContentFetcher` 优先采用全文。
- 凭据缺失或 API 失败时继续原有 HTML、摘要和标题降级。
- 配置加载器与 Docker Compose 正确传递两项环境变量，示例文件不含真实凭据。

完成单元测试后，使用容器内真实凭据请求至少一篇开放获取论文和一篇非开放获取论文，只检查状态、正文节点和文本长度，不输出论文全文或凭据。

## 验收标准

- ScienceDirect 非开放获取测试论文能在容器内通过 API 获得正文，并进入 `full_text` 内容层级。
- 关闭或移除 institutional token 后，任务仍能正常运行并回退到旧流程。
- 任何日志、Git 跟踪文件和生成报告中均不出现真实 API Key 或 institutional token。
- 既有自动化测试与新增测试全部通过。

