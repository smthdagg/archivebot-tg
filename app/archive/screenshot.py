"""长截图生成（复用 PDF 的 HTML 模板与图片内联链路）。

与 pdf.py 同模板、同数据，仅末端调用 page.screenshot(full_page=True) 而非 page.pdf()。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def build_screenshot(
    *,
    title: str,
    author: str,
    source: str,
    published: str,
    url: str,
    content_html: str,
    output_path: Path,
    archived_at: datetime | None = None,
) -> Path:
    """渲染 HTML 模板并用 Chromium 截长图（full_page PNG）。

    依赖 Playwright；在 worker 容器内运行。
    content_html 应已通过 _rewrite_image_srcs 完成 base64 内联。
    """
    from playwright.sync_api import sync_playwright

    from app.archive.pdf import _render_html

    archived_at = archived_at or datetime.now(timezone.utc)
    html = _render_html(
        title=title,
        author=author,
        source=source,
        published=published,
        url=url,
        content_html=content_html,
        archived_at=archived_at,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # 800px 宽度适合阅读；2x 保证清晰度；full_page 高度=内容高度
        page = browser.new_page(viewport={"width": 800, "height": 600}, device_scale_factor=2)
        page.set_content(html, wait_until="load", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001
            logger.info("networkidle wait timed out, capturing screenshot with loaded content")
        page.screenshot(path=str(output_path), full_page=True, type="png")
        browser.close()
    logger.info("screenshot generated: %s", output_path)
    return output_path


def maybe_compress_for_telegram(path: Path, max_bytes: int) -> Path:
    """若 PNG 超过 50MB，转换为 JPEG 80% 降体积；否则原样返回。"""
    try:
        if path.stat().st_size <= max_bytes * 0.8:
            return path
        from PIL import Image

        jpeg_path = path.with_suffix(".jpg")
        img = Image.open(path)
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        img.save(jpeg_path, "JPEG", quality=80, optimize=True)
        logger.info("screenshot compressed %s -> %s", path, jpeg_path)
        return jpeg_path
    except Exception:
        return path
