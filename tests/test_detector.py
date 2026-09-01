"""detector 平台识别测试。"""

from app.archive.detector import detect, extract_first_url
from app.database.enums import Platform


def test_wechat():
    assert detect("https://mp.weixin.qq.com/s/abc123") == Platform.WECHAT
    assert detect("https://mp.weixin.qq.com/s?__biz=xxx") == Platform.WECHAT


def test_twitter():
    assert detect("https://twitter.com/user/status/123") == Platform.TWITTER
    assert detect("https://x.com/user/status/123") == Platform.TWITTER


def test_xhs():
    assert detect("https://www.xiaohongshu.com/explore/abc") == Platform.XHS
    assert detect("https://xhslink.com/a/b") == Platform.XHS


def test_other_platforms():
    assert detect("https://weibo.com/123") == Platform.WEIBO
    assert detect("https://www.zhihu.com/question/1") == Platform.ZHIHU
    assert detect("https://www.reddit.com/r/test/comments/1/") == Platform.REDDIT
    assert detect("https://www.youtube.com/watch?v=abc") == Platform.YOUTUBE
    assert detect("https://www.bilibili.com/video/BV1") == Platform.BILIBILI
    assert detect("https://www.douyin.com/video/123") == Platform.DOUYIN


def test_generic_web():
    assert detect("https://example.com/article/1") == Platform.WEB


def test_extract_first_url():
    text = "看看这个 https://example.com/a?b=1 的文章"
    assert extract_first_url(text) == "https://example.com/a?b=1"
    assert extract_first_url("没有链接") is None
