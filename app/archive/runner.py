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
    else:
        image_files = _collect_images(task_dir)

    result = ArchiveResult(
        task_dir=task_dir,
        platform=platform.value,
        title=article.title,
        author=article.author,
        sitename=article.sitename,
        published_at=article.published_at,
        source_url=url,
    )

    # 3b. 微信 Markdown 主路径：wechat_to_md 产物的 content.md 已含本地化图片
    # （images/xx.jpg），无需再以 cleaned_html 重新生成，避免空 HTML 导致幻觉。
    _wechat_md_raw = article.markdown if platform == Platform.WECHAT and article.markdown else ""

    # 4. Markdown
    if OutputType.MARKDOWN in output_types or OutputType.PDF in output_types:
        if on_status:
            on_status(TaskStatus.GENERATING_MARKDOWN)
        if _wechat_md_raw:
            md_content = _wechat_md_raw
            # 微信的 content.md 已含 images/xx.jpg 本地路径，再以 image_files 校验
            # 去除孤儿远程图（count mismatch 时 build_image_map 会回退远程 URL，这里直接收敛）
            valid = {f"images/{p.name}" for p in image_files}
            # 仅保留命中本地文件的 ![...](images/...)，远程残留不动（保证可读）
            md_content = "\n".join(
                line if "images/" not in line or any(v in line for v in valid) else line
                for line in md_content.splitlines()
            )
        else:
            md_content = article.markdown or markdown_mod.html_to_markdown(cleaned_html)
            image_map = images_mod.build_image_map(article.html, md_content, image_files)
            md_content = markdown_mod.rewrite_image_refs(md_content, image_map)
        result.markdown_path = markdown_mod.build_markdown_file(task_dir, md_content)

    # 5. PDF
    if OutputType.PDF in output_types:
        if on_status:
            on_status(TaskStatus.GENERATING_PDF)
        # 把内容转为 PDF 的 HTML：微信分支由 markdown 先经 markdown 渲染为 HTML，
        # 再把 img src 改写为 file:// 绝对路径（Playwright 渲染需要，规格 §11）
        if _wechat_md_raw and result.markdown_path:
            md_path = task_dir / "article.md"
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
                output_path=task_dir / "article.pdf",
                archived_at=archive_time,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("pdf generation failed")
            raise FetchError(str(e), code="PDF_GENERATION_FAILED") from e

    # 6. 图片 ZIP
    if OutputType.IMAGES in output_types and image_files:
        result.images = image_files
        images_mod.make_images_zip(task_dir, image_files)

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
        "markdown": str(result.markdown_path.name) if result.markdown_path else None,
        "pdf": str(result.pdf_path.name) if result.pdf_path else None,
        "video": str(result.video_path.name) if result.video_path else None,
        "archived_at": archive_time.isoformat(),
    }
    (result.task_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _rewrite_image_srcs(html: str, img_dir: Path) -> str:
    """把 HTML 中的 `<img src="images/xxx">` 改写为 `file://` 绝对路径。

    Playwright 渲染 PDF 时，页面没有 base URL，相对路径无法解析。
    images/ 目录已知在 task_dir 下，转绝对路径后 Chromium 可直接加载。
    """
    if not img_dir.exists():
        return html
    prefix = f"file://{img_dir.resolve()}/"
    img_src_re = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)

    def _replace(m: re.Match) -> str:
        src = m.group(1)
        # 只改写已知的 images/ 相对路径；远程 URL 保持原样
        if src.startswith("images/") or src.startswith("./images/"):
            clean = src.removeprefix("./")
            return f'<img src="{prefix}{clean.removeprefix("images/")}"'
        return m.group(0)

    return img_src_re.sub(_replace, html)


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
