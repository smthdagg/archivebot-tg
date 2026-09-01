"""规范化 Markdown 生成（设计规格 §7.2）。

ArchiveBOT 已产出 content.md；本模块在其基础上：
- 清洗后 HTML → Markdown（markdownify 兜底）
- 图片引用改写为本地相对路径（images/NNN.ext），保证离线可读
"""

import logging
import re
from pathlib import Path

from app.archive.cleaner import clean_html

logger = logging.getLogger(__name__)


def html_to_markdown(html: str) -> str:
    """HTML → Markdown（readability 产物或原始片段）。"""
    from markdownify import markdownify as md

    cleaned = clean_html(html)
    return md(cleaned, heading_style="ATX", strip=["script", "style"]).strip()


def rewrite_image_refs(markdown: str, image_map: dict[str, str]) -> str:
    """把远程图片 URL 替换为本地相对路径。

    image_map: {remote_url: "images/001.jpg"}
    """
    if not image_map:
        return markdown

    def _replace(match: re.Match) -> str:
        url = match.group(1).strip()
        local = image_map.get(url)
        if local:
            return f"![{match.group(2)}]({local})"
        return match.group(0)

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace, markdown)


def build_markdown_file(task_dir: Path, content: str) -> Path:
    """写入 article.md，返回路径。"""
    path = task_dir / "article.md"
    path.write_text(content, encoding="utf-8")
    return path
