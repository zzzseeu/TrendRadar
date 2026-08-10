# coding=utf-8
"""企业微信 PDF 文件上传与发送。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests

MAX_WEWORK_FILE_BYTES = 20 * 1024 * 1024


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


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
    with path.open("rb") as file:
        if not file.read(5).startswith(b"%PDF-"):
            raise ValueError("PDF 文件无效")

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


def send_wework_pdf_file(
    webhook_url: str,
    pdf_file_path: str,
    *,
    proxies: Optional[Dict[str, str]] = None,
) -> bool:
    """上传并发送一条企业微信 PDF 文件消息。"""
    media_id = upload_wework_file(
        webhook_url,
        pdf_file_path,
        proxies=proxies,
    )
    if not send_wework_file(
        webhook_url,
        media_id,
        proxies=proxies,
    ):
        return False
    print(f"企业微信 PDF 文件发送完成: {pdf_file_path}")
    return True
