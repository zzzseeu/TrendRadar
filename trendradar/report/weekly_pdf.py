# coding=utf-8
"""A dedicated, self-contained printable template for agricultural weekly reports."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from trendradar.core.weekly import primary_weekly_topic, report_item_identity
from trendradar.report.pdf import (
    MAX_PDF_BYTES,
    MIN_PDF_BYTES,
    generate_pdf_from_html,
)


logger = logging.getLogger(__name__)


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


def _render_news_card(
    item: dict, *, module_type: str, marker_label: str
) -> str:
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
    module_rank = _module_rank(item.get("module_rank"))
    evidence_id = f"[{module_type}:{module_rank}]"
    primary_topic = primary_weekly_topic(item)
    return (
        '<article class="news-card">'
        f'<h3>{marker}{title}</h3>'
        f'<p class="evidence-meta">模块排名：{module_rank}　'
        f'证据ID：{_text(evidence_id)}　主主题：{_text(primary_topic)}</p>'
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
    industry_items: list[dict] | None = None,
    missing_dates: list[str] | None = None,
    failed_sources: dict[str, list[str]] | None = None,
) -> str:
    """Render only the selected weekly news into an offline A4 HTML document."""
    policy = list(policy_items or [])
    industry = list(industry_items or [])
    research = list(research_items or [])
    if len(policy) > 20 or len(industry) > 20 or len(research) > 20:
        raise ValueError("周报 PDF 每个模块只接受最多 20 条已入选新闻")
    _validate_module_ranks(policy, "policy")
    _validate_module_ranks(industry, "industry")
    _validate_module_ranks(research, "research")
    identities = [
        report_item_identity(item) for item in policy + industry + research
    ]
    if (
        any(not identity[1] for identity in identities)
        or len(set(identities)) != len(identities)
    ):
        raise ValueError("周报 PDF 入选新闻身份必须全局唯一")
    policy.sort(key=lambda item: _module_rank(item.get("module_rank")))
    industry.sort(key=lambda item: _module_rank(item.get("module_rank")))
    research.sort(key=lambda item: _module_rank(item.get("module_rank")))

    generated = generated_at.strftime("%Y-%m-%d %H:%M")
    page_header = _css_string(
        f"农业育种新闻周报　周期：{period_label}"
    )
    policy_html = "".join(
        _render_news_card(
            item, module_type="policy", marker_label="重点政策"
        ) for item in policy
    ) or '<p class="empty">本周暂无符合条件的政策新闻</p>'
    industry_html = "".join(
        _render_news_card(
            item, module_type="industry", marker_label="重点动态"
        ) for item in industry
    ) or '<p class="empty">本周暂无符合条件的水稻产业时事动态</p>'
    research_html = "".join(
        _render_news_card(
            item, module_type="research", marker_label="重点文献"
        ) for item in research
    ) or '<p class="empty">本周暂无符合条件的科研文献</p>'
    total_selected = len(policy) + len(industry) + len(research)
    total_highlights = sum(
        _module_rank(item.get("module_rank")) <= 5
        for item in policy + industry + research
    )
    policy_topics = _module_topic_coverage(policy)
    industry_topics = _module_topic_coverage(industry)
    research_topics = _module_topic_coverage(research)
    source_status_html = _render_source_status(
        missing_dates or [], failed_sources or {}
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
.evidence-meta {{ color: #0f766e; margin: 0 0 1mm; font-size: 9pt; font-weight: 600; }}
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
    <p>统计周期：{_text(period_label)}<br>生成时间：{_text(generated)}<br>入选普通新闻：{total_selected} 条（政策 {len(policy)} 条，产业动态 {len(industry)} 条，科研 {len(research)} 条）</p>
  </section>
  <section class="primary-module"><h2>一、政策动态</h2>
    <div class="module-analysis"><span class="label">政策趋势研判</span><br>{_analysis_text(ai_analysis, "policy_trends", "本期暂无政策趋势分析。")}</div>
    {policy_html}
  </section>
  <section class="primary-module"><h2>二、水稻产业时事动态</h2>
    <div class="module-analysis"><span class="label">产业动态研判</span><br>{_analysis_text(ai_analysis, "industry_trends", "本期暂无水稻产业动态分析。")}</div>
    {industry_html}
  </section>
  <section class="primary-module"><h2>三、科研进展</h2>
    <div class="module-analysis"><span class="label">科研趋势研判</span><br>{_analysis_text(ai_analysis, "research_trends", "本期暂无科研趋势分析。")}</div>
    {research_html}
  </section>
  {_render_weather(agro_weather, ai_analysis)}
  {source_status_html}
  <aside class="report-metrics"><h3>趋势与指标</h3>
    <div class="overview-grid">
      <div><span class="label">政策新闻</span><br>{len(policy)} 条（上限 20 条）</div>
      <div><span class="label">产业动态</span><br>{len(industry)} 条（上限 20 条）</div>
      <div><span class="label">科研文献</span><br>{len(research)} 条（上限 20 条）</div>
      <div><span class="label">政策主题覆盖</span><br>{_format_collection(policy_topics)}</div>
      <div><span class="label">产业主题覆盖</span><br>{_format_collection(industry_topics)}</div>
      <div><span class="label">科研主题覆盖</span><br>{_format_collection(research_topics)}</div>
      <div><span class="label">TOP5 标记</span><br>{total_highlights} 条（每模块最多 5 条）</div>
      <div><span class="label">气象专栏</span><br>{"已纳入" if agro_weather else "本期暂无官方报告"}</div>
    </div>
  </aside>
  <aside class="report-method"><h3>数据与方法说明</h3>
    <p class="method">普通新闻仅使用自然周筛选后的政策、水稻产业动态、科研三模块结果，每模块最多 20 条、各自 TOP5 内联标记，每条仅展示一次。农业气象为第四个独立模块，不占新闻名额。无法验证的链接与缺失字段不展示；文本与链接均经过安全处理。</p>
  </aside>
</main>
</body>
</html>"""


