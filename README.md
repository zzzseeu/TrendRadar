# TrendRadar 农业气象周报

TrendRadar 面向农业场景生成每周 PDF 周报。系统每天北京时间 10:00 静默采集；每周一以独立自然周为范围，生成上一自然周的周报。

English documentation: [README-EN.md](README-EN.md)。

## 周报内容与交付

周一先验证当期官方全国农业气象周报，再根据 `published_at` 严格筛选上一自然周的候选内容。AI 严格筛选最多 20 条，随后生成专用 A4 PDF。

企业微信通过 `upload_media` 上传 PDF，并且只发送一个文件消息。不会发送网页预览、摘要或其他文字消息。

```text
每天 10:00 静默采集 → 周一验证当期官方农业气象周报 →
上一自然周 published_at 唯一过滤 → 严格 AI（最多 20 条） →
专用 A4 PDF → 企业微信文件消息 → 周成功检查点
```

如果周一未取得当期官方农业气象周报，系统会在 10:30—12:00 的重试窗口继续尝试。周成功检查点用于确认 PDF 已生成并以文件形式交付。

## 配置与运行

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
