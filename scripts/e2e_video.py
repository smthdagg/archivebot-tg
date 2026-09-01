"""端到端视频归档自检：run_video 全链路（Phase 2，视频平台交付）。

调用 ArchiveBOT 视频 service（youtube 需系统已安装 yt-dlp）抓取视频文件，
产出到 task_dir，并校验 videos/video.mp4 存在且非空。不依赖 Telegram。

用法（网络可用则真跑 youtube 短链接）：
    python scripts/e2e_video.py <url>
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/e2e_video.py <url>")
        return 2
    url = sys.argv[1]

    import app.database.database as dbmod
    from app.archive.detector import detect
    from app.archive.runner import run_video
    from app.database.enums import Platform
    from app.storage.manager import get_storage

    dbmod.init_db()
    storage = get_storage()
    task_dir = storage.new_task_dir()
    platform = detect(url)
    if platform.value not in Platform.video_platforms():
        print(f"E2E-SKIP platform={platform.value} 非视频平台，跳过")
        return 2
    print(f"platform={platform.value} task_dir={task_dir}")

    result = run_video(
        task_dir=task_dir,
        url=url,
        platform=platform,
        archive_time=datetime.now(timezone.utc),
    )

    report = {
        "platform": result.platform,
        "title": result.title,
        "author": result.author,
        "video": {
            "path": str(result.video_path),
            "size": result.video_path.stat().st_size if result.video_path else None,
        },
        "metadata": json.loads((task_dir / "metadata.json").read_text(encoding="utf-8")),
        "task_dir": str(task_dir),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    ok = result.video_path is not None and result.video_path.exists() and result.video_path.stat().st_size > 0
    print("E2E-OK" if ok else "E2E-FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
