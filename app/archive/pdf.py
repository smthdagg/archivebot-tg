"""PDF 生成（设计规格 §11）。

链路：清洗后 HTML → HTML 模板 → Chromium Print-to-PDF。
PDF 页包含：标题、作者、来源、发布时间、原始 URL、正文、图片、页码、归档时间。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{
    size: A4;
    margin: 18mm 16mm;
    @bottom-center {{
      content: counter(page) " / " counter(pages);
      font-size: 9px;
      color: #888;
    }}
  }}
  body {{ font-family: "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
         color: #222; line-height: 1.75; font-size: 14px; }}
  .meta {{ color: #666; font-size: 13px; margin: 6px 0 2px; }}
  .url {{ word-break: break-all; color: #1a73e8; }}
  .rule {{ border: none; border-top: 1px solid #ddd; margin: 18px 0; }}
  .content img {{ max-width: 100%; height: auto; display: block; margin: 12px auto; }}
  .content p {{ margin: 10px 0; }}
  h1 {{ font-size: 22px; line-height: 1.4; }}
  .footer {{ margin-top: 24px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">作者 / Author：{author}</div>
  <div class="meta">来源 / Source：{source}</div>
  <div class="meta">发布时间 / Published：{published}</div>
  <div class="meta">原始链接 / Original：<span class="url">{url}</span></div>
  <hr class="rule">
  <div class="content">
{content_html}
  </div>
  <hr class="rule">
  <div class="footer">
    Archived by ArchiveBOT · {archived_at}
  </div>
</body>
</html>
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_pdf(
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
    """渲染 HTML 模板并用 Chromium 打印为 PDF。

    依赖 Playwright；在 worker 容器内运行。失败抛异常由调用方归类。
    """
    from playwright.sync_api import sync_playwright

    archived_at = archived_at or datetime.now(timezone.utc)
    html = _TEMPLATE.format(
        title=_escape(title or "Untitled"),
        author=_escape(author or "-"),
        source=_escape(source or "-"),
        published=_escape(published or "-"),
        url=_escape(url),
        content_html=content_html,
        archived_at=archived_at.strftime("%Y-%m-%d %H:%M"),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(path=str(output_path), format="A4", print_background=True)
        browser.close()
    logger.info("pdf generated: %s", output_path)
    return output_path
