"""任务执行编排（设计规格 §53 的 worker 内部分）。

流程：抓取 → 清洗 → 图片 → Markdown → PDF → 三行摘要 → metadata.json。
"""

import json
import logging
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

    # 4. Markdown
    if OutputType.MARKDOWN in output_types or OutputType.PDF in output_types:
        if on_status:
            on_status(TaskStatus.GENERATING_MARKDOWN)
        md_content = article.markdown or markdown_mod.html_to_markdown(cleaned_html)
        image_map = images_mod.build_image_map(article.html, md_content, image_files)
        md_content = markdown_mod.rewrite_image_refs(md_content, image_map)
        result.markdown_path = markdown_mod.build_markdown_file(task_dir, md_content)

    # 5. PDF
    if OutputType.PDF in output_types:
        if on_status:
            on_status(TaskStatus.GENERATING_PDF)
        try:
            result.pdf_path = pdf_mod.build_pdf(
                title=article.title,
                author=article.author or article.sitename,
                source=article.sitename or platform.value,
                published=article.published_at,
                url=url,
                content_html=cleaned_html,
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
        "archived_at": archive_time.isoformat(),
    }
    (result.task_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
