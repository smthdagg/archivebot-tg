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
    screenshot_path: Path | None = None  # 长截图（IMAGES 即截图替代 ZIP）
    video_path: Path | None = None  # 视频类平台（Phase 2）：视频文件路径

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def is_video(self) -> bool:
        """是否视频类产出（video_path 存在即视为视频交付）。"""
        return self.video_path is not None
