"""_md_for_delivery 单元测试：交付 MD 的本地图片引用改回远程 URL。"""

from dataclasses import dataclass, field
from pathlib import Path

from app.tasks.jobs import _md_for_delivery


@dataclass
class _FakeResult:
    markdown_path: Path
    image_urls: dict[str, str] = field(default_factory=dict)


def test_rewrites_local_refs_to_remote(tmp_path):
    md = tmp_path / "文章_2026-09-03_1200.md"
    md.write_text(
        "# 标题\n\n![](images/01.jpg)\n\n图注文字\n\n正文段落。\n",
        encoding="utf-8",
    )
    result = _FakeResult(md, {"01.jpg": "https://img.caixin.com/2026-08-29/x_840_560.jpg"})

    out = _md_for_delivery(result)

    assert out == md
    text = md.read_text(encoding="utf-8")
    assert "https://img.caixin.com/2026-08-29/x_840_560.jpg" in text
    assert "](images/" not in text


def test_unknown_local_ref_kept(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("![](images/99.jpg)\n![](images/01.jpg)\n", encoding="utf-8")
    result = _FakeResult(md, {"01.jpg": "https://example.com/1.jpg"})

    _md_for_delivery(result)

    text = md.read_text(encoding="utf-8")
    assert "![](images/99.jpg)" in text
    assert "https://example.com/1.jpg" in text


def test_no_mapping_untouched(tmp_path):
    md = tmp_path / "a.md"
    original = "![](images/01.jpg)\n"
    md.write_text(original, encoding="utf-8")
    result = _FakeResult(md, {})

    _md_for_delivery(result)

    assert md.read_text(encoding="utf-8") == original


def test_plain_md_untouched(tmp_path):
    md = tmp_path / "a.md"
    original = "# 标题\n\n正文，无图片。\n"
    md.write_text(original, encoding="utf-8")
    result = _FakeResult(md, {"01.jpg": "https://example.com/1.jpg"})

    _md_for_delivery(result)

    assert md.read_text(encoding="utf-8") == original
