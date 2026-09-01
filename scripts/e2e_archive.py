"""端到端归档自检：run_archive 全链路（抓取→清洗→图片→Markdown→PDF→摘要）。

不依赖 Telegram，用于：
- 本地：python scripts/e2e_archive.py <url>
- 容器：docker compose run --rm worker python scripts/e2e_archive.py <url>

验证容器/环境工具链：ArchiveBOT services 依赖、Playwright Chromium、CJK 字体。
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/e2e_archive.py <url>")
        return 2
    url = sys.argv[1]

    from app.archive.detector import detect
    from app.archive.runner import run_archive
    from app.database.database import init_db
    from app.database.enums import OutputType
    from app.storage.manager import get_storage

    init_db()
    storage = get_storage()
    task_dir = storage.new_task_dir()
    platform = detect(url)
    print(f"platform={platform.value} task_dir={task_dir}")

    result = run_archive(
        task_dir=task_dir,
        url=url,
        platform=platform,
        output_types=[OutputType.PDF, OutputType.MARKDOWN, OutputType.IMAGES],
        archive_time=datetime.now(timezone.utc),
    )

    def _size(path):
        return path.stat().st_size if path and path.exists() else None

    report = {
        "platform": result.platform,
        "title": result.title,
        "author": result.author,
        "excerpt_lines": len((result.excerpt or "").splitlines()),
        "markdown": {"path": str(result.markdown_path), "size": _size(result.markdown_path)},
        "pdf": {"path": str(result.pdf_path), "size": _size(result.pdf_path)},
        "image_count": result.image_count,
        "images_zip": _size(task_dir / "images.zip"),
        "task_dir": str(task_dir),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    ok = result.markdown_path and result.pdf_path and _size(result.pdf_path) > 0
    print("E2E-OK" if ok else "E2E-FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
