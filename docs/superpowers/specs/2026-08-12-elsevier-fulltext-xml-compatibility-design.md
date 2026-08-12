# Elsevier 全文 XML 兼容设计

## 目标

让现有 Elsevier Article Retrieval API 客户端兼容官方 XSD 中实际出现的期刊正文结构，避免 Rice Science 等文章在 API 已返回全文时错误降级到 RSS 摘要或标题。

## 范围

- 保留现有 `article/body/para` 提取路径。
- 接受 `simple-article`、`converted-article` 下的唯一正文 `body`。
- 当 `xocs:doc` 没有可用正文 `body` 时，读取其非空 `rawtext`。
- 继续忽略 `originalText` 外的元数据伪正文。
- 多个候选正文、空正文、坏 XML 继续失败关闭并进入现有 HTML/RSS/标题降级链。
- 不改 API 凭据、请求参数、AI 分类、周报选择或 PDF 逻辑。

## 提取优先级

1. 在唯一 `originalText` 内定位唯一的 `article`、`simple-article` 或 `converted-article` 正文 `body`。
2. 从正文中按文档顺序提取最外层 `para` 和 `simple-para`，避免嵌套段落重复。
3. 如果没有可用 `body`，仅从唯一 `xocs:doc` 的直接子元素 `rawtext` 读取并规范化空白。
4. 若结构含糊、正文为空或 XML 无效，返回空字符串，由既有调用链降级。

## 验证

- 脱敏 fixture 覆盖 Rice Science `doc/rawtext`。
- fixture 覆盖 `simple-article/body` 和 `converted-article/body`。
- 保留标准 `article/body/para`、元数据隔离、嵌套段落去重和多正文失败关闭回归。
- 运行 Elsevier 全文及文章内容集成测试，并用真实 Rice Science PII 做一次只输出状态和长度的验证。
