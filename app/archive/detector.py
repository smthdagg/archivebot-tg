"""平台识别（设计规格 §6/§53）。

detect(url) -> Platform
基于主机名识别，覆盖 ArchiveBOT 已支持的主流平台；通用网页回退 WEB。
"""

import re
from urllib.parse import urlparse

from app.database.enums import Platform

# 域名 → 平台映射（按主机名后缀匹配，注意子域顺序）
_HOST_RULES: list[tuple[re.Pattern, Platform]] = [
    (re.compile(r"(^|\.)mp\.weixin\.qq\.com$"), Platform.WECHAT),
    (re.compile(r"(^|\.)weixin\.qq\.com$"), Platform.WECHAT),
    (re.compile(r"(^|\.)(twitter|x|mobile\.twitter|m\.twitter|fxtwitter|fixupx|nitter)\.com$"), Platform.TWITTER),
    (re.compile(r"(^|\.)xiaohongshu\.com$"), Platform.XHS),
    (re.compile(r"xhslink\.(com|[a-z]{2,6})$"), Platform.XHS),
    (re.compile(r"(^|\.)weibo\.(com|cn)$"), Platform.WEIBO),
    (re.compile(r"(^|\.)zhihu\.com$"), Platform.ZHIHU),
    (re.compile(r"(^|\.)reddit\.com$"), Platform.REDDIT),
    (re.compile(r"(^|\.)(youtube|youtu)\.(com|be)$"), Platform.YOUTUBE),
    (re.compile(r"(^|\.)bilibili\.com$"), Platform.BILIBILI),
    (re.compile(r"(^|\.)b23\.tv$"), Platform.BILIBILI),
    (re.compile(r"(^|\.)douyin\.com$"), Platform.DOUYIN),
    (re.compile(r"(^|\.)iesdouyin\.com$"), Platform.DOUYIN),
    (re.compile(r"(^|\.)kuaishou\.com$"), Platform.KUAISHOU),
    (re.compile(r"(^|\.)instagram\.com$"), Platform.INSTAGRAM),
    (re.compile(r"(^|\.)threads\.net$"), Platform.THREADS),
    (re.compile(r"(^|\.)pinterest\.(com|[a-z]{2,6})$"), Platform.PINTEREST),
    (re.compile(r"(^|\.)feishu\.cn$"), Platform.FEISHU),
]

# 平台显示名（i18n key）
PLATFORM_I18N = {
    Platform.WECHAT: "platform.wechat",
    Platform.TWITTER: "platform.twitter",
    Platform.XHS: "platform.xhs",
    Platform.WEIBO: "platform.weibo",
    Platform.ZHIHU: "platform.zhihu",
    Platform.REDDIT: "platform.reddit",
    Platform.YOUTUBE: "platform.youtube",
    Platform.BILIBILI: "platform.bilibili",
    Platform.DOUYIN: "platform.douyin",
    Platform.KUAISHOU: "platform.kuaishou",
    Platform.INSTAGRAM: "platform.instagram",
    Platform.THREADS: "platform.threads",
    Platform.PINTEREST: "platform.pinterest",
    Platform.FEISHU: "platform.feishu",
    Platform.WEB: "platform.web",
    Platform.UNKNOWN: "platform.unknown",
}


def detect(url: str) -> Platform:
    """识别 URL 所属平台；无法识别时返回 WEB（通用网页）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return Platform.UNKNOWN
    if not host:
        return Platform.UNKNOWN
    for pattern, platform in _HOST_RULES:
        if pattern.search(host):
            return platform
    return Platform.WEB


def extract_first_url(text: str) -> str | None:
    """从任意文本中提取第一个 http/https 链接（用于消息中夹带 URL 的场景）。"""
    match = re.search(r"https?://[^\s<>\"']+", text)
    return match.group(0).rstrip(".,;!?)]}") if match else None


def platform_label_key(platform: Platform) -> str:
    return PLATFORM_I18N.get(platform, PLATFORM_I18N[Platform.UNKNOWN])
