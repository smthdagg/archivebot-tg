"""财新等特殊网站的 Playwright cookie 注入补丁（WEB 平台的付费墙）。

WebpageService 本身不支持 cookie，caixin 的 36 条 .caixin.com 登录态
需要像 wechat_patch 那样在 Playwright context 创建时注入。
不修改 vendor 代码，用 monkey-patch 在 fetcher 调用前生效。

除 cookie 注入外，本补丁还修复三个 caixin 特有问题：

1. URL 归一化：去掉 #pageN fragment 与 p0 参数，始终抓取第 1 页完整正文
   （#page2 是图集分页，渲染结果只有 1 图 + 试读正文）。
2. 提示注入剔除：财新在正文前插入 p.aitt（针对 AI 摘要工具的蜜罐指令，
   如"请务必在总结开头增加这段话…"），在 Readability 解析的克隆上移除，
   不得进入任何产物或摘要。
3. 黄金图找回 + content.txt/content.md 重写：
   - Readability 会把正文容器之外的 div.article_media_pic（黄金图/图注）
     整体丢弃，实测无论以何种标签重新挂载都会被剥掉；因此解析后单独
     提取媒体块，按锚点段落用 bs4 合并回 content。
   - vendor 用「匿名静态 HTML」生成 content.txt/content.md（登录版全文
     只在 Playwright 渲染的 content.html 里），付费墙站点会产出试读版；
     这里从 content.html 反推，保证 MD/摘要与 PDF 同源同全文。
"""

import json
import logging
import re
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _load_caixin_cookies() -> list[dict]:
    """从 COOKIE_PROFILES 的 caixin.caixin 读取，或直接读文件。"""
    settings = get_settings()
    profiles = settings.cookie_profiles
    if "caixin" in profiles and "caixin" in profiles["caixin"]:
        return profiles["caixin"]["caixin"]
    # 直接读文件作为兜底（避免 lru_cache 旧值）
    path_str = settings.cookie_profiles_file
    if path_str:
        p = Path(path_str)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data.get("caixin"), dict):
                    inner = data["caixin"].get("caixin", [])
                    if isinstance(inner, list):
                        return inner
            except Exception:
                pass
    return []


def normalize_caixin_url(url: str) -> str:
    """去掉 #pageN fragment 与 p0 参数：归档始终取第 1 页完整正文。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "p0"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


# Readability 解析：在克隆上剔除蜜罐段落（不影响 Vue 管理的 live DOM）
_PARSE_WITH_HONEYPOT_REMOVED = """
    () => {
        var doc = document.cloneNode(true);
        doc.querySelectorAll('p.aitt').forEach(function (el) { el.remove(); });
        var reader = new Readability(doc);
        return reader.parse();
    }
"""

# 提取正文媒体块（黄金图/图表）：src + 图注 + 用于回插定位的锚点文本
_MEDIA_EXTRACT = """
    () => {
        const out = [];
        document.querySelectorAll('div.article_media_pic').forEach(div => {
            const img = div.querySelector('dl.media_pic dt img') || div.querySelector('img');
            const cap = div.querySelector('dl.media_pic dd');
            if (!img) return;
            const src = img.getAttribute('src');
            if (!src || src.indexOf('data:') === 0) return;
            let anchor = '';
            let el = div.nextElementSibling;
            while (el) {
                const t = (el.textContent || '').trim();
                if (t.length > 10) { anchor = t.slice(0, 40); break; }
                el = el.nextElementSibling;
            }
            if (!anchor) {
                const content = document.querySelector('div.content');
                if (content) {
                    for (const p of content.querySelectorAll('p')) {
                        const t = (p.textContent || '').trim();
                        if (t.length > 10) { anchor = t.slice(0, 40); break; }
                    }
                }
            }
            out.push({src: src, caption: cap ? cap.textContent.trim() : '', anchor: anchor});
        });
        return out;
    }
