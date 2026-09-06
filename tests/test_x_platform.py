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
