"""内容清洗测试。"""

from app.archive.cleaner import clean_html, clean_text


def test_removes_scripts_and_styles():
    html = '<div id="content">正文<script>alert(1)</script><style>.x{}</style></div>'
    cleaned = clean_html(html)
    assert "alert" not in cleaned
    assert "正文" in cleaned


def test_removes_ad_nav_containers():
    html = (
        '<div>正文内容</div>'
        '<div class="ad-container">广告</div>'
        '<nav>导航</nav>'
        '<div id="comments">评论区</div>'
    )
    cleaned = clean_html(html)
    assert "正文内容" in cleaned
    assert "广告" not in cleaned
    assert "导航" not in cleaned
    assert "评论区" not in cleaned


def test_clean_text_normalizes():
    text = "  第一行内容  \n\n\n  第二行\t内容  "
    assert clean_text(text) == "第一行内容\n\n第二行 内容"