def _render_source_status(
    missing_dates: list[str], failed_sources: dict[str, list[str]]
) -> str:
    if not missing_dates and not failed_sources:
        return ""
    missing = _format_collection(sorted(set(missing_dates)))
    failures = "；".join(
        f"{_text(day)}：{_format_collection(source_ids)}"
        for day, source_ids in sorted(failed_sources.items())
    ) or "无"
    return (
        '<aside class="source-status"><h3>来源采集状态</h3>'
        f'<p class="method">缺失抓取日期：{missing}<br>'
        f'不可访问或失败来源：{failures}。其余可用来源正常参与本期筛选。</p>'
        '</aside>'
    )


def _validate_module_ranks(items: list[dict], module_name: str) -> None:
    ranks = [item.get("module_rank") for item in items]
    if (
        any(not isinstance(rank, int) or isinstance(rank, bool) for rank in ranks)
        or sorted(ranks) != list(range(1, len(items) + 1))
    ):
        raise ValueError(
            f"周报 PDF {module_name} module_rank 必须是唯一连续正整数 1..N"
        )


def _module_topic_coverage(items: list[dict]) -> list[str]:
    topics = set()
    for item in items:
        values = item.get("weekly_topics") or item.get("tags") or []
        if isinstance(values, str):
            values = [values]
        topics.update(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
    return sorted(topics)


def _render_weather(agro_weather: Any, ai_analysis: Any) -> str:
    analysis = _analysis_text(
        ai_analysis, "weather_risks", "本期暂无农业气象风险分析。"
    )
    if agro_weather is None:
        return f"""<section class="primary-module"><h2>四、农业气象与灾害风险</h2>
<div class="module-analysis"><span class="label">气象风险研判</span><br>{analysis}</div>
<p class="empty">本期未取得可验证的中央气象台农业气象周报。</p></section>"""
    title = _text(getattr(agro_weather, "title", "全国农业气象周报"))
    impact = _text(getattr(agro_weather, "impact", "")) or "暂未提供"
    outlook = _text(getattr(agro_weather, "outlook", "")) or "暂未提供"
    recommendations = _text(getattr(agro_weather, "recommendations", "")) or "暂未提供"
    source = _link(getattr(agro_weather, "source_url", ""), "中央气象台原页")
    return f"""<section class="primary-module"><h2>四、农业气象与灾害风险</h2>
<div class="module-analysis"><span class="label">气象风险研判</span><br>{analysis}</div>
<p class="evidence-meta">证据ID：<span class="evidence-id">[weather:official]</span></p>
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
    except Exception:
        preserved_backups = set()
        if replaced_html:
            if had_html and backup_html.is_file():
                try:
                    os.replace(backup_html, html_path)
                except Exception as rollback_error:
                    preserved_backups.add(backup_html)
                    logger.warning(
                        "周报 HTML 回滚失败，保留可恢复 backup %s: %s",
                        backup_html, rollback_error,
                    )
            elif not had_html:
                _cleanup_artifacts((html_path,))
        if replaced_pdf:
            if had_pdf and backup_pdf.is_file():
                try:
                    os.replace(backup_pdf, pdf_path)
                except Exception as rollback_error:
                    preserved_backups.add(backup_pdf)
                    logger.warning(
                        "周报 PDF 回滚失败，保留可恢复 backup %s: %s",
                        backup_pdf, rollback_error,
                    )
            elif not had_pdf:
                _cleanup_artifacts((pdf_path,))
        _cleanup_artifacts(
            (temp_html, temp_pdf, backup_html, backup_pdf),
            preserve=preserved_backups,
        )
        raise
    _cleanup_artifacts((temp_html, temp_pdf, backup_html, backup_pdf))
    return str(pdf_path)


def _cleanup_artifacts(
    artifacts: tuple[Path, ...], *, preserve: set[Path] | None = None
) -> None:
    preserved = preserve or set()
    for artifact in artifacts:
        if artifact in preserved:
            continue
        try:
            artifact.unlink(missing_ok=True)
        except Exception as cleanup_error:
            logger.warning(
                "周报临时文件清理失败，保留 %s: %s", artifact, cleanup_error
            )


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

    pdfinfo_bin = os.environ.get("PDFINFO_BIN", "pdfinfo").strip() or "pdfinfo"
    pdftotext_bin = (
        os.environ.get("PDFTOTEXT_BIN", "pdftotext").strip() or "pdftotext"
    )
    try:
        info = subprocess.run(
            [pdfinfo_bin, str(pdf_path)], capture_output=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"无法执行 PDFINFO_BIN（{pdfinfo_bin}）验证周报 PDF"
        ) from exc
    if info.returncode != 0:
        detail = (info.stderr or "").strip()[-300:]
        raise RuntimeError(
            f"PDFINFO_BIN（{pdfinfo_bin}）无法验证周报 PDF: {detail}"
        )
    pages = re.search(r"Pages:\s+(\d+)", info.stdout)
    size = re.search(
        r"Page size:\s+59[4-6](?:\.\d+)? x 84[1-2](?:\.\d+)? pts",
        info.stdout,
    )
    if pages is None or int(pages.group(1)) < 1 or size is None:
        raise RuntimeError("周报 PDF 不是有效的 A4 文档")

    try:
        extracted = subprocess.run(
            [pdftotext_bin, "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"无法执行 PDFTOTEXT_BIN（{pdftotext_bin}）提取周报中文"
        ) from exc
    if extracted.returncode != 0 or not re.search(
        r"[\u4e00-\u9fff]", extracted.stdout
    ):
        detail = (extracted.stderr or "").strip()[-300:]
        raise RuntimeError(
            f"PDFTOTEXT_BIN（{pdftotext_bin}）无法提取周报中文文本: {detail}"
        )


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
