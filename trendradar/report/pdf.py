# coding=utf-8
"""将现有 HTML 报告打印为 PDF。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


MIN_PDF_BYTES = 6
MAX_PDF_BYTES = 20 * 1024 * 1024


def _find_chromium() -> Optional[str]:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def generate_pdf_from_html(
    html_file_path: str,
    output_file_path: Optional[str] = None,
    *,
    timeout: int = 120,
) -> str:
    """使用无头 Chromium 将 HTML 报告转换成同目录 PDF。"""
    html_path = Path(html_file_path).resolve()
    pdf_path = (
        Path(output_file_path).resolve()
        if output_file_path
        else html_path.with_suffix(".pdf")
    )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{pdf_path.stem}.", suffix=".tmp.pdf", dir=pdf_path.parent
    )
    os.close(descriptor)
    staged_pdf = Path(staged_name)
    staged_pdf.unlink()
    try:
        if not html_path.is_file():
            raise FileNotFoundError(f"HTML 报告不存在: {html_path}")

        chromium = _find_chromium()
        if not chromium:
            raise RuntimeError("未找到 Chromium，无法生成 PDF 报告")

        with tempfile.TemporaryDirectory(prefix="trendradar-chromium-") as profile_dir:
            command = [
                chromium,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-pdf-header-footer",
                "--print-to-pdf-no-header",
                "--print-to-pdf=" + str(staged_pdf),
                "--user-data-dir=" + profile_dir,
                html_path.as_uri(),
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=max(1, int(timeout)),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"PDF 生成超时 ({timeout}s)") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(
                f"Chromium 生成 PDF 失败，退出码 {result.returncode}: {detail}"
            )

        if not staged_pdf.is_file() or staged_pdf.stat().st_size < MIN_PDF_BYTES:
            raise RuntimeError("Chromium 未生成有效 PDF 文件")
        if staged_pdf.stat().st_size > MAX_PDF_BYTES:
            raise RuntimeError("生成的 PDF 超过 20MB 限制")

        with staged_pdf.open("rb") as file:
            if file.read(4) != b"%PDF":
                raise RuntimeError("生成的文件不是有效 PDF")
        os.replace(staged_pdf, pdf_path)
        return str(pdf_path)
    finally:
        staged_pdf.unlink(missing_ok=True)
