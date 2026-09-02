"""Bot 侧 Cookie 管理：/cookies 列表 + /set_cookie 更新。

仅 ADMIN_IDS 可操作。上传的 cookies.txt 含敏感值，处理后删除消息。
"""

import json
import logging
import tempfile
from pathlib import Path

from aiogram import Router, types
from aiogram.filters import Command

from app.archive.cookie_expiry import admin_cookie_list_items
from app.archive.cookie_parser import parse_netscape, site_domains, validate_required
from app.archive.cookie_registry import SPECIAL_SITES, earliest_expires_from_netscape
from app.config import get_settings
from app.database.database import SessionLocal
from app.database.enums import AuditAction
from app.database.services import audit

logger = logging.getLogger(__name__)

router = Router(name="cookies")


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids


def _get_cookie_file_text() -> str:
    path_str = get_settings().cookie_profiles_file
    if not path_str:
        return ""
    p = Path(path_str)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _write_cookie_file(site_key: str, cookies: list[dict]) -> None:
    """把 cookies 写入 COOKIE_PROFILES_FILE 的 JSON（按 site_key 合并）。"""
    settings = get_settings()
    path_str = settings.cookie_profiles_file or "data/cookie_profiles.json"
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    # 特殊网站的 platform
    platform = SPECIAL_SITES.get(site_key, {}).get("platform", site_key)
    # 合并：profile 名 = site_key（简化：每个站点一个 profile，同名）
    if site_key not in data:
        data[site_key] = {}
    data[site_key][platform] = cookies
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同步更新 .env 的 COOKIE_PROFILES_FILE 指向
    if not settings.cookie_profiles_file:
        _update_env_cookie_file(path_str)


def _update_env_cookie_file(path_str: str) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    found = False
    out = []
    for line in lines:
        if line.startswith("COOKIE_PROFILES_FILE="):
            out.append(f"COOKIE_PROFILES_FILE={path_str}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"COOKIE_PROFILES_FILE={path_str}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


@router.message(Command("cookies"))
async def cookies_list(message: types.Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ 仅管理员可用")
        return
    items = admin_cookie_list_items()
    if not items:
        await message.answer("📋 暂无特殊网站配置")
        return
    lines = ["📋 Cookie 站点列表"]
    for it in items:
        exp_str = it["expires_at"].strftime("%Y-%m-%d") if it["expires_at"] else "—"
        days = f"剩余 {it['days_left']} 天" if it["status"] != "unknown" else "未配置"
        lines.append(
            f"{it['badge']} {it['display_name']}（{it['site_key']}） {it['count']} 条\n"
            f"   状态 {it['status']} · 过期 {exp_str} · {days}"
        )
    lines.append("\n更新：回复 cookies.txt 文件并执行 /set_cookie <site>")
    await message.answer("\n".join(lines))


@router.message(Command("set_cookie"))
async def set_cookie(message: types.Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ 仅管理员可用")
        return
    # 解析参数
    parts = (message.text or "").split(maxsplit=1)
    site_key = parts[1].strip().lower() if len(parts) > 1 else ""
    # 支持回复文件/文本
    raw_text = ""
    reply = message.reply_to_message
    secret_ids = [message.message_id]
    try:
        if reply and getattr(reply, "document", None):
            if reply.document.file_size and reply.document.file_size > 1024 * 1024:
                await message.answer("❌ 文件过大（>1MB）")
                return
            if reply.message_id:
                secret_ids.append(reply.message_id)
            # 下载文件（Bot 侧）
            # 用 message.bot 下载
            bot = message.bot
            file = await bot.get_file(reply.document.file_id)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                tmp_path = tmp.name
            await bot.download_file(file.file_path, destination=tmp_path)
            raw_text = Path(tmp_path).read_text(encoding="utf-8", errors="ignore")
            Path(tmp_path).unlink(missing_ok=True)
        elif reply and (getattr(reply, "text", None) or getattr(reply, "caption", None)):
            if reply.message_id:
                secret_ids.append(reply.message_id)
            raw_text = reply.text or reply.caption or ""
        elif site_key and "cookie" in site_key.lower():
            # 直接粘贴 Cookie: 文本作为 site_key 的情况
            raw_text = site_key
            site_key = ""

        if not site_key:
            avail = ", ".join(SPECIAL_SITES.keys())
            await message.answer(f"用法：回复 cookies.txt 文件，发送 /set_cookie <site>\n可用站点：{avail}")
            return
        if site_key not in SPECIAL_SITES:
            await message.answer(f"❌ 未知站点 {site_key!r}，可用：{', '.join(SPECIAL_SITES.keys())}")
            return
        if not raw_text or not raw_text.strip():
            await message.answer("❌ 未获取到 Cookie 内容，请回复 cookies.txt 文件或粘贴 Cookie: 文本")
            return
        domains = site_domains(site_key)
        cookies = parse_netscape(raw_text, domains)
        # 若 Netscape 解析为空，尝试按粘贴的 Cookie: 头解析
        if not cookies:
            # 尝试通用解析
            text = raw_text.strip()
            if text.lower().startswith("cookie:"):
                text = text.split(":", 1)[1].strip()
            # 按 ; 分割
            for part in text.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name, value = name.strip(), value.strip()
                if name and value:
                    cookies.append({
                        "name": name, "value": value,
                        "domain": domains[0] if domains else ".example.com",
                        "path": "/",
                    })
        if not cookies:
            await message.answer("❌ 未解析到有效 Cookie，请检查文件格式（Netscape cookies.txt 或 Cookie: 头）")
            return
        # 校验必需项
        missing = validate_required(cookies, site_key)
        if missing:
            await message.answer(f"⚠️ 已解析 {len(cookies)} 条，但缺少必需项 {missing}，已写入但可能无法生效")
        # 写入
        _write_cookie_file(site_key, cookies)
        # 刷新内存（清 lru_cache）
        try:
            get_settings.cache_clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        # 审计（不记 value）
        db = SessionLocal()
        try:
            # 取最早过期
            raw_for_exp = raw_text
            expires_at = earliest_expires_from_netscape(raw_for_exp, domains)
            exp_str = expires_at.strftime("%Y-%m-%d") if expires_at else "—"
            audit(db, action=AuditAction.COOKIE_UPDATED, operator_user_id=message.from_user.id,
                  target_type="cookie_site", target_id=site_key,
                  details={"count": len(cookies), "expires_at": exp_str, "missing": missing})
            db.commit()
        finally:
            db.close()
        # 清理敏感消息
        try:
            await message.bot.delete_messages(message.chat.id, secret_ids)
        except Exception:
            pass
        await message.answer(f"✅ {SPECIAL_SITES[site_key]['display_name']} 已更新 {len(cookies)} 条，过期 {exp_str}")
        logger.info("cookie site %s updated by %s: %d cookies", site_key, message.from_user.id, len(cookies))
    except Exception as e:
        logger.exception("set_cookie failed: %s", e)
        await message.answer(f"❌ 更新失败：{e}")
        try:
            await message.bot.delete_messages(message.chat.id, secret_ids)
        except Exception:
            pass
