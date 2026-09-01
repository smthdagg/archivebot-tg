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

# 微信公众号解析需要 wechat_to_md 包（vendor 化避免外部工具路径依赖）。
# 用 insert 在这段注册，比 wechat_service.py 的 ~/.agent-reach/tools/wechat-article-for-ai
# 优先，同时也服务容器内无该路径的情况。
_WECHAT_VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "wechat_to_md"
if _WECHAT_VENDOR.is_dir() and str(_WECHAT_VENDOR.parent) not in sys.path:
    sys.path.insert(0, str(_WECHAT_VENDOR.parent))


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


@dataclass
class VideoResult:
    """视频类平台（youtube/bilibili/douyin/kuaishou/instagram）的抓取结果。

    对应 ArchiveBOT 视频 service 的统一产物布局：
        <save_path>/{content.txt, content.md, metadata.json, videos/video.mp4, thumbnails/}
    """

    title: str = ""
    author: str = ""
    sitename: str = ""
    published_at: str = ""
    source_url: str = ""
    video_path: Path | None = None
    cover: Path | None = None
    save_path: Path | None = None  # ArchiveBOT 落盘目录
    duration: str = ""
    video_id: str = ""


def _find_video_file(save_path: Path) -> Path | None:
    """定位 service 产出的视频文件（统一约定 videos/video.mp4，兼容其它命名）。"""
    vid_dir = save_path / "videos"
    if not vid_dir.exists():
        return None
    fixed = vid_dir / "video.mp4"
    if fixed.exists():
        return fixed
    candidates = sorted(p for p in vid_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mp4")
    return candidates[0] if candidates else None


def _find_cover(save_path: Path) -> Path | None:
    """封面定位：优先 thumbnails/，其次根目录。"""
    for base in (save_path / "thumbnails", save_path):
        for name in ("cover.jpg", "cover.png", "cover.webp"):
            candidate = base / name
            if candidate.exists():
                return candidate
    return None


# ---------------------------------------------------------------------------
# 平台 → ArchiveBOT 服务调度表
# 文本类平台：每个条目 (service 模块名, service 类名, save 方法名)
# 产出 content.txt/md、images/；由 fetch_article 读取为 FetchedArticle。
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
        if platform == Platform.WECHAT:
            from app.archive.wechat_patch import _patch_wechat_fetch_page_html

            _patch_wechat_fetch_page_html(None)
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


# ---------------------------------------------------------------------------
# 视频类平台 → ArchiveBOT 服务调度表（Phase 2，M2 遗留）
# 产出 videos/video.mp4，由 fetch_video 读取为 VideoResult。
# 注意：TikTok 在 ArchiveBOT 尚无独立 service（tiktok service 未落地），保持未适配。
# ---------------------------------------------------------------------------

_VIDEO_DISPATCH: dict[Platform, tuple[str, str, str]] = {
    Platform.YOUTUBE: ("services.youtube_service", "YoutubeService", "save_video"),
    Platform.BILIBILI: ("services.bilibili_service", "BilibiliService", "save_video"),
    Platform.DOUYIN: ("services.douyin_service", "DouyinService", "save_video"),
    Platform.KUAISHOU: ("services.kuaishou_service", "KuaishouService", "save_video"),
    Platform.INSTAGRAM: ("services.instagram_service", "InstagramService", "save_video"),
}


def fetch_video(url: str, platform: Platform, task_dir: Path) -> VideoResult:
    """调用 ArchiveBOT 视频 service 抓取视频文件并返回标准化结果。

    未适配视频平台抛 FetchError(ErrorCode.UNKNOWN)。
    """
    entry = _VIDEO_DISPATCH.get(platform)
    if entry is None:
        raise FetchError(
            f"Video platform {platform.value} is not adapted yet",
            code=ErrorCode.UNKNOWN,
        )

    # 出网前装 requests 层守卫（幂等）
    ssrf_guard.ensure_installed()

    module_name, class_name, method_name = entry
    try:
        module = __import__(module_name, fromlist=[class_name])
        cls = getattr(module, class_name)
        service = cls(base_path=str(task_dir), create_date_folders=False)
        result = getattr(service, method_name)(url)
    except ImportError as e:
        logger.error("ArchiveBOT dependency missing for %s: %s", platform, e)
        raise FetchError("ArchiveBOT backend unavailable", code=ErrorCode.UNKNOWN) from e
    except ssrf_guard.BlockedHostError as e:
        logger.error("SSRF guard blocked fetch for %s: %s", platform.value, e)
        raise FetchError("Blocked internal/private address", code=ErrorCode.INVALID_URL) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("ArchiveBOT %s video failed: %s", platform, e)
        raise FetchError(_classify(platform, e), code=_classify_code(platform, e)) from e

    save_path = (result or {}).get("save_path")
    if not save_path:
        raise FetchError("ArchiveBOT returned no save_path", code=ErrorCode.EMPTY_CONTENT)
    return _from_video_save_result(save_path, url, platform.value, result)


def _from_video_save_result(save_path: str, source_url: str, platform: str, raw: dict) -> VideoResult:
    path = Path(save_path)
    meta = _read_json(path / "metadata.json")
    video_file = _find_video_file(path)
    if video_file is None:
        raise FetchError("ArchiveBOT saved no video file", code=ErrorCode.EMPTY_CONTENT)

    if raw is None:
        raw = {}
    upload_date = raw.get("upload_date") or meta.get("upload_date") or ""
    published = ""
    if len(upload_date) == 8 and upload_date.isdigit():
        published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return VideoResult(
        title=raw.get("title") or meta.get("title") or "",
        author=raw.get("channel") or raw.get("author_name") or meta.get("uploader") or "",
        sitename=platform,
        published_at=published,
        source_url=source_url,
        video_path=video_file,
        cover=_find_cover(path),
        save_path=path,
        duration=str(raw.get("duration") or meta.get("duration_string") or ""),
        video_id=str(raw.get("video_id") or meta.get("id") or ""),
    )


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
