"""X 平台鉴别测试：检测/路由正确，不碰真实抓取。"""

import sys
from pathlib import Path

import pytest

from app.archive.detector import detect
from app.archive.fetcher import ErrorCode
from app.database.enums import Platform

sys.path.insert(0, ".")

# 判据 1：真实推文 URL 必须识别为推特平台
X_URL = "https://x.com/elonmusk/status/2013063069075169532"


def test_detect_identifies_real_tweet_as_twitter() -> None:
    assert detect(X_URL) == Platform.TWITTER


def test_dispatch_contains_twitter_target() -> None:
    from app.archive.fetcher import _DISPATCH

    assert Platform.TWITTER in _DISPATCH
    mod, cls, meth = _DISPATCH[Platform.TWITTER]
    # 只是登记，不验证交付
    assert mod == "services.twitter_service"
    assert meth == "get_tweet"


# 无授权时需落显式码（不误标 UNKNOWN）：vendor 无 cookie 的真实抓取会因
# 环境网络差异抛不同异常（本机可访问 x.com / CI runner 不可达），因此用
# mock 固定 vendor 异常文本做归类断言，不触网、不依赖浏览器可用性。
@pytest.mark.slow
def test_twitter_without_cookie_yields_login_required(tmp_path: Path, monkeypatch) -> None:
    from services.twitter_service import TwitterScrapingError, TwitterService

    from app.archive.fetcher import FetchError, fetch_article

    def _boom(self, url):
        raise TwitterScrapingError("所有提取策略都失败了")

    monkeypatch.setattr(TwitterService, "get_tweet", _boom)

    err = None
    try:
        fetch_article(url=X_URL, platform=Platform.TWITTER, task_dir=tmp_path)
    except FetchError as e:
        err = e
    assert err is not None
    # 正经的登录态提示，不再是包名不匹配导致的 UNKNOWN
    assert err.code == ErrorCode.LOGIN_REQUIRED
    assert "login required" in str(err).lower()


def test_twitter_other_error_not_mislabeled_login(tmp_path: Path, monkeypatch) -> None:
    """非登录类异常（网络错误等）不得误标 LOGIN_REQUIRED。"""
    from services.twitter_service import TwitterScrapingError, TwitterService

    from app.archive.fetcher import FetchError, fetch_article

    def _boom(self, url):
        raise TwitterScrapingError("Failed to fetch tweet: connection reset by peer")

    monkeypatch.setattr(TwitterService, "get_tweet", _boom)

    err = None
    try:
        fetch_article(url=X_URL, platform=Platform.TWITTER, task_dir=tmp_path)
    except FetchError as e:
        err = e
    assert err is not None
    assert err.code != ErrorCode.LOGIN_REQUIRED


# ---------------------------------------------------------------------------
# 回归（用户反馈）：引用推文必须包含 + 配图必须被引用（不再只下载不引用）
# ---------------------------------------------------------------------------

def test_tweet_to_article_includes_quote_and_images(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from app.archive.fetcher import _tweet_to_article

    class _FakeTweet:  # vendor Tweet 的最小替身
        id = "123"
        text = "主推文正文"
        html_content = "<p>主推文正文</p>"
        author_username = "someone"
        author_name = "Some One"
        created_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
        media_urls = ["https://pbs.twimg.com/media/abc.jpg?name=small"]
        media_types = ["photo"]
        reply_to = None
        conversation_id = "123"

    # 媒体下载 stub：落一个真实字节文件
    class _Resp:
        status_code = 200
        content = b"\xff\xd8fake"

        def raise_for_status(self):
            pass

    import app.archive.fetcher as fetcher_mod

    orig_get = fetcher_mod.requests.get if hasattr(fetcher_mod, "requests") else None
    import requests as _real_requests

    _real_requests.get = lambda *a, **kw: _Resp()  # type: ignore[assignment]
    try:
        extras = {
            "quoted_author": "quoted_user",
            "quoted_text": "引用推文的内容\n第二行",
            "thread": [
                {"author": "someone", "text": "自回复：裁决原文 pdf，大家需要可以自取\nhttps://assets.bwbx.io/documents/x"},
                {"author": "other", "text": "讨论回复"},
            ],
        }
        article = _tweet_to_article(_FakeTweet(), tmp_path, "https://x.com/someone/status/123", extras=extras)
    finally:
        if orig_get is not None:
            fetcher_mod.requests.get = orig_get

    md = article.markdown
    html = article.html
    # 引用推文进产物
    assert "引用推文的内容" in md and "quoted_user" in md
    assert "引用推文的内容" in html and "quoted_user" in html
    assert "<blockquote>" in html
    # 对话串进产物（作者自回复里的「引用文章」链接）
    assert "对话串" in md and "裁决原文 pdf" in md and "assets.bwbx.io" in md
    assert "对话串" in html and "裁决原文 pdf" in html
    # 配图被引用（本地 images/NN 路径，runner 内联 base64 后 PDF 带图）
    assert "![](images/" in md
    assert '<img src="images/' in html
    # 下载的文件实际存在
    imgs = list((tmp_path / article.save_path.name / "images").glob("*")) if article.save_path else []
    assert imgs, "配图应已下载到产物目录"
