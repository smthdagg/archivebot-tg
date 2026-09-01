"""归档结果类型定义。"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ArchiveResult:
    """一条任务归档的标准化产出（落在任务目录内）。"""

    task_dir: Path
    platform: str
    title: str = ""
    author: str = ""
    sitename: str = ""
    published_at: str = ""
    source_url: str = ""
    excerpt: str = ""
    # 产出文件路径
    markdown_path: Path | None = None
    pdf_path: Path | None = None
    cover_path: Path | None = None
    images: list[Path] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return len(self.images)
