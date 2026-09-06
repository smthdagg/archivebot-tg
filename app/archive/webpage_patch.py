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
    """读取 caixin 登录态 cookie。文件优先：渲染后的会话回写会更新文件，
    文件永远是最新的会话状态；settings（lru_cache）仅作兜底。"""
    settings = get_settings()
    path_str = settings.cookie_profiles_file
    if path_str:
        p = Path(path_str)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data.get("caixin"), dict):
                    inner = data["caixin"].get("caixin", [])
                    if isinstance(inner, list) and inner:
                        return inner
            except Exception:
                pass
    profiles = settings.cookie_profiles
    if "caixin" in profiles and "caixin" in profiles["caixin"]:
        return profiles["caixin"]["caixin"]
    return []


def normalize_caixin_url(url: str) -> str:
    """归一化财新 URL：桌面版单页结构最完整（cookie 全文 + 黄金图容器）。

    - /m/ 手机版路径 → 桌面版（手机模板无 article_media_pic 等容器，图片丢失；
      正文亦为 H5 分页变体）
    - 去掉 #pageN fragment 与 p0 参数（#page2 是图集分页，渲染结果只有
      1 图 + 试读正文）
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    path = re.sub(r"^(/m/)", "/", parts.path, count=1)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "p0"]
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


# Readability 解析：在克隆上剔除蜜罐/分页导航/AI 组件/重复标题/页内重复封面
# captions 参数 = 当前页提取到的媒体图注（桌面模板会把同一封面在正文内再渲染一份，
# 且那份是裸 <dl><dt><img><dd>，不带 media_pic 类，媒体提取拿不到，只能按图注匹配剔除）
_PARSE_WITH_CLEANUP = """
    (captions) => {
        var doc = document.cloneNode(true);
        doc.querySelectorAll('p.aitt').forEach(function (el) { el.remove(); });
        // AI 猜你想问组件
        doc.querySelectorAll('#questions_container').forEach(function (el) { el.remove(); });
        // 正文内重复的文章标题（#conTit，标题已由 content.md 头部承载）
        doc.querySelectorAll('#conTit').forEach(function (el) { el.remove(); });
        // 分页导航（隐藏的 li#purl* 列表与可见翻页链接）——拼接版不需要
        var navUl = doc.querySelector('li[id^="purl"]');
        if (navUl && navUl.parentElement) navUl.parentElement.remove();
        doc.querySelectorAll('a').forEach(function (a) {
            var t = (a.textContent || '').trim();
            if (t === '下一页' || t === '上一页' || t === '余下全文' || t === '本文导航') {
                var holder = a.closest('p') || a;
                holder.remove();
            }
        });
        // 页内重复封面：dl 内 img+图注 与已提取媒体图注一致 → 移除（由媒体合并回插）
        (captions || []).forEach(function (cap) {
            if (!cap) return;
            doc.querySelectorAll('dl').forEach(function (dl) {
                var dd = dl.querySelector('dd');
                var img = dl.querySelector('img');
                if (dd && img && (dd.textContent || '').trim() === cap) dl.remove();
            });
        });
        // 自引用图片链接（文末「阅读原文」式回链）整体移除
        doc.querySelectorAll('a[href] img').forEach(function (img) {
            var a = img.closest('a');
            if (a && a.href && location.pathname && a.href.indexOf(location.pathname) !== -1) {
                a.remove();
            }
        });
        var reader = new Readability(doc);
        return reader.parse();
    }
