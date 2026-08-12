# TrendRadar Agricultural Breeding Weekly Report

TrendRadar produces a weekly PDF report for agriculture. It collects data silently at 10:00 Beijing time from Tuesday through Sunday, then aggregates the previous natural week every Monday.

中文说明：[README.md](README.md)。

## Weekly report and delivery

On Monday, `published_at` is the strict eligibility filter for content from the previous natural week, with one global score threshold of `0.5`. The report has four independent modules:

- Rice breeding policy: up to 20 rice-specific items.
- Rice industry current affairs: up to 20 substantive rice-industry items.
- Breeding research: up to 20 items, prioritizing rice while allowing transferable research from other crops.
- Agricultural weather is independent of the news quotas; the current official national agricultural meteorological weekly report is verified on Monday and included in this module.

The PDF is uploaded with WeCom `upload_media`, and WeCom receives one PDF file only.

```text
Tuesday through Sunday: silent collection at 10:00 → Monday: aggregate the previous natural week →
Rice policy 20 + Rice industry 20 + Rice-first research 20 + independent agricultural weather →
one PDF → WeCom file message
```

If the current official agricultural weather report is unavailable on Monday, the system retries between 10:30 and 12:00. The weekly success checkpoint confirms that the PDF was generated and delivered as a file.

Fixed sources reuse the existing RSS, web-list, and shared official-document parsers. If some sources are temporarily unavailable, the report is still produced from available sources and the PDF lists missing dates and failed source IDs. Generation stops only when the whole reporting window has no usable source evidence.

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

### PDF tools outside Docker

For a non-Docker deployment, install Poppler and make `pdfinfo` and `pdftotext` executable. If they are not on `PATH`, set `PDFINFO_BIN` and `PDFTOTEXT_BIN` to their absolute paths. Windows PowerShell example:

```powershell
$env:PDFINFO_BIN = 'C:\poppler\Library\bin\pdfinfo.exe'
$env:PDFTOTEXT_BIN = 'C:\poppler\Library\bin\pdftotext.exe'
```

The Docker image already includes Poppler, so these variables are unnecessary there.

## Compatibility entry points

The dedicated weekly path does not remove TrendRadar's ordinary runtime features:

- The `current` ranking and `daily` summary modes remain available through `report.mode` in [config/config.en.yaml](config/config.en.yaml).
- For installation and deployment, see the [project documentation](docs/index.html) and the [Docker directory](docker/).
- MCP client and tool guidance remains in the [MCP FAQ](README-MCP-FAQ-EN.md).
- Multi-channel ordinary-mode notifications remain configured under `notification` in [config/config.en.yaml](config/config.en.yaml); the PDF-only rule on this page applies only to dedicated weekly delivery.
