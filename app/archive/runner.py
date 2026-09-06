"""任务执行编排（设计规格 §53 的 worker 内部分）。

流程：抓取 → 清洗 → 图片 → Markdown → PDF → 三行摘要 → metadata.json。
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from app.archive import cleaner, excerpt
from app.archive import images as images_mod
from app.archive import markdown as markdown_mod
from app.archive import pdf as pdf_mod
from app.archive.fetcher import FetchError, fetch_article
from app.archive.types import ArchiveResult
from app.database.enums import ErrorCode, OutputType, Platform, TaskStatus

logger = logging.getLogger(__name__)


def run_archive(
    *,
    task_dir: Path,
    url: str,
    platform: Platform,
    output_types: list[OutputType],
    archive_time: datetime | None = None,
    cookie_profile: str | None = None,
    on_status: Callable[[TaskStatus], None] | None = None,
) -> ArchiveResult:
    """执行一次归档，产出到 task_dir，返回结果描述。

    cookie_profile：可选的 Cookie Profile 名（登录类网站，Phase 2）。
    仅当任务显式指定时才会向 ArchiveBOT 注入 cookie。

    on_status：可选进度回调，收到各阶段 TaskStatus（worker 用它更新 DB 与
    Telegram 状态消息）。
    """
    archive_time = archive_time or datetime.now(timezone.utc)

    # 0. worker 侧 SSRF 复验（入口校验之外的第二道防线，规格 §50）
    from app.archive.ssrf import validate_url

    if not validate_url(url):
        raise FetchError("URL failed SSRF validation", code=ErrorCode.INVALID_URL)

    # 1. 抓取（ArchiveBOT）
    if on_status:
        on_status(TaskStatus.FETCHING)
    article = fetch_article(url, platform, task_dir, cookie_profile=cookie_profile)

    # 2. 清洗
    if on_status:
        on_status(TaskStatus.PARSING)
    if platform == Platform.WECHAT:
        # 微信公众号内容来自 wechat_to_md（parser 已按 #js_content + code/media/噪声过滤），
        # 产物 content.md 已是本地化的 Markdown 主路径，HTML 仅作 PDF 的渲染源。
        # 通用 cleaner 的 blocklist（含 share-/comment/related-/recommend 等）
        # 会误杀公众号正文容器（rich_media_content/share_notice 等），因此
        # 微信分支仅做脚本/样式去除与空块过滤，不走 blocklist。
        cleaned_html = cleaner.clean_html_for_wechat(article.html)
        cleaned_text = cleaner.clean_text(article.text or _html_to_text(cleaned_html))
    else:
        cleaned_html = cleaner.clean_html(article.html)
        cleaned_text = cleaner.clean_text(article.text or _html_to_text(cleaned_html))

    # 3. 图片本地化（从 ArchiveBOT 产物目录拷贝到 task_dir/images）
    if on_status:
        on_status(TaskStatus.DOWNLOADING_IMAGES)
    if article.save_path is not None:
        image_files = images_mod.copy_images(article.save_path, task_dir)
        # 彩图封面：WEB（如 caixin #page2）无 cover.jpg，仅 avatar.jpg 时取首图
        cover_src = article.cover
        if cover_src is None and image_files:
            cover_src = image_files[0]
    else:
        image_files = _collect_images(task_dir)
        cover_src = None

    result = ArchiveResult(
        task_dir=task_dir,
        platform=platform.value,
        title=article.title,
        author=article.author,
        sitename=article.sitename,
        published_at=article.published_at,
        source_url=url,
        cover_path=cover_src,
    )

    # 存档文件名：标题_YYYY-MM-DD_HHMM.ext（仅正文与图片，命名跟标题走）
    # archive_time 已在函数入口确定（默认 now UTC），用于本次全部产物的统一时间戳
    from app.archive.naming import archive_basename
    _basename = archive_basename(article.title or "Untitled", archive_time)

    # 3b. 微信 Markdown 主路径：wechat_to_md 产物的 content.md 已含本地化图片
    # （images/xx.jpg），无需再以 cleaned_html 重新生成，避免空 HTML 导致幻觉。
    _wechat_md_raw = article.markdown if platform == Platform.WECHAT and article.markdown else ""

    # 4. Markdown
    if OutputType.MARKDOWN in output_types or OutputType.PDF in output_types:
        if on_status:
            on_status(TaskStatus.GENERATING_MARKDOWN)
        if _wechat_md_raw:
            md_content = _wechat_filtered_md(_wechat_md_raw, image_files)
            # 交付 MD 需要本地名→远程 URL 映射（页面 data-src 顺序 ↔ md 本地引用顺序）
            result.image_urls = _wechat_image_url_map(article.page_html, md_content)
        else:
            md_content = article.markdown or markdown_mod.html_to_markdown(cleaned_html)
            image_map = images_mod.build_image_map(article.html, md_content, image_files)
            md_content = markdown_mod.rewrite_image_refs(md_content, image_map)
            # vendor 下载图片后 content.html 里只剩本地路径，原始 URL 在 data-original-src
            result.image_urls = _extract_original_image_urls(article.html)
        result.markdown_path = markdown_mod.build_markdown_file(task_dir, md_content, basename=_basename)

    # 5. PDF
    if OutputType.PDF in output_types:
        if on_status:
            on_status(TaskStatus.GENERATING_PDF)
        # 把内容转为 PDF 的 HTML：微信分支由 markdown 先经 markdown 渲染为 HTML，
        # 再把 img src 改写为 file:// 绝对路径（Playwright 渲染需要，规格 §11）
        if _wechat_md_raw and result.markdown_path:
            md_path = task_dir / f"{_basename}.md"
            pdf_source_md = (
                md_path.read_text(encoding="utf-8")
                if md_path.exists()
                else md_content
            )
            pdf_html = markdown_mod.markdown_to_html(pdf_source_md)
            pdf_html = _rewrite_image_srcs(pdf_html, task_dir / "images")
        else:
            pdf_html = _rewrite_image_srcs(cleaned_html, task_dir / "images")
        try:
            result.pdf_path = pdf_mod.build_pdf(
                title=article.title,
                author=article.author or article.sitename,
                source=article.sitename or platform.value,
                published=article.published_at,
                url=url,
                content_html=pdf_html,
                output_path=task_dir / f"{_basename}.pdf",
                archived_at=archive_time,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("pdf generation failed")
            raise FetchError(str(e), code="PDF_GENERATION_FAILED") from e

    # 6. 长截图（替代 ZIP）：IMAGES 即截图，不依赖是否有原图
    if OutputType.IMAGES in output_types:
        import app.archive.screenshot as screenshot_mod

        # 截图用与 PDF 相同的正文 HTML（已 base64 内联），保证一致性
        if OutputType.PDF in output_types:
            screenshot_html = pdf_html  # 同一份已内联的 HTML
        elif _wechat_md_raw:
            # 微信 article.html 为空（service 不产 content.html），用 cleaned_html
            # 渲染会得到空白页；正文以 content.md 为准，即使未选 Markdown 也一样
            wechat_md = _wechat_filtered_md(_wechat_md_raw, image_files)
            screenshot_html = markdown_mod.markdown_to_html(wechat_md)
            screenshot_html = _rewrite_image_srcs(screenshot_html, task_dir / "images")
        else:
            screenshot_html = _rewrite_image_srcs(cleaned_html, task_dir / "images")
        result.screenshot_path = screenshot_mod.build_screenshot(
            title=article.title,
            author=article.author or article.sitename,
            source=article.sitename or platform.value,
            published=article.published_at,
            url=url,
            content_html=screenshot_html,
            output_path=task_dir / f"{_basename}.png",
            archived_at=archive_time,
        )
        result.images = image_files  # 保留计数用于 metadata/展示

    # 7. 三行摘要（不调用 LLM）
    result.excerpt = excerpt.extract_excerpt(cleaned_text)

    # 8. metadata.json
    _write_metadata(result, archive_time)
    return result


def _collect_images(task_dir: Path) -> list[Path]:
    img_dir = task_dir / "images"
    if not img_dir.exists():
        return []
    return sorted(p for p in img_dir.iterdir() if p.is_file())


def _wechat_filtered_md(md_raw: str, image_files: list[Path]) -> str:
    """微信 content.md 行过滤：仅保留命中本地文件的 images/ 引用行。"""
    valid = {f"images/{p.name}" for p in image_files}
    return "\n".join(
        line if "images/" not in line or any(v in line for v in valid) else line
        for line in md_raw.splitlines()
    )


def _wechat_image_url_map(page_html: str, md_content: str) -> dict[str, str]:
    """微信 本地图片名 → 远程 URL。

    vendor 下载后 md 只剩 images/xx 本地引用，原始 URL 不落盘；但下载顺序
    与 #js_content 内 img 的 data-src/src 顺序一致（同一次 DOM 遍历产出），
    两侧各自去重后按顺序对齐。数量对不上（相册页等特殊版式）则放弃映射，
    交付 MD 保留本地引用。
    """
    if not page_html:
        return {}
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "html.parser")
    content_el = soup.select_one("#js_content") or soup
    remote: list[str] = []
    seen: set[str] = set()
    for img in content_el.select("img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if src.startswith(("http://", "https://")) and src not in seen:
            seen.add(src)
            remote.append(src)
    local: list[str] = []
    seen_local: set[str] = set()
    for m in re.finditer(r"!\[[^\]]*\]\(((?:\./)?images/[^)]+)\)", md_content):
        name = Path(m.group(1)).name
        if name not in seen_local:
            seen_local.add(name)
            local.append(name)
    if not remote or len(remote) != len(local):
        logger.info(
            "wechat image url map skipped (remote=%d local=%d)", len(remote), len(local)
        )
        return {}
    return dict(zip(local, remote, strict=True))


def _extract_original_image_urls(html: str) -> dict[str, str]:
    """提取 本地图片名 → 原始远程 URL（webpage_patch 合并媒体时留下的
    data-original-src；vendor 下载图片后 src 已改写为本地路径）。"""
    from bs4 import BeautifulSoup

    out: dict[str, str] = {}
    for img in BeautifulSoup(html, "html.parser").find_all("img"):
        orig = (img.get("data-original-src") or "").strip()
        src = (img.get("src") or "").strip()
        if orig and src.startswith("images/"):
            out[Path(src).name] = orig
    return out


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text(separator="\n")


def _write_metadata(result: ArchiveResult, archive_time: datetime) -> None:
    meta = {
        "platform": result.platform,
        "title": result.title,
        "author": result.author,
        "sitename": result.sitename,
        "published_at": result.published_at,
        "source_url": result.source_url,
        "excerpt": result.excerpt,
        "image_count": result.image_count,
        "image_urls": result.image_urls or None,
        "markdown": str(result.markdown_path.name) if result.markdown_path else None,
        "pdf": str(result.pdf_path.name) if result.pdf_path else None,
        "screenshot": str(result.screenshot_path.name) if result.screenshot_path else None,
        "cover": str(result.cover_path.name) if result.cover_path else None,
        "video": str(result.video_path.name) if result.video_path else None,
        "archived_at": archive_time.isoformat(),
    }
    (result.task_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _rewrite_image_srcs(html: str, img_dir: Path) -> str:
    """把 HTML 中的 `images/xxx` 图片内联为 base64 data URI。

    之前的 file:// 重写在 Chromium set_content 场景下受 file 访问限制且
    正则替换会截断 alt 等属性，导致 PDF 中图片仍显示为裂图。改为 base64
    内联后 PDF 完全自包含，渲染 100% 可复现。
    """
    if not img_dir.exists():
        return html
    import base64
    import mimetypes

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not (src.startswith("images/") or src.startswith("./images/")):
            continue
        clean = src.removeprefix("./").removeprefix("images/")
        # 去掉可能的 query/hash
        clean = clean.split("?")[0].split("#")[0]
        file_path = img_dir / clean
        if not file_path.exists():
            # 兼容大小写/后缀差异：按 stem 模糊匹配
            candidates = [p for p in img_dir.iterdir() if p.stem == Path(clean).stem]
            if candidates:
                file_path = candidates[0]
            else:
                continue
        try:
            mime, _ = mimetypes.guess_type(str(file_path))
            if not mime or not mime.startswith("image/"):
                ext = file_path.suffix.lower()
                mime = {
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".gif": "image/gif",
                    ".webp": "image/webp", ".bmp": "image/bmp",
                }.get(ext, "image/jpeg")
            data = file_path.read_bytes()
            if not data:
                continue
            b64 = base64.b64encode(data).decode("ascii")
            img["src"] = f"data:{mime};base64,{b64}"
        except Exception:
            # 降级为 file://，至少保留路径
            try:
                img["src"] = f"file://{file_path.resolve()}"
            except Exception:
                continue
    # BeautifulSoup 会包一层 <html><body>，只返回 body 内部
    if soup.body is not None:
        return soup.body.decode_contents()
    return str(soup)


def run_video(
    *,
    task_dir: Path,
    url: str,
    platform: Platform,
    archive_time: datetime | None = None,
    on_status: Callable[[TaskStatus], None] | None = None,
) -> ArchiveResult:
    """视频类平台（youtube/bilibili/douyin/kuaishou/instagram）的归档。

    只产出 video 文件（内容抓取→落盘），不生成 MD/PDF/图片。
    """
    from app.archive.fetcher import fetch_video
    from app.archive.ssrf import validate_url

    archive_time = archive_time or datetime.now(timezone.utc)

    # worker 侧 SSRF 复验
    if not validate_url(url):
        raise FetchError("URL failed SSRF validation", code=ErrorCode.INVALID_URL)

    if on_status:
        on_status(TaskStatus.FETCHING)
    video = fetch_video(url, platform, task_dir)

    result = ArchiveResult(
        task_dir=task_dir,
        platform=platform.value,
        title=video.title,
        author=video.author or video.sitename,
        sitename=video.sitename,
        published_at=video.published_at,
        source_url=url,
        video_path=video.video_path,
    )
    _write_metadata(result, archive_time)
    return result
