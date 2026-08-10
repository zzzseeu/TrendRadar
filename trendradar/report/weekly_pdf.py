# coding=utf-8
"""A dedicated, self-contained printable template for agricultural weekly reports."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from trendradar.crawler.news_search import canonicalize_url, normalize_title
from trendradar.report.pdf import generate_pdf_from_html


def flatten_unique_news(groups: list[dict]) -> list[dict]:
    """Return stable, de-duplicated selected news from weekly topic groups."""
    ordered = []
    seen = set()
    for group in groups:
        for item in group.get("titles", []):
            key = canonicalize_url(item.get("url", ""))
            if not key:
                key = normalize_title(item.get("title", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(item)
    return sorted(
        ordered,
        key=lambda item: (
            _highlight_sort_value(item.get("highlight_rank")),
            str(item.get("published_at") or ""),
            str(item.get("title") or ""),
        ),
    )


def _highlight_sort_value(value: Any) -> int:
    try:
        return int(value or 10**9)
    except (TypeError, ValueError):
        return 10**9


def _text(value: Any) -> str:
    """Escape all text supplied by feeds, AI, or remote official reports."""
    return escape(str(value or ""), quote=True)


def _safe_http_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return raw


def _link(url: Any, label: str) -> str:
    safe_url = _safe_http_url(url)
    if not safe_url:
        return ""
    return f'<a href="{_text(safe_url)}">{_text(label)}</a>'


def _news_topic(item: dict) -> str:
    topics = item.get("weekly_topics") or item.get("tags") or []
    if isinstance(topics, str):
        topics = [topics]
    for topic in topics:
        normalized = str(topic or "").strip()
        if normalized:
            return normalized
    return "其他"


def _render_news_card(item: dict, *, show_marker: bool = False) -> str:
    title = _text(item.get("title")) or "（无标题）"
    summary = _text(item.get("ai_summary") or item.get("summary"))
    source = _text(item.get("source_name") or item.get("feed_name") or "未标注来源")
    published_at = _text(item.get("published_at") or item.get("time_display") or "未标注时间")
    marker = ""
    if show_marker:
        marker = '<span class="highlight-marker">重点标记</span>'
    links = [
        _link(item.get("url"), "原文链接"),
        _link(item.get("reader_url"), "备用链接"),
    ]
    links = [link for link in links if link]
    summary_html = (
        f'<p class="summary">{summary}</p>' if summary else ""
    )
    links_html = f'<p class="links">{"　".join(links)}</p>' if links else ""
    return (
        '<article class="news-card">'
        f'<h3>{marker}{title}</h3>'
        f'<p class="meta">来源：{source}　发布时间：{published_at}</p>'
        f'{summary_html}{links_html}'
        '</article>'
    )


def _analysis_text(ai_analysis: Any, attribute: str, fallback: str) -> str:
    if ai_analysis is None:
        return fallback
    return _text(getattr(ai_analysis, attribute, "") or fallback)


def _format_collection(value: Any) -> str:
    if isinstance(value, str):
        values = [value]
    else:
        values = list(value or ())
    return "、".join(_text(item) for item in values if str(item or "").strip()) or "暂未列出"


def render_weekly_pdf_html(
    *,
    news_items: list[dict],
    ai_analysis: Any,
    agro_weather: Any,
    period_label: str,
    generated_at: datetime,
) -> str:
    """Render only the selected weekly news into an offline A4 HTML document."""
    selected = list(news_items or [])
    if len(selected) > 20:
        raise ValueError("周报 PDF 只接受最多 20 条已入选新闻")

    selected = sorted(
        selected,
        key=lambda item: (
            _highlight_sort_value(item.get("highlight_rank")),
            str(item.get("published_at") or ""),
            str(item.get("title") or ""),
        ),
    )
    highlights = [
        item for item in selected
        if _highlight_sort_value(item.get("highlight_rank")) <= 5
    ][:5]
    topic_groups: OrderedDict[str, list[dict]] = OrderedDict()
    for item in selected:
        topic_groups.setdefault(_news_topic(item), []).append(item)

    generated = generated_at.strftime("%Y-%m-%d %H:%M")
    weather_html = _render_weather(agro_weather)
    topic_html = "".join(
        f'<section class="topic"><h3>{_text(topic)}</h3>'
        f'{"".join(_render_news_card(item) for item in items)}</section>'
        for topic, items in topic_groups.items()
    ) or '<p class="empty">本期没有入选普通新闻。</p>'
    highlight_html = "".join(
        _render_news_card(item, show_marker=True) for item in highlights
    ) or '<p class="empty">本期没有重点新闻。</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>农业育种新闻周报</title>
<style>
@page {{
  size: A4;
  margin: 18mm 14mm 18mm;
  @bottom-center {{ content: "第 " counter(page) " 页"; }}
}}
* {{ box-sizing: border-box; }}
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; color: #172554; font-size: 10.5pt; line-height: 1.58; }}
.page-header {{ position: fixed; top: -14mm; left: 0; right: 0; color: #475569; font-size: 8.5pt; border-bottom: 1px solid #cbd5e1; padding-bottom: 2mm; }}
.cover {{ border-left: 6px solid #0f766e; padding: 6mm 7mm; background: #f0fdfa; margin-bottom: 6mm; }}
h1 {{ margin: 0 0 3mm; color: #134e4a; font-size: 25pt; }}
h2 {{ color: #0f766e; font-size: 15pt; border-bottom: 1px solid #99f6e4; padding-bottom: 2mm; margin-top: 8mm; }}
h3 {{ font-size: 11.5pt; margin: 0 0 2mm; color: #164e63; }}
.meta {{ color: #475569; margin: 0 0 2mm; font-size: 9pt; }}
.summary {{ margin: 2mm 0; }}
.news-card {{ break-inside: avoid-page; border: 1px solid #dbeafe; border-radius: 2mm; padding: 3mm 4mm; margin: 3mm 0; background: #fff; }}
.highlight-marker {{ display: inline-block; color: #fff; background: #0f766e; padding: 0 1.6mm; border-radius: 1mm; font-size: 8pt; margin-right: 2mm; vertical-align: 1px; }}
.topic {{ break-inside: avoid-page; }}
.overview-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 3mm; }}
.overview-grid > div, .weather-grid > div {{ background: #f8fafc; padding: 3mm; border-radius: 1.5mm; break-inside: avoid-page; }}
.weather-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 3mm; }}
.label {{ font-weight: 700; color: #0f766e; }}
.links {{ margin: 2mm 0 0; font-size: 9pt; }}
a {{ color: #155e75; text-decoration: none; word-break: break-all; }}
.empty {{ color: #64748b; }}
.method {{ font-size: 9pt; color: #475569; }}
</style>
</head>
<body>
<header class="page-header">农业育种新闻周报　周期：{_text(period_label)}</header>
<main>
  <section class="cover">
    <h1>农业育种新闻周报</h1>
    <p>统计周期：{_text(period_label)}<br>生成时间：{_text(generated)}<br>入选普通新闻：{len(selected)} 条（TOP5 为同批重点）</p>
  </section>
  <section><h2>核心观点摘要</h2>
    <div class="overview-grid">
      <div><span class="label">核心趋势</span><br>{_analysis_text(ai_analysis, "core_trends", "本期未生成 AI 核心趋势。")}</div>
      <div><span class="label">风险与争议</span><br>{_analysis_text(ai_analysis, "sentiment_controversy", "本期未识别到单独风险分类。")}</div>
      <div><span class="label">关注信号</span><br>{_analysis_text(ai_analysis, "signals", "本期未识别到新增关注信号。")}</div>
      <div><span class="label">后续建议</span><br>{_analysis_text(ai_analysis, "outlook_strategy", "持续跟踪种业政策、技术与灾害风险信号。")}</div>
    </div>
  </section>
  <section><h2>重点新闻</h2>{highlight_html}</section>
  <section><h2>入选新闻</h2>{topic_html}</section>
  {weather_html}
  <section><h2>趋势与指标</h2>
    <div class="overview-grid">
      <div><span class="label">入选总量</span><br>{len(selected)} 条（上限 20 条）</div>
      <div><span class="label">重点新闻</span><br>{len(highlights)} 条</div>
      <div><span class="label">主题覆盖</span><br>{len(topic_groups)} 个主题</div>
      <div><span class="label">气象专栏</span><br>{"已纳入" if agro_weather else "本期暂无官方报告"}</div>
    </div>
  </section>
  <section><h2>数据与方法说明</h2>
    <p class="method">普通新闻仅使用自然周筛选后的同一批最多 20 条结果；TOP5 仅为其中的重点展示，不重复计入。农业气象为独立官方专栏，不占新闻名额。按规范去重、按主题组织，无法验证的链接与缺失字段不展示；文本与链接均经过安全处理。</p>
  </section>
</main>
</body>
</html>"""


