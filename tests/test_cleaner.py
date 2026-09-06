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


def test_decompose_in_loop_does_not_crash():
    """回归：decompose 后代 attrs 置 None 导致的崩溃（bs4 遍历中删除）。"""
    html = (
        '<div class="ad-container">广告'
        '<div class="ad-sub"><p>子广告</p><span>更多</span></div>'
        "</div>"
        "<div>正文内容</div>"
    )
    assert clean_html(html)  # 不抛 AttributeError
    assert "正文内容" in clean_html(html)


def test_clean_text_normalizes():
    text = "  第一行内容  \n\n\n  第二行\t内容  "
    assert clean_text(text) == "第一行内容\n\n第二行 内容"