"""

# 提取分页链接（?p1..?pN，文档顺序）
_PAGE_URLS = """
    () => {
        const out = [];
        document.querySelectorAll('li[id^="purl"] a[href]').forEach(a => {
            // 当前页的导航项是 javascript:void(0)，跳过避免无效导航
            if (a.href && a.href.indexOf('javascript:') !== 0) out.push(a.href);
        });
        return out;
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


def _text_head(content_html: str, n: int = 60) -> str:
    """取内容片段的去空白文本前缀，用于判断翻页抓取是否返回了重复内容。"""
    from bs4 import BeautifulSoup

    text = BeautifulSoup(content_html, "html.parser").get_text(separator="")
    return re.sub(r"\s+", "", text)[:n]


def _dedupe_media(media: list[dict], seen: set[str]) -> list[dict]:
    """过滤已合并过的媒体（财新每个分页的模板都带同一张文章内封面）。"""
    out: list[dict] = []
    for m in media:
        src = (m.get("src") or "").split("#")[0]
        if not src or src in seen:
            continue
        seen.add(src)
        out.append(m)
    return out


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
        # vendor 下载图片后会把 src 改写为本地路径，原始远程 URL 留在
        # data-original-src 供交付 MD 时把本地引用改回远程
        img["data-original-src"] = src
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


def _persist_session_cookies(cookie_list: list[dict]) -> None:
    """把渲染会话的最新 cookie 回写到 cookie profile 文件。

    财新会滚动刷新登录 token（用户浏览器无感自动接受新值），静态快照会
    因此失效；回写让服务端 token 更新时本地自动跟随（与用户浏览器同机制）。
    只合并 .caixin.com 域条目，不触碰其他站点配置。worker 单进程串行消费，
    写文件无并发问题。
    """
    settings = get_settings()
    path_str = settings.cookie_profiles_file
    if not path_str:
        return
    p = Path(path_str)
    if not p.exists():
        return
    latest = {
        c["name"]: c
        for c in cookie_list
        if ".caixin.com" in (c.get("domain") or "")
    }
    if not latest:
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    inner = data.get("caixin", {}).get("caixin")
    if not isinstance(inner, list) or not inner:
        return
    updated = 0
    for c in inner:
        new = latest.get(c.get("name"))
        if new and str(c.get("value", "")) != str(new.get("value", "")):
            c["value"] = str(new.get("value", ""))
            if new.get("domain"):
                c["domain"] = new["domain"]
            updated += 1
    known = {c.get("name") for c in inner}
    for name, new in latest.items():
        if name not in known:
            inner.append({
                "name": name,
                "value": str(new.get("value", "")),
                "domain": new.get("domain", ".caixin.com"),
                "path": new.get("path", "/"),
            })
            updated += 1
    if updated:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("caixin: persisted %d session cookie update(s)", updated)


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

        # 重写版：带 cookie 注入的反检测浏览器流程（Patchright 优先）

        from app.archive.stealth import STEALTH_ARGS, get_async_playwright
        from vendor.ArchiveBOT.services.webpage_service import _READABILITY_JS  # noqa: PLC0415

        readability_src = _READABILITY_JS.read_text(encoding="utf-8")

        async_playwright, engine = get_async_playwright()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=STEALTH_ARGS)
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
            # 注意：Patchright 的 evaluate 在隔离世界执行，主世界全局（如
            # add_script_tag/add_init_script 定义的 Readability）不可见。
            # 因此解析时把库源码与解析脚本拼进同一次 evaluate（见下）。

            page = await context.new_page()

            async def _render_current_page() -> tuple[dict | None, list[dict]]:
                """渲染当前页：滚动触发懒加载 → Readability（蜜罐/导航剔除）。

                媒体块只提取不合并——由调用方跨页去重后再按锚点合并，
                否则每个分页模板里的同一张封面图会重复插入。
                """
                await self._goto_with_fallbacks(page, url_current)
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
                # 登录态检测：cookie 过期时页面显示「登录/注册」而非「退出」，
                # 此时只能拿到试读版——明确报 LOGIN_REQUIRED 提示重新导出 cookie，
                # 而不是静默交付残缺内容
                login_state = await page.evaluate(
                    """() => {
                        const box = document.querySelector('#showLoginId');
                        const t = box ? (box.innerText || '').trim() : '';
                        return {logged_in: t.includes('退出'), box: t.slice(0, 20)};
                    }"""
                )
                if not login_state.get("logged_in"):
                    # 不在此处抛错：vendor save_page 会捕获并回退到匿名静态提取，
                    # 失效信号被吞。改为打标志，由 _save_page_patched 在拿到
                    # 「试读版结果」后统一抛 LOGIN_REQUIRED（穿透 vendor 吞异常）。
                    logger.warning(
                        "caixin: login state invalid (box=%r), cookies expired?",
                        login_state.get("box"),
                    )
                    self._caixin_login_invalid = True
                    return None, []
                # Readability 已由 context.add_init_script 注入（导航自动生效）
                # 先提取媒体（图注供克隆清理匹配页内重复封面），再解析正文
                media_n: list[dict] = []
                try:
                    media_n = await page.evaluate(_MEDIA_EXTRACT)
                except Exception:  # noqa: BLE001 - 媒体提取失败不影响正文
                    logger.exception("caixin: media extract failed")
                captions = [m.get("caption") or "" for m in media_n]
                # 库源码与解析函数拼进同一次 evaluate：Patchright 的 evaluate
                # 运行在隔离世界，看不到页面/主世界注入的全局 Readability
                parse_js = (
                    "(function(captions) {\n"
                    + readability_src
                    + "\n;\nreturn ("
                    + _PARSE_WITH_CLEANUP.strip()
                    + ")(captions);\n})"
                )
                art = await page.evaluate(parse_js, captions)
                return art, media_n

            try:
                url_current = url
                seen_media: set[str] = set()
                article: dict | None = None
                media: list[dict] = []
                article, media = await _render_current_page()
                media = _dedupe_media(media, seen_media)
                if article and article.get("content") and media:
                    article["content"] = _merge_media(article["content"], media)
                    logger.info("caixin: merged %d media blocks (page 1)", len(media))

                # 长文分页：财新把多页文章拆成 ?p1..?pN（隐藏导航 li#purl*），
                # 逐页用同一浏览器上下文（cookie 保持）渲染后按顺序拼接正文
                try:
                    page_urls = await page.evaluate(_PAGE_URLS)
                except Exception:  # noqa: BLE001
                    page_urls = []
                if page_urls:
                    logger.info("caixin: detected %d extra pages", len(page_urls))
                seen_pages = {url}
                parts = [(article or {}).get("content") or ""]
                for page_url in page_urls[:30]:
                    # 双保险：过滤非 http(s) 的分页链接（当前页导航项是 javascript:void）
                    if not page_url.startswith(("http://", "https://")):
                        continue
                    if page_url in seen_pages:
                        continue
                    seen_pages.add(page_url)
                    url_current = page_url
                    try:
                        art_n, media_n = await _render_current_page()
                    except Exception:  # noqa: BLE001 - 单页失败跳过，不弃全文
                        logger.exception("caixin: page fetch failed: %s", page_url)
                        continue
                    media_n = _dedupe_media(media_n, seen_media)
                    content_n = (art_n or {}).get("content") or ""
                    if not content_n:
                        continue
                    if media_n:
                        content_n = _merge_media(content_n, media_n)
                    # 防重复：?pN 被服务端忽略时会返回与上一页相同的内容
                    if _text_head(content_n) == _text_head(parts[-1]):
                        logger.info("caixin: page %s identical to previous, stop", page_url)
                        break
                    parts.append(content_n)
                    if media_n:
                        logger.info("caixin: page %s merged %d media", page_url, len(media_n))
                if len(parts) > 1 and article is not None:
                    article["content"] = "".join(parts)
                    logger.info("caixin: merged %d pages into content", len(parts))
            finally:
                # 会话回写：财新会滚动刷新登录 token（浏览器无感自动更新），
                # 静态快照因此失效。渲染成功且已登录时，把 context 里的最新
                # cookie 写回 profile 文件，让服务端 token 更新时自动跟随
                # （与用户浏览器同一机制）。
                try:
                    if article and article.get("content"):
                        await _persist_session_cookies(await context.cookies())
                except Exception:  # noqa: BLE001 - 回写失败不影响本次结果
                    logger.exception("caixin: session cookie persist failed")
                await browser.close()
            return article

    def _save_page_patched(self, page_url: str) -> dict:
        # 登录失效标志复位（本页检测在 _patched 内设置）
        login_invalid = getattr(self, "_caixin_login_invalid", False)
        self._caixin_login_invalid = False
        result = orig_save_page(self, page_url)
        if login_invalid:
            # vendor 把登录失效吞掉后回退到了匿名静态提取（试读版）——
            # 在这里把 LOGIN_REQUIRED 抛出去，阻止静默交付残缺内容
            from app.archive.fetcher import FetchError
            from app.database.enums import ErrorCode

            raise FetchError(
                "Caixin cookies expired, please re-export cookie profile",
                code=ErrorCode.LOGIN_REQUIRED,
            )
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
