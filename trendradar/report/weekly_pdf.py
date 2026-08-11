# coding=utf-8
"""A dedicated, self-contained printable template for agricultural weekly reports."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from trendradar.core.weekly import report_item_identity
from trendradar.report.pdf import (
    MAX_PDF_BYTES,
    MIN_PDF_BYTES,
    generate_pdf_from_html,
)


def _module_rank(value: Any) -> int:
    try:
        return int(value or 10**9)
    except (TypeError, ValueError):
        return 10**9


def _text(value: Any) -> str:
    """Escape all text supplied by feeds, AI, or remote official reports."""
    return escape(str(value or ""), quote=True)


def _css_string(value: Any) -> str:
    """Escape controlled text for a quoted CSS content value."""
    return (
        " ".join(str(value or "").split())
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("<", "\\3c ")
        .replace(">", "\\3e ")
    )


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


def _render_news_card(item: dict, *, marker_label: str) -> str:
    title = _text(item.get("title")) or "（无标题）"
    summary = _text(item.get("ai_summary") or item.get("summary"))
    source = _text(item.get("source_name") or item.get("feed_name") or "未标注来源")
    published_at = _text(item.get("published_at") or item.get("time_display") or "未标注时间")
    marker = ""
    if _module_rank(item.get("module_rank")) <= 5:
        marker = f'<span class="highlight-marker">{_text(marker_label)}</span>'
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
    policy_items: list[dict],
    research_items: list[dict],
    ai_analysis: Any,
    agro_weather: Any,
    period_label: str,
    generated_at: datetime,
) -> str:
    """Render only the selected weekly news into an offline A4 HTML document."""
    policy = list(policy_items or [])
    research = list(research_items or [])
    if len(policy) > 20 or len(research) > 20:
        raise ValueError("周报 PDF 每个模块只接受最多 20 条已入选新闻")
    identities = [report_item_identity(item) for item in policy + research]
    if (
        any(not identity[1] for identity in identities)
        or len(set(identities)) != len(identities)
    ):
        raise ValueError("周报 PDF 入选新闻身份必须全局唯一")
    policy.sort(key=lambda item: _module_rank(item.get("module_rank")))
    research.sort(key=lambda item: _module_rank(item.get("module_rank")))

    generated = generated_at.strftime("%Y-%m-%d %H:%M")
    page_header = _css_string(
        f"农业育种新闻周报　周期：{period_label}"
    )
    policy_html = "".join(
        _render_news_card(item, marker_label="重点政策") for item in policy
    ) or '<p class="empty">本周暂无符合条件的政策新闻</p>'
    research_html = "".join(
        _render_news_card(item, marker_label="重点文献") for item in research
    ) or '<p class="empty">本周暂无符合条件的科研文献</p>'
    total_selected = len(policy) + len(research)
    total_highlights = sum(
        _module_rank(item.get("module_rank")) <= 5
        for item in policy + research
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>农业育种新闻周报</title>
<style>
@page {{
  size: A4;
  margin: 18mm 14mm 18mm;
  @top-center {{
    content: "{page_header}";
    color: #475569;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    font-size: 8.5pt;
    border-bottom: 1px solid #cbd5e1;
    padding-bottom: 2mm;
  }}
  @bottom-center {{ content: "第 " counter(page) " 页"; }}
}}
* {{ box-sizing: border-box; }}
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; color: #172554; font-size: 10.5pt; line-height: 1.58; }}
.cover {{ border-left: 6px solid #0f766e; padding: 6mm 7mm; background: #f0fdfa; margin-bottom: 6mm; }}
h1 {{ margin: 0 0 3mm; color: #134e4a; font-size: 25pt; }}
h2 {{ color: #0f766e; font-size: 15pt; border-bottom: 1px solid #99f6e4; padding-bottom: 2mm; margin-top: 8mm; }}
h3 {{ font-size: 11.5pt; margin: 0 0 2mm; color: #164e63; }}
.meta {{ color: #475569; margin: 0 0 2mm; font-size: 9pt; }}
.summary {{ margin: 2mm 0; }}
.news-card {{ break-inside: avoid-page; border: 1px solid #dbeafe; border-radius: 2mm; padding: 3mm 4mm; margin: 3mm 0; background: #fff; }}
.highlight-marker {{ display: inline-block; color: #fff; background: #0f766e; padding: 0 1.6mm; border-radius: 1mm; font-size: 8pt; margin-right: 2mm; vertical-align: 1px; }}
.overview-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 3mm; }}
.overview-grid > div {{ background: #f8fafc; padding: 3mm; border-radius: 1.5mm; break-inside: avoid-page; }}
.weather-grid {{ display: block; }}
.weather-grid > div {{ background: #f8fafc; padding: 3mm; border-radius: 1.5mm; margin: 3mm 0; break-inside: auto; }}
.label {{ font-weight: 700; color: #0f766e; }}
.links {{ margin: 2mm 0 0; font-size: 9pt; }}
a {{ color: #155e75; text-decoration: none; word-break: break-all; }}
.empty {{ color: #64748b; }}
.method {{ font-size: 9pt; color: #475569; }}
</style>
</head>
<body>
<main>
  <section class="cover">
    <h1>农业育种新闻周报</h1>
    <p>统计周期：{_text(period_label)}<br>生成时间：{_text(generated)}<br>入选普通新闻：{total_selected} 条（政策 {len(policy)} 条，科研 {len(research)} 条）</p>
  </section>
  <section><h2>一、政策动态</h2>
    <div class="module-analysis"><span class="label">政策趋势研判</span><br>{_analysis_text(ai_analysis, "policy_trends", "本期暂无政策趋势分析。")}</div>
    {policy_html}
  </section>
  <section><h2>二、科研进展</h2>
    <div class="module-analysis"><span class="label">科研趋势研判</span><br>{_analysis_text(ai_analysis, "research_trends", "本期暂无科研趋势分析。")}</div>
    {research_html}
  </section>
  {_render_weather(agro_weather, ai_analysis)}
  <section><h2>趋势与指标</h2>
    <div class="overview-grid">
      <div><span class="label">政策新闻</span><br>{len(policy)} 条（上限 20 条）</div>
      <div><span class="label">科研文献</span><br>{len(research)} 条（上限 20 条）</div>
      <div><span class="label">TOP5 标记</span><br>{total_highlights} 条（每模块最多 5 条）</div>
      <div><span class="label">气象专栏</span><br>{"已纳入" if agro_weather else "本期暂无官方报告"}</div>
    </div>
  </section>
  <section><h2>数据与方法说明</h2>
    <p class="method">普通新闻仅使用自然周筛选后的政策、科研双模块结果，每模块最多 20 条、各自 TOP5 内联标记，每条仅展示一次。农业气象为第三个独立模块，不占新闻名额。无法验证的链接与缺失字段不展示；文本与链接均经过安全处理。</p>
  </section>
</main>
</body>
</html>"""


