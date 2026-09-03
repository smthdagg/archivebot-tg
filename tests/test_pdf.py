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


def test_info_block_at_end_without_title():
    """信息块在正文之后且不含标题（标题只在顶部出现一次）。"""
    html = pdf._render_html(
        title="我的文章",
        author="张三",
        source="https://example.com",
        published="2026-09-01",
        url="https://example.com/a",
        content_html="<p>正文</p>",
        archived_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert html.index("<p>正文</p>") < html.index("作者 / Author")
    assert "<h1>我的文章</h1>" in html
    footer_part = html[html.index('class="footer"'):]
    assert "我的文章" not in footer_part
    assert "文章来源互联网" in footer_part


def test_body_front_matter_kept_template_title_suppressed():
    """正文自带的 标题/作者/发布时间 原样保留在前面，模板不再额外加标题。"""
    content = (
        "<h1>我的文章</h1>"
        '<p style="color:#888;font-size:0.9em">作者：张三 · https://example.com · 2026-09-01</p>'
        "<p>正文第一段</p>"
    )
    html = pdf._render_html(
        title="我的文章",
        author="张三",
        source="https://example.com",
        published="2026-09-01",
        url="https://example.com/a",
        content_html=content,
        archived_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert html.count("<h1>") == 1  # 只有正文自己的标题
    assert 'color:#888' in html  # 作者/发布时间行保留
    assert "正文第一段" in html
    footer_part = html[html.index('class="footer"'):]
    assert "我的文章" not in footer_part  # 文末信息块不含标题


def test_template_supplies_title_when_body_has_none():
    """正文没有标题时，模板补一个 h1 标题。"""
    html = pdf._render_html(
        title="我的文章",
        author="张三",
        source="https://example.com",
        published="2026-09-01",
        url="https://example.com/a",
        content_html="<p>正文第一段</p>",
        archived_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert html.count("<h1>") == 1
    assert "<h1>我的文章</h1>" in html
