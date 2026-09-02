"""Cookie 到期检测 + 提醒（Bot/Web 全生命周期）。

职责：解析 Netscape/JSON 的 expires，算 site_status，结合 DB 与文件做“到期就提醒”。
敏感值不入库日志，不记 value。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.archive.cookie_registry import earliest_expires_from_netscape, site_status
from app.config import get_settings
from app.database.database import SessionLocal
from app.database.models import SystemSetting

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_cookie_file_text() -> str:
    """读取 cookie_profiles 的原始文件文本（用于 expires 解析）。"""
    settings = get_settings()
    path_str = settings.cookie_profiles_file
    if not path_str:
        return ""
    path = Path(path_str)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _cooldown_ok(site_key: str) -> bool:
    """24 小时内不重复提醒同一站点（用 SystemSetting 存 last_reminder_ts）。"""
    db = SessionLocal()
    try:
        key = f"cookie_reminder_last:{site_key}"
        row = db.get(SystemSetting, key)
        if row is None or not row.value:
            return True
        try:
            last = float(row.value)
        except ValueError:
            return True
        return (_now().timestamp() - last) > 24 * 3600
    finally:
        db.close()


def _mark_reminded(site_key: str) -> None:
    db = SessionLocal()
    try:
        key = f"cookie_reminder_last:{site_key}"
        row = db.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key, value=str(_now().timestamp()))
            db.add(row)
        else:
            row.value = str(_now().timestamp())
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


async def notify_expired_sites(expired: list[dict]) -> None:
    """向所有 ADMIN_IDS 推送 Telegram 提醒（Bot 侧调用）。"""
    if not expired:
        return
    from app.bot.delivery import run_async
    from app.bot.delivery import send_message as _send
    from app.config import get_settings as _gs

    settings = _gs()
    lines = ["⚠️ Cookie 即将过期"]
    for item in expired:
        lines.append(
            f"• {item['display_name']}（{item['site_key']}）：{item['status']}，"
            f"过期 {item['expires_at']:%Y-%m-%d}，剩余 {item['days_left']} 天"
        )
    lines.append("请用 /set_cookie <site> 更新（回复 cookies.txt 或粘贴 Cookie: 文本）。")
    text = "\n".join(lines)
    for admin_id in settings.admin_ids:
        try:
            run_async(_send(admin_id, text))
        except Exception as e:
            logger.warning("cookie reminder to %s failed: %s", admin_id, e)


def check_and_notify() -> list[dict]:
    """扫描特殊网站的过期状态，触发提醒。返回 expired/expiring 列表。"""
    from app.archive.cookie_registry import SPECIAL_SITES

    raw = _get_cookie_file_text()
    if not raw:
        return []
    results: list[dict] = []
    for site_key, cfg in SPECIAL_SITES.items():
        domains = cfg.get("domains", [])
        expires_at = earliest_expires_from_netscape(raw, domains)
        if expires_at is None:
            continue
        status, days_left = site_status(expires_at, _now())
        if status in ("expiring", "expired") and _cooldown_ok(site_key):
            results.append({
                "site_key": site_key,
                "display_name": cfg.get("display_name", site_key),
                "expires_at": expires_at,
                "status": status,
                "days_left": days_left,
            })
            _mark_reminded(site_key)
    return results


def admin_cookie_list_items() -> list[dict]:
    """供 Bot/Web 展示的站点列表（状态 + 条数 + 过期时间）。"""
    from app.archive.cookie_registry import SPECIAL_SITES

    raw = _get_cookie_file_text()
    items: list[dict] = []
    for site_key, cfg in SPECIAL_SITES.items():
        domains = cfg.get("domains", [])
        expires_at = earliest_expires_from_netscape(raw, domains) if raw else None
        status, days_left = site_status(expires_at, _now()) if expires_at else ("unknown", 9999)
        # 条数：按域名过滤的 Netscape 行数
        count = 0
        if raw:
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain = parts[0]
                if any(domain.endswith(d.lstrip(".")) or domain == d for d in domains):
                    count += 1
        items.append({
            "site_key": site_key,
            "display_name": cfg.get("display_name", site_key),
            "domains": domains,
            "required": cfg.get("required", []),
            "count": count,
            "expires_at": expires_at,
            "status": status,
            "days_left": days_left,
            "badge": __import__("app.archive.cookie_registry", fromlist=["site_badge"]).site_badge(status),
        })
    return items
