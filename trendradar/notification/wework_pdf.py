# coding=utf-8
"""企业微信简报预览与 PDF 文件发送。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests

from trendradar.report.pdf import generate_pdf_from_html


MAX_WEWORK_FILE_BYTES = 20 * 1024 * 1024


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clip_utf8(value: Any, max_bytes: int) -> str:
    text = _clean_text(value)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = "…"
    budget = max(0, max_bytes - len(suffix.encode("utf-8")))
    result = bytearray()
    for char in text:
        encoded = char.encode("utf-8")
        if len(result) + len(encoded) > budget:
            break
        result.extend(encoded)
    return result.decode("utf-8") + suffix


def collect_highlights(
    report_data: Dict,
    rss_items: Optional[List[Dict]],
    *,
    limit: int = 5,
) -> List[Dict]:
    """跨热榜和 RSS 收集、去重并按 highlight_rank 排序。"""
    ranked: List[Dict] = []
    seen = set()
    groups = list(report_data.get("stats", [])) + list(rss_items or [])

    candidates = []
    for group in groups:
        for item in group.get("titles", []):
            rank = item.get("highlight_rank")
            try:
                rank_value = int(rank)
            except (TypeError, ValueError):
                continue
            if rank_value <= 0:
                continue
            candidates.append((rank_value, item))

    for _, item in sorted(candidates, key=lambda pair: pair[0]):
        key = _clean_text(item.get("url") or item.get("mobile_url"))
        if not key:
            key = _clean_text(item.get("title")).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        ranked.append(item)
        if len(ranked) >= max(0, int(limit)):
            break

    return ranked


def _escape_markdown_title(value: Any) -> str:
    return _clean_text(value).replace("[", "【").replace("]", "】")


def _render_preview(
    report_data: Dict,
    highlights: List[Dict],
    ai_analysis: Any,
    report_type: str,
    *,
    title_bytes: int,
    summary_bytes: int,
    include_links: bool,
) -> str:
    lines = [f"**🌾 TrendRadar · {report_type}**"]

    rss_matched = int(report_data.get("rss_matched_count", 0) or 0)
    rss_total = int(report_data.get("rss_total_count", 0) or 0)
    source_total = int(report_data.get("rss_source_total", 0) or 0)
    source_failed = int(report_data.get("rss_source_failed", 0) or 0)
    source_success = max(0, source_total - source_failed)
    lines.extend(
        [
            f"> RSS：{rss_matched} / {rss_total} · 来源：{source_success} / {source_total}",
            "",
        ]
    )

    core_trends = ""
    if ai_analysis and getattr(ai_analysis, "success", False):
        core_trends = _clip_utf8(
            getattr(ai_analysis, "core_trends", ""),
            600,
        )
    if core_trends:
        lines.extend(["**简短摘要**", core_trends, ""])

    lines.append(f"**⭐ 重点新闻 TOP {len(highlights)}**")
    for index, item in enumerate(highlights, start=1):
        title = _escape_markdown_title(_clip_utf8(item.get("title"), title_bytes))
        url = _clean_text(item.get("mobile_url") or item.get("url"))
        source = _clip_utf8(item.get("source_name"), 72)
        if include_links and url:
            lines.append(f"{index}. [{title}]({url})")
        else:
            lines.append(f"{index}. {title}")
        if source:
            lines.append(f"   来源：{source}")
        if summary_bytes > 0:
            summary = _clip_utf8(item.get("ai_summary"), summary_bytes)
            if summary:
                lines.append(f"   摘要：{summary}")

    lines.extend(["", "> 📎 完整报告见 PDF 附件"])
    return "\n".join(lines)


def build_wework_pdf_preview(
    report_data: Dict,
    rss_items: Optional[List[Dict]],
    ai_analysis: Any,
    report_type: str,
    *,
    top_n: int = 5,
    max_bytes: int = 4000,
) -> str:
    """生成单条企业微信摘要，必要时逐级压缩但保留 Top 5。"""
    highlights = collect_highlights(report_data, rss_items, limit=top_n)
    strategies = (
        (180, 240, True),
        (150, 0, True),
        (120, 0, False),
    )
    for title_bytes, summary_bytes, include_links in strategies:
        preview = _render_preview(
            report_data,
            highlights,
            ai_analysis,
            report_type,
            title_bytes=title_bytes,
            summary_bytes=summary_bytes,
            include_links=include_links,
        )
        if len(preview.encode("utf-8")) <= max_bytes:
            return preview
    raise ValueError("企业微信 PDF 预览消息超过长度限制")


def _wework_upload_target(webhook_url: str) -> tuple[str, str]:
    parsed = urlsplit(webhook_url)
    key_values = parse_qs(parsed.query).get("key", [])
    key = key_values[0].strip() if key_values else ""
    if parsed.scheme != "https" or not parsed.netloc or not key:
        raise ValueError("企业微信 Webhook URL 缺少有效 key")
    upload_url = urlunsplit(
        (parsed.scheme, parsed.netloc, "/cgi-bin/webhook/upload_media", "", "")
    )
    return upload_url, key


def _response_succeeded(response: requests.Response) -> bool:
    if response.status_code != 200:
        return False
    try:
        return response.json().get("errcode") == 0
    except (ValueError, AttributeError):
        return False


def upload_wework_file(
    webhook_url: str,
    file_path: str,
    *,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> str:
    """上传 PDF 到企业微信并返回仅对当前机器人有效的 media_id。"""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF 文件不存在: {path}")
    file_size = path.stat().st_size
    if file_size <= 5:
        raise ValueError("PDF 文件必须大于 5 字节")
    if file_size > MAX_WEWORK_FILE_BYTES:
        raise ValueError("PDF 文件超过企业微信 20MB 限制")

    upload_url, key = _wework_upload_target(webhook_url)
    with path.open("rb") as file:
        response = requests.post(
            upload_url,
            params={"key": key, "type": "file"},
            files={"media": (path.name, file, "application/pdf")},
            proxies=proxies,
            timeout=timeout,
        )
    if not _response_succeeded(response):
        raise RuntimeError("企业微信 PDF 上传失败")
    media_id = _clean_text(response.json().get("media_id"))
    if not media_id:
        raise RuntimeError("企业微信 PDF 上传成功但未返回 media_id")
    return media_id


def send_wework_file(
    webhook_url: str,
    media_id: str,
    *,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> bool:
    """发送企业微信文件消息。"""
    response = requests.post(
        webhook_url,
        headers={"Content-Type": "application/json"},
        json={"msgtype": "file", "file": {"media_id": media_id}},
        proxies=proxies,
        timeout=timeout,
    )
    return _response_succeeded(response)


def send_wework_pdf_report(
    webhook_url: str,
    html_file_path: str,
    report_data: Dict,
    rss_items: Optional[List[Dict]],
    ai_analysis: Any,
    report_type: str,
    *,
    top_n: int = 5,
    proxies: Optional[Dict[str, str]] = None,
) -> bool:
    """发送一条简报预览和一个完整 PDF 附件。"""
    pdf_path = generate_pdf_from_html(html_file_path)
    preview = build_wework_pdf_preview(
        report_data,
        rss_items,
        ai_analysis,
        report_type,
        top_n=top_n,
    )
    media_id = upload_wework_file(
        webhook_url,
        pdf_path,
        proxies=proxies,
    )

    preview_response = requests.post(
        webhook_url,
        headers={"Content-Type": "application/json"},
        json={"msgtype": "markdown", "markdown": {"content": preview}},
        proxies=proxies,
        timeout=30,
    )
    if not _response_succeeded(preview_response):
        return False

    if not send_wework_file(
        webhook_url,
        media_id,
        proxies=proxies,
    ):
        return False

    print(f"企业微信 PDF 报告发送完成: {pdf_path}")
    return True