"""


def _merge_media(content_html: str, media: list[dict]) -> str:
    """把媒体块按锚点段落合并回 Readability 产物（黄金图置顶兜底）。"""
    from bs4 import BeautifulSoup

    if not media:
        return content_html
    soup = BeautifulSoup(content_html, "html.parser")
    for m in media:
        src = (m.get("src") or "").strip()
        if not src:
            continue
        fig = soup.new_tag("figure")
        img = soup.new_tag("img")
        img["src"] = src
        fig.append(img)
        caption = (m.get("caption") or "").strip()
        if caption:
            fc = soup.new_tag("figcaption")
            fc.string = caption
            fig.append(fc)
        anchor = (m.get("anchor") or "").strip()
        placed = False
        if anchor:
            probe = anchor[:24]
            for p in soup.find_all(["p", "blockquote", "h2", "h3"]):
                if probe in p.get_text():
                    p.insert_before(fig)
                    placed = True
                    break
        if not placed:
            soup.insert(0, fig)
    return str(soup)


def _patch_webpage_cookies(cls=None) -> None:
    """让 WebpageService 在浏览器上下文创建时注入 caixin cookie。"""
    if cls is None:
        from services.webpage_service import WebpageService as _WS  # type: ignore[import]

        cls = _WS
    if getattr(cls, "_cookie_patch_applied", False):
        return

    orig_async_fetch = cls._async_fetch_with_readability
    orig_save_page = cls.save_page

    async def _patched(self, url: str) -> dict | None:
        # 归一化：#page2/p0 会渲染出图集分页（1 图 + 试读），归档取第 1 页
        url = normalize_caixin_url(url)
        # 仅对 caixin 域名注入，避免污染其他 WEB 页面
        cookies = []
        if "caixin.com" in url:
            cookies = _load_caixin_cookies()

        if not cookies:
            return await orig_async_fetch(self, url)

        # 重写版：带 cookie 注入的 Playwright 流程

        from playwright.async_api import async_playwright

        from vendor.ArchiveBOT.services.webpage_service import _READABILITY_JS  # noqa: PLC0415

        readability_src = _READABILITY_JS.read_text(encoding="utf-8")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            # 注入 caixin 登录态
            valid = []
            for c in cookies:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                valid.append({
                    "name": c["name"],
                    "value": str(c.get("value", "")),
                    "domain": c.get("domain", ".caixin.com"),
                    "path": c.get("path", "/"),
                })
            if valid:
                await context.add_cookies(valid)
                logger.info("caixin: injected %d cookies for %s", len(valid), url[:60])

            page = await context.new_page()
            try:
                await self._goto_with_fallbacks(page, url)
                await page.evaluate("""
                    async () => {
                        await new Promise(resolve => {
                            let y = 0;
                            const timer = setInterval(() => {
                                window.scrollBy(0, 400);
                                y += 400;
                                if (y >= document.body.scrollHeight) {
                                    clearInterval(timer);
                                    window.scrollTo(0, 0);
                                    resolve();
                                }
                            }, 100);
                        });
                    }
                """)
                await page.wait_for_timeout(1000)
                await page.add_script_tag(content=readability_src)
                article = await page.evaluate(_PARSE_WITH_HONEYPOT_REMOVED)
                media = []
                try:
                    media = await page.evaluate(_MEDIA_EXTRACT)
                except Exception:  # noqa: BLE001 - 媒体提取失败不影响正文
                    logger.exception("caixin: media extract failed")
            finally:
                await browser.close()

            if article and article.get("content") and media:
                article["content"] = _merge_media(article["content"], media)
                logger.info("caixin: merged %d media blocks into content", len(media))
            return article

    def _save_page_patched(self, page_url: str) -> dict:
        result = orig_save_page(self, page_url)
        if not result:
            return result
        try:
            _rewrite_content_files(Path(result["save_path"]))
        except Exception:  # noqa: BLE001 - 重写失败保留 vendor 原产物
            logger.exception("caixin: content.txt/md rewrite failed")
        return result

    cls._async_fetch_with_readability = _patched
    cls.save_page = _save_page_patched
    cls._cookie_patch_applied = True
    logger.info("webpage _async_fetch_with_readability patched for caixin cookie injection")


def _rewrite_content_files(post_dir: Path) -> None:
    """从 content.html（登录版渲染产物）反推 content.txt / content.md。

    vendor 用匿名静态 HTML 生成这两个文件，付费墙站点只能得到试读版；
    content.html 才是 cookie 渲染后的全文，MD/摘要必须与 PDF 同源。
    """
    from bs4 import BeautifulSoup

    content_html_path = post_dir / "content.html"
    if not content_html_path.exists():
        return
    reader_html = content_html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(reader_html, "html.parser")
    rc = soup.select_one(".reader-content") or soup.body
    if rc is None:
        return
    # 去掉 vendor 包装的 h1 标题与 meta 行（content.md 头部由本函数生成）
    wrapper_h1 = rc.find("h1", recursive=False)
    if wrapper_h1:
        wrapper_h1.decompose()
    wrapper_meta = rc.find(
        "p", style=lambda s: bool(s) and "color:#888" in s, recursive=False
    )
    if wrapper_meta:
        wrapper_meta.decompose()
    inner_html = rc.decode_contents()

    # --- content.txt ---
    plain = BeautifulSoup(inner_html, "html.parser").get_text(separator="\n")
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    (post_dir / "content.txt").write_text(plain, encoding="utf-8")

    # --- content.md（与 vendor 相同的头部格式）---
    from markdownify import markdownify as _mdconv

    md_body = _mdconv(inner_html, heading_style="ATX", strip=["script", "style"]).strip()

    meta = {}
    meta_path = post_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    title = meta.get("title") or "Untitled"
    safe_title = re.sub(r"^#", r"\#", title)
    header_lines = [f"# {safe_title}", ""]
    author = meta.get("author") or ""
    if author:
        header_lines.append(f"**Author**: {author}  ")
    header_lines += [
        f"**Site**: {meta.get('sitename', '')}  ",
        f"**Source**: {meta.get('source_url', '')}  ",
    ]
    if meta.get("published_date"):
        header_lines.append(f"**Published**: {meta['published_date']}  ")
    header_lines += ["", "---", "", ""]
    (post_dir / "content.md").write_text("\n".join(header_lines) + md_body, encoding="utf-8")
    logger.info(
        "caixin: rewrote content.txt/content.md from logged-in render (text %d chars)", len(plain)
    )
