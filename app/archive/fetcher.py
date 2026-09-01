"""平台抓取：调度 ArchiveBOT 服务（vendor/ArchiveBOT）获取内容（设计规格 §2.1/§3）。

ArchiveBOT 各平台服务的统一产物布局：
    <save_path>/{content.txt, content.md, content.html, metadata.json, images/}
本模块调用其 save_* 方法并读取该布局，包装为统一 FetchedArticle。
Telegram Gateway 不重复实现平台抓取逻辑。
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.archive import ssrf_guard
from app.archive.cookie_profile import (
    EXTERNAL_COOKIE_PLATFORMS,
    CookieProfileError,
    inject_cookies,
    load_profiles,
    resolve_cookies,
)
from app.database.enums import ErrorCode, Platform

logger = logging.getLogger(__name__)

# 把 ArchiveBOT 子模块注册进 sys.path（其 services/utils/models 为顶层包）。
# 必须 append 而非 insert：vendor 根目录下有自己的 app.py（Flask 入口），
# 放在 path 前部会遮蔽本项目自己的 app 包。
_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "ArchiveBOT"
if (_VENDOR_ROOT / "services" / "__init__.py").is_file() and str(_VENDOR_ROOT) not in sys.path:
    sys.path.append(str(_VENDOR_ROOT))


class FetchError(Exception):
    def __init__(self, message: str, code: str = ErrorCode.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FetchedArticle:
    title: str = ""
    author: str = ""
    sitename: str = ""
    published_at: str = ""
    source_url: str = ""
    markdown: str = ""
    html: str = ""
    text: str = ""
    images: list[Path] = field(default_factory=list)
    cover: Path | None = None
    save_path: Path | None = None  # ArchiveBOT 落盘目录（内含 images/）


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _collect_images(save_path: Path) -> list[Path]:
    img_dir = save_path / "images"
    if not img_dir.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    return sorted(p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in exts)


def _from_save_result(save_path: str, source_url: str, platform: str) -> FetchedArticle:
    """从 ArchiveBOT 的 save_* 产物目录读取统一内容。"""
    path = Path(save_path)
    meta = _read_json(path / "metadata.json")
    article = FetchedArticle(
        title=meta.get("title") or "",
        author=meta.get("author") or meta.get("author_name") or "",
        sitename=meta.get("sitename") or "",
        published_at=meta.get("published_date") or meta.get("publish_time") or "",
        source_url=source_url,
        markdown=(path / "content.md").read_text(encoding="utf-8") if (path / "content.md").exists() else "",
        html=(path / "content.html").read_text(encoding="utf-8") if (path / "content.html").exists() else "",
        text=(path / "content.txt").read_text(encoding="utf-8") if (path / "content.txt").exists() else "",
        images=_collect_images(path),
        save_path=path,
    )
    # 微信等平台的 cover 可能直接存在根目录
    for name in ("cover.jpg", "cover.png", "avatar.jpg"):
        candidate = path / name
        if candidate.exists():
            article.cover = candidate
            break
    logger.info("fetched platform=%s title=%r images=%d", platform, article.title, len(article.images))
    return article


# ---------------------------------------------------------------------------
# 平台 → ArchiveBOT 服务调度表
# 每个条目：(service 模块名, service 类名, save 方法名)
# 视频类平台（youtube/bilibili/douyin/tiktok/kuaishou/instagram）产出视频文件，
# 交付路径不同，属后续里程碑，不在本期表格内。
# ---------------------------------------------------------------------------

_DISPATCH: dict[Platform, tuple[str, str, str]] = {
    Platform.WEB: ("services.webpage_service", "WebpageService", "save_page"),
    Platform.WECHAT: ("services.wechat_service", "WechatService", "save_article"),
    Platform.REDDIT: ("services.reddit_service", "RedditService", "save_post"),
    Platform.TWITTER: ("services.twitter_service", "TwitterService", "save_tweet"),
    Platform.XHS: ("services.xhs_service", "XHSService", "save_post"),
    Platform.WEIBO: ("services.weibo_service", "WeiboService", "save_post"),
    Platform.ZHIHU: ("services.zhihu_service", "ZhihuService", "save_article"),
}


def fetch_article(
    url: str,
    platform: Platform,
    task_dir: Path,
    cookie_profile: str | None = None,
) -> FetchedArticle:
    """调用 ArchiveBOT 服务抓取并返回标准化内容。

    若传入 cookie_profile，则在调用前按平台注入该 profile 的 cookie
    （仅用于用户自己登录过的网站）。未适配平台抛 FetchError(ErrorCode.UNKNOWN)。
    """
    entry = _DISPATCH.get(platform)
    if entry is None:
        raise FetchError(
            f"Platform {platform.value} is not adapted yet",
            code=ErrorCode.UNKNOWN,
        )

    # 出网前装 requests 层守卫（幂等；覆盖 services 的 Session 与模块级 requests.get）
    ssrf_guard.ensure_installed()

    # Cookie Profile 注入（Phase 2）：解析 → 归一 → 调用期间注入
    profiles = load_profiles()
    try:
        cookies = resolve_cookies(profiles, cookie_profile, platform)
    except CookieProfileError as e:
        logger.error("cookie profile error for platform %s: %s", platform.value, e)
        raise FetchError(str(e), code=ErrorCode.UNKNOWN) from e
    if cookie_profile:
        if cookies is None:
            if platform.value in EXTERNAL_COOKIE_PLATFORMS:
                logger.warning(
                    "profile %r has no cookies for platform %s; fetching without cookies",
                    cookie_profile,
                    platform.value,
                )
            else:
                logger.info(
                    "cookie profile %r ignored for platform %s (no cookie support)",
                    cookie_profile,
                    platform.value,
                )

    module_name, class_name, method_name = entry
    try:
        module = __import__(module_name, fromlist=[class_name])
        cls = getattr(module, class_name)
        with inject_cookies(cls, platform, cookies):
            service = cls(base_path=str(task_dir), create_date_folders=False)
            result = getattr(service, method_name)(url)
    except ImportError as e:
        logger.error("ArchiveBOT dependency missing for %s: %s", platform, e)
        raise FetchError("ArchiveBOT backend unavailable", code=ErrorCode.UNKNOWN) from e
    except ssrf_guard.BlockedHostError as e:
        # 覆盖重定向跳转到内网的情况（入口只校验了首跳 URL）
        logger.error("SSRF guard blocked fetch for %s: %s", platform.value, e)
        raise FetchError("Blocked internal/private address", code=ErrorCode.INVALID_URL) from e
    except Exception as e:  # noqa: BLE001 - 平台服务错误种类繁多，统一归类
        logger.exception("ArchiveBOT %s failed: %s", platform, e)
        raise FetchError(_classify(platform, e), code=_classify_code(platform, e)) from e

    save_path = (result or {}).get("save_path")
    if not save_path:
        raise FetchError("ArchiveBOT returned no save_path", code=ErrorCode.EMPTY_CONTENT)
    return _from_save_result(save_path, url, platform.value)


def _classify(platform: Platform, exc: Exception) -> str:
    text = str(exc).lower()
    if "403" in text or "forbidden" in text:
        return "HTTP 403"
    if "login" in text or "captcha" in text:
        return "Login required."
    if "404" in text or "not found" in text:
        return "Page not found"
    if "timeout" in text:
        return "Timeout"
    return str(exc)[:200] or exc.__class__.__name__


def _classify_code(platform: Platform, exc: Exception) -> str:
    text = str(exc).lower()
    if "403" in text or "forbidden" in text:
        return ErrorCode.HTTP_FORBIDDEN
    if "login" in text or "captcha" in text:
        return ErrorCode.LOGIN_REQUIRED
    if "404" in text or "not found" in text:
        return ErrorCode.NOT_FOUND
    if "timeout" in text:
        return ErrorCode.TIMEOUT
    if "empty" in text or "no content" in text or "no title" in text:
        return ErrorCode.EMPTY_CONTENT
    return ErrorCode.UNKNOWN
