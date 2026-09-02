"""特殊网站注册表（Bot/Web 共享）：Cookie 列表 + 到期时间 + 自动更新触发。

参考思想：ALL-VideoDownload-Plus 的多级回退 + 热更新，但在 ArchiveBot 中收口
为“白名单 + TTL + 提醒”。不绕过付费墙/访问控制，仅用于用户自己已登录的站点。
"""

from datetime import datetime, timezone

# 特殊网站：site_key → 配置。domains 用于匹配 url，ttl_days 用于会话 cookie 估算。
SPECIAL_SITES: dict[str, dict] = {
    "caixin": {
        "display_name": "财新网",
        "domains": [".caixin.com", "weekly.caixin.com"],
        "required": ["SA"],
        "ttl_days": 30,
        "platform": "web",
    },
    "wechat": {
        "display_name": "微信公众号",
        "domains": [".mp.weixin.qq.com", ".qq.com", ".tencent.com"],
        "required": ["hy_token"],
        "ttl_days": 7,
        "platform": "wechat",
    },
    "xhs": {
        "display_name": "小红书",
        "domains": [".xiaohongshu.com"],
        "required": [],
        "ttl_days": 30,
        "platform": "xhs",
    },
    "zhihu": {
        "display_name": "知乎",
        "domains": [".zhihu.com"],
        "required": ["z_c0"],
        "ttl_days": 60,
        "platform": "zhihu",
    },
}

# 提醒阈值（天）：≤7 天将过期，≤0 已过期
EXPIRING_DAYS = 7
EXPIRED_DAYS = 0

# 去重：同一域名下的重复 cookie 以最后一行为准（Netscape 文件导出顺序）
# 提醒去重：24 小时内不重复推送同一站点
REMINDER_COOLDOWN_SECONDS = 24 * 3600


def site_status(expires_at: datetime | None, now: datetime | None = None) -> tuple[str, int]:
    """返回 (status, days_left)。status: active/expiring/expired/unknown"""
    if expires_at is None:
        return "unknown", 9999
    now = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = (expires_at - now).days
    if days < EXPIRED_DAYS:
        return "expired", days
    if days <= EXPIRING_DAYS:
        return "expiring", days
    return "active", days


def site_badge(status: str) -> str:
    return {"active": "🟢", "expiring": "🟡", "expired": "🔴", "unknown": "⚪"}.get(status, "⚪")


def parse_netscape_expires(line: str) -> int | None:
    """解析 Netscape 第 5 列 expires（Unix 时间戳字符串）。0=会话 cookie。"""
    parts = line.split("\t")
    if len(parts) < 7:
        return None
    try:
        return int(parts[4])
    except ValueError:
        return None


def earliest_expires_from_netscape(raw_text: str, domain_filter: list[str] | None = None) -> datetime | None:
    """从 Netscape 文本中取最早的 expires（>0），用于站点过期时间。"""
    earliest: int | None = None
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0]
        if domain_filter and not any(domain.endswith(d.lstrip(".")) or domain == d for d in domain_filter):
            continue
        exp = parse_netscape_expires(line)
        if exp is None or exp == 0:
            continue
        if earliest is None or exp < earliest:
            earliest = exp
    if earliest is None:
        return None
    return datetime.fromtimestamp(earliest, tz=timezone.utc)
