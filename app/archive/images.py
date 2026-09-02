"""图片处理（设计规格 §7.3）：本地化引用 + images.zip 打包。"""

import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def copy_images(src_dir: Path, task_dir: Path) -> list[Path]:
    """把 ArchiveBOT 产物目录的 images/ 拷贝到任务目录，返回本地路径列表。"""
    src = src_dir / "images"
    dst = task_dir / "images"
    dst.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return []
    copied: list[Path] = []
    for p in sorted(src.iterdir()):
        if p.is_file():
            target = dst / p.name
            if not target.exists():
                target.write_bytes(p.read_bytes())
            copied.append(target)
    # 封面/头像：若存在 cover.* / avatar.* 且未被 images/ 覆盖，单独拷贝
    for name in ("cover.jpg", "cover.png", "cover.webp", "avatar.jpg", "avatar.png"):
        src_cover = src_dir / name
        if src_cover.exists():
            target = dst / src_cover.name
            if not target.exists():
                target.write_bytes(src_cover.read_bytes())
                copied.append(target)
                logger.info("copied cover %s to %s", name, dst)
    logger.info("copied %d images to %s", len(copied), dst)
    return copied


def build_image_map(html: str, markdown: str, image_files: list[Path]) -> dict[str, str]:
    """尽力把 Markdown 中的远程图片 URL 映射为本地相对路径。

    对齐策略：content.html 中图片的本地 src（images/NNN.ext）与 Markdown 中
    图片远程 URL 都保持正文顺序；按顺序配对。数量不一致时放弃改写（保留远程
    链接，保证在线可读）。
    """
    if not image_files:
        return {}
    local_srcs = _IMG_SRC_RE.findall(html)
    remote_urls = [u for u in _MD_IMG_RE.findall(markdown)]
    if not local_srcs or not remote_urls:
        return {}
    local_srcs = [s for s in local_srcs if s.startswith("images/")]
    if len(local_srcs) != len(remote_urls):
        logger.warning(
            "image count mismatch html=%d md=%d; partial mapping to min(%d)",
            len(local_srcs),
            len(remote_urls),
            min(len(local_srcs), len(remote_urls)),
        )
    return dict(zip(remote_urls, local_srcs, strict=False))


def make_images_zip(task_dir: Path, image_files: list[Path], basename: str | None = None) -> Path:
    """把图片打包为 ZIP（用于 IMAGES 输出类型）。basename 为标题_YYYY-MM-DD_HHMM。"""
    zip_name = f"{basename}.zip" if basename else "images.zip"
    zip_path = task_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in sorted(image_files):
            zf.write(img, arcname=img.name)
    logger.info("images zip: %s (%d files)", zip_path, len(image_files))
    return zip_path
