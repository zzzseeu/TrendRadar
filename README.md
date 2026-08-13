# TrendRadar 农业育种周报

TrendRadar 面向农业场景生成每周 PDF 周报。系统在周二至周日每天北京时间 10:00 静默采集；每周一汇总上一自然周的数据并生成周报。

English documentation: [README-EN.md](README-EN.md)。

## 周报内容与交付

周一以 `published_at` 严格筛选上一自然周的候选内容，并使用全局 `0.5` 分数阈值。周报固定为三个模块：

- 时事动态最多 20 条：除有明确论文证据的内容外，其余水稻相关官方发布、产业消息和在线搜索结果归入此模块。
- 科研进展最多 20 条：学术期刊、预印本，以及明确给出期刊名或完整论文题名的机构报道归入此模块；水稻优先，其他作物研究可作借鉴。
- 全国农业气象周报独立于新闻名额；周一验证当期官方周报后纳入该模块。

企业微信通过 `upload_media` 上传并仅发送一个 PDF 文件。

```text
周二至周日每天 10:00 静默采集 → 周一汇总上一自然周 →
时事动态 20 + 科研进展 20 + 全国农业气象周报 →
一个 PDF → 企业微信文件消息
```

如果周一未取得当期官方农业气象周报，系统会在 10:30—12:00 的重试窗口继续尝试。周成功检查点用于确认 PDF 已生成并以文件形式交付。

固定来源采用现有 RSS、网页列表和共享官方文档解析策略。某些来源暂时不可访问时，系统继续使用其余可用来源生成周报，并在 PDF 中列出缺失日期与失败来源；只有整个统计窗口没有任何可用来源证据时才停止生成。

## 配置与运行

首次部署、密钥配置、定时运行、项目更新和故障排查，请参阅 [项目接手与运维指南](docs/project-handover-guide.md)。

使用 `config/config.yaml` 配置 RSS 来源、AI 凭据和企业微信凭据。RSS 来源仅需标识、名称和 URL，例如：

```yaml
rss:
  feeds:
    - id: "example-rss"
      name: "示例 RSS 新闻源"
      url: "https://example.org/news/feed.xml"
```

定时任务定义在 `config/daily.crontab`。项目虚拟环境中的运行命令为：

```bash
.venv/bin/python -m trendradar
```

容器部署与环境变量示例见 `docker/` 目录。

### 非 Docker 的 PDF 工具

非 Docker 部署需要安装 Poppler，并让 `pdfinfo` 与 `pdftotext` 可执行。若它们不在 `PATH` 中，请设置 `PDFINFO_BIN` 和 `PDFTOTEXT_BIN` 为对应的绝对路径。Windows PowerShell 示例：

```powershell
$env:PDFINFO_BIN = 'C:\poppler\Library\bin\pdfinfo.exe'
$env:PDFTOTEXT_BIN = 'C:\poppler\Library\bin\pdftotext.exe'
```

Docker 镜像已自带 Poppler，无需设置这两个变量。

## 兼容功能入口

专用周报链路不会移除 TrendRadar 原有的普通运行能力：

- `current` 当前榜单和 `daily` 当日汇总模式仍由 [config/config.yaml](config/config.yaml) 的 `report.mode` 配置。
- 完整安装与部署入口见 [项目文档](docs/index.html) 和 [Docker 目录](docker/)。
- MCP 客户端与工具用法见 [MCP 常见问题](README-MCP-FAQ.md)。
- 普通模式的多渠道通知仍使用 [config/config.yaml](config/config.yaml) 的 `notification` 配置；本页的 PDF-only 规则只适用于专用周报交付。
