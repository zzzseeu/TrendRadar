# TrendRadar Agricultural Weather Weekly Report

TrendRadar creates a weekly PDF report for agricultural use. It collects data silently every day at 10:00 Beijing time, then produces a report for the previous natural week each Monday.

中文说明：[README.md](README.md)。

## Weekly report and delivery

On Monday, the system first verifies the current official national agricultural meteorological weekly report. It then uses `published_at` as the sole eligibility filter for content from the previous natural week. Strict AI selection keeps no more than 20 items and produces a dedicated A4 PDF.

The PDF is uploaded to WeCom with `upload_media`, and WeCom receives exactly one file message. No web preview, summary, or other text message is sent.

```text
Daily 10:00 silent collection → Monday official agricultural weather report verification →
previous natural-week published_at-only filtering → strict AI (up to 20 items) →
dedicated A4 PDF → WeCom file message → weekly success checkpoint
```

If the current official agricultural weather report is unavailable on Monday, the system retries between 10:30 and 12:00. The weekly success checkpoint confirms that the PDF was generated and delivered as a file.

## Configuration and operation

Configure RSS sources, AI credentials, and WeCom credentials in `config/config.yaml`. An RSS source only needs an identifier, a name, and a URL:

```yaml
rss:
  feeds:
    - id: "example-rss"
      name: "Example RSS feed"
      url: "https://example.org/news/feed.xml"
```

The schedule is defined in `config/daily.crontab`. Run the project with its virtual environment:

```bash
.venv/bin/python -m trendradar
```

Container deployment and environment examples are in `docker/`.
