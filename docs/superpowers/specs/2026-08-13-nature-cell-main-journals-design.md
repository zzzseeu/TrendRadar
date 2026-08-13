# Nature 与 Cell 正刊监控设计

## 目标

在不改变现有抓取、筛选和周报规则的前提下，将 Nature 正刊与 Cell 正刊加入固定学术来源。

## 设计

- Nature 使用官方 RSS：`https://www.nature.com/nature.rss`。
- Cell 使用官方 RSS：`https://www.cell.com/cell/current.rss`。
- 两个来源均设置 `content_category: scholarly`，因此进入“科研进展”候选。
- 中英文配置保持一致。
- 学术来源名称识别规则同时加入 Nature 正刊；Cell 已在规则中。
- 继续复用现有 RSS 解析器、自然周范围、AI 相关性阈值和论文去重逻辑。

## 验收

- 配置中存在两个来源，ID 唯一、URL 正确、类别为 `scholarly`。
- Nature 与 Cell 名称能够被来源证据规则识别为科研进展。
- 相关配置与来源证据测试通过。
