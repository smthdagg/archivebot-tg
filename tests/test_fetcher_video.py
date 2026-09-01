"""fetch_video：视频类平台抓取结果解析（Phase 2 交付链路的 fetcher 端）。"""

import sys
import types
from pathlib import Path

import pytest

from app.archive import fetcher
from app.archive.fetcher import FetchError, fetch_video
from app.database.enums import ErrorCode, Platform


class _FakeVideoService:
    """模拟 ArchiveBOT 视频 service 的产物布局（videos/video.mp4 + 元数据）。"""

    def __init__(self, base_path, create_date_folders=True):
        self.base_path = Path(base_path)
        self.create_date_folders = create_date_folders

    def save_video(self, url: str) -> dict:
        post_dir = self.base_path / "video_v1"
        (post_dir / "videos").mkdir(parents=True)
        (post_dir / "thumbnails").mkdir(parents=True)
        (post_dir / "videos" / "video.mp4").write_bytes(b"fakemp4")
        (post_dir / "thumbnails" / "cover.jpg").write_bytes(b"fakejpg")
        (post_dir / "metadata.json").write_text(
            '{"id": "v999", "title": "Meta 标题", "uploader": "Meta 频道", "upload_date": "20230102"}',
            encoding="utf-8",
        )
        return {
            "video_id": "v1",
            "title": "Fake 视频标题",
            "channel": "Fake 频道",
            "author_name": "Fake 作者",
            "save_path": str(post_dir),
            "duration": "3:21",
        }


class _NoVideoService(_FakeVideoService):
    """save_video 返回 save_path 但未产出视频文件（技能应为 EMPTY_CONTENT）。"""

    def save_video(self, url: str) -> dict:
        post_dir = self.base_path / "empty_video"
        post_dir.mkdir(parents=True)
        return {"save_path": str(post_dir)}


@pytest.fixture()
def fake_module(monkeypatch):
    def _patch(cls):
        mod = types.ModuleType("fake_video_service")
        mod.FakeVideoService = cls
        monkeypatch.setitem(sys.modules, "fake_video_service", mod)
        monkeypatch.setitem(
            fetcher._VIDEO_DISPATCH,
            Platform.YOUTUBE,
            ("fake_video_service", "FakeVideoService", "save_video"),
        )

    return _patch


@pytest.fixture()
def task_dir(tmp_path):
    return tmp_path / "tasks" / "uuid"


def test_video_platforms_registered():
    assert {"youtube", "bilibili", "douyin", "kuaishou", "instagram"} <= Platform.video_platforms()


def test_fetch_video_parses_result(fake_module, task_dir, monkeypatch):
    fake_module(_FakeVideoService)
    task_dir.mkdir(parents=True)

    result = fetch_video("https://youtu.be/v1", Platform.YOUTUBE, task_dir)

    assert result.video_path is not None
    assert result.video_path.exists()
    assert result.video_path.name == "video.mp4"
    assert result.title == "Fake 视频标题"  # raw 优先于 metadata.json
    assert result.author == "Fake 频道"
    assert result.duration == "3:21"
    assert result.video_id == "v1"
    assert result.cover is not None and result.cover.name == "cover.jpg"
    assert result.source_url == "https://youtu.be/v1"
    assert result.sitename == Platform.YOUTUBE.value
    # 真实 service 的 published_at 来自 upload_date（YYYYMMDD → YYYY-MM-DD）
    assert result.published_at == "2023-01-02"


def test_fetch_video_meta_fallback_when_raw_short(fake_module, task_dir):
    """raw 缺 title/channel 时回退 metadata.json 字段。"""
    # 复用 FakeVideoService，但 monkeypatch raw：这里直接测 _from_video_save_result
    path = task_dir / "video_v1"
    (path / "videos").mkdir(parents=True)
    (path / "videos" / "video.mp4").write_bytes(b"fakemp4")
    (path / "metadata.json").write_text(
        '{"id": "v999", "title": "Meta 标题", "uploader": "Meta 频道"}', encoding="utf-8"
    )

    result = fetcher._from_video_save_result(str(path), "https://youtu.be/x", "youtube", {})

    assert result.title == "Meta 标题"
    assert result.author == "Meta 频道"
    assert result.video_id == "v999"


def test_fetch_video_unadapted_platform_raises(fake_module, task_dir):
    fake_module(_FakeVideoService)
    task_dir.mkdir(parents=True)
    # WEB 不在视频调度表内
    with pytest.raises(FetchError) as ei:
        fetch_video("https://example.com", Platform.WEB, task_dir)
    assert ei.value.code == ErrorCode.UNKNOWN


def test_fetch_video_missing_video_raises(fake_module, task_dir):
    fake_module(_NoVideoService)
    task_dir.mkdir(parents=True)
    with pytest.raises(FetchError) as ei:
        fetch_video("https://youtu.be/empty", Platform.YOUTUBE, task_dir)
    assert ei.value.code == ErrorCode.EMPTY_CONTENT