def _render_weather(agro_weather: Any, ai_analysis: Any) -> str:
    analysis = _analysis_text(
        ai_analysis, "weather_risks", "本期暂无农业气象风险分析。"
    )
    if agro_weather is None:
        return f"""<section><h2>三、农业气象与灾害风险</h2>
<div class="module-analysis"><span class="label">气象风险研判</span><br>{analysis}</div>
<p class="empty">本期未取得可验证的中央气象台农业气象周报。</p></section>"""
    title = _text(getattr(agro_weather, "title", "全国农业气象周报"))
    impact = _text(getattr(agro_weather, "impact", "")) or "暂未提供"
    outlook = _text(getattr(agro_weather, "outlook", "")) or "暂未提供"
    recommendations = _text(getattr(agro_weather, "recommendations", "")) or "暂未提供"
    source = _link(getattr(agro_weather, "source_url", ""), "中央气象台原页")
    return f"""<section><h2>三、农业气象与灾害风险</h2>
<div class="module-analysis"><span class="label">气象风险研判</span><br>{analysis}</div>
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
    """Validate unique temporary artifacts before replacing the formal pair."""
    html_path = weekly_pdf_output_path(
        output_dir, period_start, period_end, suffix=".html"
    )
    pdf_path = weekly_pdf_output_path(
        output_dir, period_start, period_end, suffix=".pdf"
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    temp_html = _unique_artifact_path(html_path, ".tmp.html")
    temp_pdf = _unique_artifact_path(pdf_path, ".tmp.pdf")
    backup_html = _unique_artifact_path(html_path, ".tmp.backup.html")
    backup_pdf = _unique_artifact_path(pdf_path, ".tmp.backup.pdf")
    had_html = html_path.is_file()
    had_pdf = pdf_path.is_file()
    replaced_html = False
    replaced_pdf = False
    try:
        temp_html.write_text(html, encoding="utf-8")
        generate_pdf_from_html(str(temp_html), str(temp_pdf))
        _validate_weekly_pdf(temp_pdf)

        if had_html:
            shutil.copy2(html_path, backup_html)
        if had_pdf:
            shutil.copy2(pdf_path, backup_pdf)
        os.replace(temp_html, html_path)
        replaced_html = True
        os.replace(temp_pdf, pdf_path)
        replaced_pdf = True
        return str(pdf_path)
    except Exception:
        if replaced_html:
            if had_html and backup_html.is_file():
                os.replace(backup_html, html_path)
            elif not had_html:
                html_path.unlink(missing_ok=True)
        if replaced_pdf:
            if had_pdf and backup_pdf.is_file():
                os.replace(backup_pdf, pdf_path)
            elif not had_pdf:
                pdf_path.unlink(missing_ok=True)
        raise
    finally:
        for artifact in (temp_html, temp_pdf, backup_html, backup_pdf):
            artifact.unlink(missing_ok=True)


def _unique_artifact_path(final_path: Path, suffix: str) -> Path:
    descriptor, path = tempfile.mkstemp(
        prefix=f".{final_path.stem}.", suffix=suffix, dir=final_path.parent
    )
    os.close(descriptor)
    artifact = Path(path)
    artifact.unlink()
    return artifact


def _validate_weekly_pdf(pdf_path: Path) -> None:
    if not pdf_path.is_file() or pdf_path.stat().st_size < MIN_PDF_BYTES:
        raise RuntimeError("Chromium 未生成有效 PDF 文件")
    if pdf_path.stat().st_size > MAX_PDF_BYTES:
        raise RuntimeError("生成的 PDF 超过 20MB 限制")
    with pdf_path.open("rb") as pdf_file:
        if pdf_file.read(4) != b"%PDF":
            raise RuntimeError("生成的文件不是有效 PDF")

    info = subprocess.run(
        ["pdfinfo", str(pdf_path)], capture_output=True, text=True,
        timeout=30, check=False,
    )
    if info.returncode != 0:
        raise RuntimeError("pdfinfo 无法验证周报 PDF")
    pages = re.search(r"Pages:\s+(\d+)", info.stdout)
    size = re.search(
        r"Page size:\s+59[4-6](?:\.\d+)? x 84[1-2](?:\.\d+)? pts",
        info.stdout,
    )
    if pages is None or int(pages.group(1)) < 1 or size is None:
        raise RuntimeError("周报 PDF 不是有效的 A4 文档")

    extracted = subprocess.run(
        ["pdftotext", str(pdf_path), "-"], capture_output=True, text=True,
        timeout=30, check=False,
    )
    if extracted.returncode != 0 or not re.search(
        r"[\u4e00-\u9fff]", extracted.stdout
    ):
        raise RuntimeError("周报 PDF 无法提取中文文本")


def weekly_pdf_output_path(
    output_dir: str,
    period_start: date,
    period_end: date,
    *,
    suffix: str = ".pdf",
) -> Path:
    """Return the deterministic artifact path for one weekly window."""
    if suffix not in {".html", ".pdf"}:
        raise ValueError(f"unsupported weekly report suffix: {suffix}")
    folder = Path(output_dir) / "pdf" / period_end.isoformat()
    stem = (
        f"农业育种新闻周报_三模块_{period_start:%Y-%m-%d}至"
        f"{period_end - timedelta(days=1):%Y-%m-%d}"
    )
    return folder / f"{stem}{suffix}"
