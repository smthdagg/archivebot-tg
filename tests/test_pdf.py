"""PDF 模板渲染测试：转义正确、无源码注释泄漏、页码走 footer_template。"""

from datetime import datetime, timezone

from app.archive import pdf


def test_render_html_escapes_user_fields():
    html = pdf._render_html(
        title="<script>alert(1)</script> 文章",
        author='A"&b',
        source="x",
        published="2026-01-01",
        url="https://example.com/a?x=1&y=2",
        content_html="<p>正文</p>",
        archived_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "A&quot;&amp;b" in html
    assert "https://example.com/a?x=1&amp;y=2" in html
    assert "<p>正文</p>" in html  # 正文保持原样


def test_template_has_no_margin_box_or_code_comments():
    # Chromium 不支持 @bottom-center 等 margin box；历史缺陷是把 noqa 注释写进了模板字符串
    assert "@bottom-center" not in pdf._TEMPLATE
    assert "noqa" not in pdf._TEMPLATE


def test_footer_template_uses_playwright_counters():
    assert 'class="pageNumber"' in pdf._FOOTER_TEMPLATE
    assert 'class="totalPages"' in pdf._FOOTER_TEMPLATE
    assert pdf._HEADER_TEMPLATE.strip() == "<span></span>"