def _render_weather(agro_weather: Any) -> str:
    if agro_weather is None:
        return """<section><h2>农业气象与灾害风险</h2>
<p class="empty">本期未取得可验证的中央气象台农业气象周报。</p></section>"""
    title = _text(getattr(agro_weather, "title", "全国农业气象周报"))
    impact = _text(getattr(agro_weather, "impact", "")) or "暂未提供"
    outlook = _text(getattr(agro_weather, "outlook", "")) or "暂未提供"
    recommendations = _text(getattr(agro_weather, "recommendations", "")) or "暂未提供"
    source = _link(getattr(agro_weather, "source_url", ""), "中央气象台原页")
    return f"""<section><h2>农业气象与灾害风险</h2>
<p><strong>{title}</strong>{("　" + source) if source else ""}</p>
<div class="weather-grid">
  <div><span class="label">气象影响</span><br>{impact}</div>
  <div><span class="label">未来10天</span><br>{outlook}</div>
  <div><span class="label">区域作物风险</span><br>区域：{_format_collection(getattr(agro_weather, "risk_regions", ())) }<br>作物：{_format_collection(getattr(agro_weather, "risk_crops", ()))}</div>
  <div><span class="label">生产建议</span><br>{recommendations}</div>
</div></section>"""


def build_weekly_pdf(
    output_dir: str,
    period_start: date,
    period_end: date,
    html: str,
) -> str:
    """Persist the dedicated template and produce its precisely named PDF."""
    folder = Path(output_dir) / "pdf" / period_end.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stem = (
        f"农业育种新闻周报_{period_start:%Y-%m-%d}至"
        f"{period_end - timedelta(days=1):%Y-%m-%d}"
    )
    html_path = folder / f"{stem}.html"
    pdf_path = folder / f"{stem}.pdf"
    html_path.write_text(html, encoding="utf-8")
    return generate_pdf_from_html(str(html_path), str(pdf_path))
