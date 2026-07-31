# 临时停用 ScienceDirect RSS 源设计

## 背景

Elsevier Article Retrieval API 当前对现有 API Key 返回 HTTP 403，相关权限仍在等待 Elsevier 支持确认。在此期间，TrendRadar 暂停监控所有通过 ScienceDirect RSS 提供内容的期刊，避免继续抓取无法稳定访问原文的条目。

## 目标

- 暂停以下 4 个 ScienceDirect RSS 源：
  - `molecular-plant`
  - `plant-communications`
  - `rice-science`
  - `crop-journal`
- 保持 Nature、Science、bioRxiv、IRRI 及其他现有来源继续运行。
- 保留现有 ScienceDirect 配置、双链接代码和 Elsevier API 设置，以便收到权限确认后快速恢复。
- 让正在运行的 Docker 服务在重启后应用新配置，且不触发立即推送。

## 方案

在 `config/config.yaml` 中为上述 4 个 RSS 源分别增加：

```yaml
enabled: false
```

现有 RSS 加载流程已经支持该字段，并在构建抓取列表时排除禁用源。因此不需要修改抓取器、存储、AI 分析、报告或通知代码。

不删除源配置，也不增加域名级黑名单。这样可以保留每个源的 ID、名称和 URL；后续恢复时只需删除 `enabled: false` 或改为 `true`。

## 数据流

1. 服务加载 `config/config.yaml`。
2. RSS 配置加载器读取所有源。
3. 主流程只把 `enabled` 不为 `false` 的源加入抓取列表。
4. ScienceDirect 的 4 个源不会产生网络请求、入库数据、AI 分析内容或新推送。
5. 其他启用源继续经过现有抓取、过滤、分析和推送流程。

历史数据库和已有 HTML 报告不做删除；本次变更只影响后续抓取。

## 验证

由于本次只修改配置文件，不新增生产代码或测试接口，使用配置级验证：

1. 通过项目虚拟环境加载 `config/config.yaml`，确认 4 个目标源均为禁用状态。
2. 确认仍存在启用的非 ScienceDirect 来源。
3. 检查配置中不存在其他仍启用的 `rss.sciencedirect.com` URL。
4. 重启 Docker 服务；`IMMEDIATE_RUN=false`，重启过程不执行即时推送。
5. 检查容器状态和启动日志，确认定时服务正常运行。

## 成功标准

- 后续定时任务不再请求 `rss.sciencedirect.com`。
- 推送中不再出现新抓取的上述 4 个期刊条目。
- 其他来源的抓取和定时任务保持启用。
- ScienceDirect 配置可通过单行开关恢复。
