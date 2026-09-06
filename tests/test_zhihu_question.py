"""zhihu_question 知乎问题页归档单测（浏览器链路由 VPS 真实任务验证）。

覆盖：纯问题页 URL 识别（排除 answer/article/pin 形态）；
     回答正文清洗（剥脚本/视频，图容器保留）；
     图片下载本地化（src → images/，data-original-src 保留原 URL）。
"""


import pytest

from app.archive import zhihu_question as zq


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.zhihu.com/question/2079254257211893392", "2079254257211893392"),
        ("https://www.zhihu.com/question/123456789", "123456789"),
        ("https://www.zhihu.com/question/123?utm_source=wechat", "123"),
        # answer / article / pin 形态不属于问题页
        ("https://www.zhihu.com/question/2079866742101241983/answer/2079969373893276150", None),
        ("https://www.zhihu.com/answer/123", None),
        ("https://zhuanlan.zhihu.com/p/123", None),
        ("https://www.zhihu.com/pin/123", None),
        ("https://example.com/question/123", None),
        ("https://www.zhihu.com/question/abc", None),
    ],
)
def test_classify_zhihu_question(url, expected):
    assert zq.classify_zhihu_question(url) == expected


def test_clean_answer_body_strips_noise_keeps_content():
    raw = (
        "<p>正文<strong>加粗</strong></p>"
        '<script>alert(1)</script>'
        '<iframe src="https://www.youtube.com/embed/x"></iframe>'
        '<figure><img src="https://pic1.zhimg.com/1.jpg"></figure>'
        '<p><img src="https://pic2.zhimg.com/2.png" data-actualsrc="https://pic2.zhimg.com/2_big.png"></p>'
    )
    out = zq._clean_answer_body(raw)
    assert "<script" not in out and "<iframe" not in out
    assert "<strong>加粗</strong>" in out
    # figure 内图片保留、src/data-actualsrc 属性保留（下载阶段消费）
    assert 'src="https://pic1.zhimg.com/1.jpg"' in out
    assert "data-actualsrc" in out


def test_download_images_localizes(monkeypatch, tmp_path):
    """图片下载成功 → src 改 images/xxx + data-original-src 保留远程；data URI 剔除。"""
    import sys
    import types

    class _Resp:
        def __init__(self, url):
            self.status_code = 200
            self.headers = {"Content-Type": "image/png" if url.endswith(".png") else "image/jpeg"}
            self.content = b"\xff\xd8\xff\xe0fake-jpeg"

        def raise_for_status(self):
            pass

    calls: list[str] = []

    def fake_get(url, timeout=20, headers=None):
        calls.append(url)
        return _Resp(url)

    fake_req = types.SimpleNamespace(get=fake_get)
    # _download_images 内部 `import requests as _req` 读取 sys.modules
    monkeypatch.setitem(sys.modules, "requests", fake_req)

    body = (
        "<p>看图</p>"
        '<p><img src="https://pic1.zhimg.com/a.jpg"></p>'
        '<p><img src="https://pic2.zhimg.com/b.png" data-original="https://pic2.zhimg.com/b_big.png"></p>'
        '<p><img src="data:image/png;base64,AAAA"></p>'
    )
    out = zq._download_images(body, tmp_path, 1, referer="https://www.zhihu.com/question/1")

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["q1_01.jpg", "q1_02.png"]
    assert 'src="images/q1_01.jpg"' in out
    assert 'src="images/q1_02.png"' in out
    assert 'data-original-src="https://pic1.zhimg.com/a.jpg"' in out
    assert "data:image/png" not in out  # data URI 图剔除
    # data-original 优先（高清大图）
    assert calls == ["https://pic1.zhimg.com/a.jpg", "https://pic2.zhimg.com/b_big.png"]
    assert (tmp_path / "q1_01.jpg").read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg"


def test_text_fallback_md_keeps_sections():
    html = (
        "<h1>问题</h1>"
        '<section><h2>半佛仙人 的回答</h2><p style="x">587 人赞同了该回答</p>'
        "<p>正文第一句。</p></section>"
        '<section><h2>答主 2 的回答</h2><p>第二答正文。</p></section>'
    )
    md = zq._text_fallback_md(html, "问题")
    assert md.startswith("# 问题")
    assert "## 半佛仙人 的回答" in md
    assert "> 587 人赞同了该回答" in md
    assert "正文第一句。" in md
    assert "第二答正文。" in md
