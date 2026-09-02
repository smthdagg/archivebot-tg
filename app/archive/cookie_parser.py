"""Netscape/JSON cookie 解析 + 特殊网站过滤（去重、到期计算）。

供 Bot 命令与 Web 后台共用。敏感值不记日志。
"""


from app.archive.cookie_registry import SPECIAL_SITES


def parse_netscape(raw_text: str, domain_filter: list[str] | None = None) -> list[dict]:
    """从 Netscape 文本解析为 Cookie-Editor 格式列表（去重，最后一行覆盖）。"""
    seen: dict[str, dict] = {}
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
        name, value = parts[5], parts[6]
        if not name:
            continue
        key = f"{domain}|{name}|{parts[2]}"
        try:
            expires = int(parts[4])
        except ValueError:
            expires = 0
        seen[key] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": parts[2],
            "expires": expires,
        }
    # 按 domain+name 去重后转为 Cookie-Editor 格式
    result = []
    for v in seen.values():
        result.append({
            "name": v["name"],
            "value": v["value"],
            "domain": v["domain"],
            "path": v["path"],
        })
    return result


def extract_cookie_header(raw_text: str) -> tuple[str, int]:
    """从 Netscape 或粘贴的 Cookie: 文本提取 Cookie header 值。"""
    pairs = []
    for line in raw_text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            domain, name, value = fields[0], fields[5], fields[6]
            if (
                domain.endswith("tencent.com")
                or domain.endswith("qq.com")
                or domain.endswith("caixin.com")
                or domain.endswith("xiaohongshu.com")
                or domain.endswith("zhihu.com")
            ):
                pairs.append(f"{name}={value}")
    if pairs:
        return "; ".join(pairs), len(pairs)
    # 粘贴的 Cookie: Header
    text = raw_text.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if "=" not in text:
        return "", 0
    pair_count = len([part for part in text.split(";") if "=" in part])
    return text, pair_count


def to_cookie_profiles_raw(cookies: list[dict], site_key: str) -> dict:
    """把解析出的 cookies 按 site_key 转为 COOKIE_PROFILES 的 JSON 结构。"""
    cfg = SPECIAL_SITES.get(site_key)
    if not cfg:
        return {}
    platform = cfg.get("platform", site_key)
    return {site_key: {platform: cookies}}


def site_domains(site_key: str) -> list[str]:
    cfg = SPECIAL_SITES.get(site_key, {})
    return list(cfg.get("domains", []))


def validate_required(cookies: list[dict], site_key: str) -> list[str]:
    """检查必需的 cookie name 是否存在，返回缺失列表。"""
    cfg = SPECIAL_SITES.get(site_key, {})
    required = cfg.get("required", [])
    names = {c.get("name") for c in cookies}
    return [r for r in required if r not in names]
