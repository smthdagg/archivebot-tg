"""PDF 生成（设计规格 §11）。

链路：清洗后 HTML → HTML 模板 → Chromium Print-to-PDF。
版式：正文在前（自带的 标题/作者/发布时间 原样保留；正文无标题时模板补一个），
文末信息块（作者/来源/发布时间/原始链接，不含标题）+ 来源免责声明与生成时间。
页码走 Playwright footer_template（Chromium 不支持 CSS margin box）。
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Playwright 页脚模板：页码 "1 / N"。模板在 Chromium 内独立渲染，
# 必须用内联样式（不继承页面 CSS，也不加载外部字体/样式）。
_FOOTER_TEMPLATE = (
    '<div style="width:100%; text-align:center; font-size:9px; color:#888;">'
    '<span class="pageNumber"></span> / <span class="totalPages"></span>'
    "</div>"
)

# 显式传空头部，避免 Chromium 默认页眉（标题 + 日期）
_HEADER_TEMPLATE = "<span></span>"

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
         color: #222; line-height: 1.75; font-size: 14px; }}
  .meta {{ color: #666; font-size: 13px; margin: 6px 0 2px; }}
  .url {{ word-break: break-all; color: #1a73e8; }}
  .rule {{ border: none; border-top: 1px solid #ddd; margin: 18px 0; }}
  .content img {{ max-width: 100%; height: auto; display: block; margin: 12px auto; }}
  .content p {{ margin: 10px 0; }}
  h1 {{ font-size: 22px; line-height: 1.4; text-align: center; margin: 0 0 14px; }}
  /* vendor reader 的作者/来源/时间 meta 行跟随标题居中 */
  .content p[style*="color:#888"] {{ text-align: center; margin: 0 0 12px; }}
  .footer {{ margin-top: 24px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
{title_html}
  <div class="content">
{content_html}
  </div>
  <hr class="rule">
  <div class="footer">
    <div class="meta">作者 / Author：{author}</div>
    <div class="meta">来源 / Source：{source}</div>
    <div class="meta">发布时间 / Published：{published}</div>
    <div class="meta">原始链接 / Original：<span class="url">{url}</span></div>
    <div>文章来源互联网，仅供参考，如涉及商用请与财新官方联系。 · 生成时间 {archived_at}</div>
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


def _body_has_title_heading(content_html: str, title: str) -> bool:
    """正文里是否已有与标题一致的 h1/h2（有则模板不再重复加标题）。"""
    from bs4 import BeautifulSoup

    norm_title = re.sub(r"\s+", "", title or "")
    if not norm_title:
        return False
    soup = BeautifulSoup(content_html, "html.parser")
    for h in soup.find_all(["h1", "h2"]):
        if re.sub(r"\s+", "", h.get_text()) == norm_title:
            return True
    return False


def _render_html(
    *,
    title: str,
    author: str,
    source: str,
    published: str,
    url: str,
    content_html: str,
    archived_at: datetime,
) -> str:
    """填充 HTML 模板（正文不转义原样保留，其余字段转义）。

    正文自带的 标题/作者/发布时间 保持在前；仅当正文没有标题时模板补一个。
    """
    title_html = (
        "" if _body_has_title_heading(content_html, title or "") else f"<h1>{_escape(title or 'Untitled')}</h1>"
    )
    return _TEMPLATE.format(
        title=_escape(title or "Untitled"),
        title_html=title_html,
        author=_escape(author or "-"),
        source=_escape(source or "-"),
        published=_escape(published or "-"),
        url=_escape(url),
        content_html=content_html,
        archived_at=archived_at.strftime("%Y-%m-%d %H:%M"),
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
        page = browser.new_page()
        page.set_content(html, wait_until="load", timeout=60_000)
        try:
            # 给远程图片留加载时间；不可达时快速放弃，不让整页卡死
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001
            logger.info("networkidle wait timed out, printing with loaded content")
        page.pdf(
            path=str(output_path),
            format="A4",
            print_background=True,
            margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
            display_header_footer=True,
            header_template=_HEADER_TEMPLATE,
            footer_template=_FOOTER_TEMPLATE,
        )
        browser.close()
    logger.info("pdf generated: %s", output_path)
    return output_path
