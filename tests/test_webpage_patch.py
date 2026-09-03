"""webpage_patch 单元测试：URL 归一化、媒体合并、content.txt/md 重写。"""

import json
from pathlib import Path

from app.archive.webpage_patch import _merge_media, _rewrite_content_files, normalize_caixin_url


class TestNormalizeCaixinUrl:
    def test_strips_fragment_and_p0(self):
        url = "https://weekly.caixin.com/2026-08-29/102479584.html?p0#page2"
        assert normalize_caixin_url(url) == "https://weekly.caixin.com/2026-08-29/102479584.html"

    def test_mobile_path_normalized_to_desktop(self):
        url = "https://weekly.caixin.com/m/2026-08-29/102479596.html?p0#page2"
        assert normalize_caixin_url(url) == "https://weekly.caixin.com/2026-08-29/102479596.html"

    def test_keeps_other_query_params(self):
        url = "https://weekly.caixin.com/a.html?p0&foo=bar#page2"
        assert normalize_caixin_url(url) == "https://weekly.caixin.com/a.html?foo=bar"

    def test_plain_url_unchanged(self):
        url = "https://weekly.caixin.com/2026-08-29/102479584.html"
        assert normalize_caixin_url(url) == url


class TestMergeMedia:
    def test_merges_lead_figure_before_anchor_paragraph(self):
        content = "<p>文｜财新周刊 王石玉 岳跃</p><p>一片寂静，一场黄金“暗夜大迁徙”正悄然进行。</p>"
        media = [{"src": "https://img.caixin.com/x/1.jpg", "caption": "金库图注", "anchor": "文｜财新周刊 王石玉 岳跃"}]
        out = _merge_media(content, media)
        assert out.startswith("<figure>")
        assert 'src="https://img.caixin.com/x/1.jpg"' in out
        # 原始远程 URL 保留在 data-original-src（vendor 下载后 src 会改写为本地路径）
        assert 'data-original-src="https://img.caixin.com/x/1.jpg"' in out
        assert "<figcaption>金库图注</figcaption>" in out
        # 图片插在锚点段落之前
        assert out.index("</figure>") < out.index("文｜财新周刊")

    def test_prepends_when_anchor_missing(self):
        content = "<p>正文段落</p>"
        media = [{"src": "https://img.caixin.com/x/2.jpg", "caption": "", "anchor": ""}]
        out = _merge_media(content, media)
        assert out.startswith("<figure>")
        assert "<figcaption" not in out

    def test_no_media_unchanged(self):
        content = "<p>正文</p>"
        assert _merge_media(content, []) == content

    def test_skips_media_without_src(self):
        content = "<p>正文</p>"
        media = [{"src": "", "caption": "x", "anchor": ""}]
        assert _merge_media(content, media) == content


class TestRewriteContentFiles:
    def _make_task_dir(self, tmp_path: Path) -> Path:
        post_dir = tmp_path / "task"
        post_dir.mkdir()
        reader_html = (
            "<html><body><div class=\"reader-content\">"
            "<h1>文章标题</h1>"
            "<p style=\"color:#888;font-size:0.9em\">作者 · 财新 · 2026-08-29</p>"
            "<figure><img src=\"images/01.jpg\"/><figcaption>图注</figcaption></figure>"
            "<p>　　正文第一段，足够长的一段话。</p><p>　　正文第二段。</p>"
            "</div></body></html>"
        )
        (post_dir / "content.html").write_text(reader_html, encoding="utf-8")
        meta = {
            "title": "文章标题",
            "author": "王石玉",
            "sitename": "caixin.com",
            "published_date": "2026-08-29",
            "source_url": "https://weekly.caixin.com/a.html",
        }
        (post_dir / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        return post_dir

    def test_rewrites_txt_and_md_from_logged_in_render(self, tmp_path):
        post_dir = self._make_task_dir(tmp_path)
        # 模拟 vendor 先写入的试读版
        (post_dir / "content.txt").write_text("试读截断", encoding="utf-8")
        (post_dir / "content.md").write_text("# 试读截断", encoding="utf-8")

        _rewrite_content_files(post_dir)

        txt = (post_dir / "content.txt").read_text(encoding="utf-8")
        assert "正文第一段" in txt
        assert "试读截断" not in txt

        md = (post_dir / "content.md").read_text(encoding="utf-8")
        assert md.startswith("# 文章标题")
        assert "**Author**: 王石玉" in md
        assert "**Source**: https://weekly.caixin.com/a.html" in md
        assert "![](images/01.jpg)" in md
        assert "正文第二段" in md
        # vendor 包装的 h1/meta 不重复进入 md 正文
        assert md.count("# 文章标题") == 1

    def test_missing_content_html_is_noop(self, tmp_path):
        post_dir = tmp_path / "empty"
        post_dir.mkdir()
        _rewrite_content_files(post_dir)
        assert not (post_dir / "content.txt").exists()


class TestWechatImageUrlMap:
    """runner._wechat_image_url_map：data-src 顺序 ↔ md 本地引用顺序对齐。"""

    def _make(self):
        from app.archive.runner import _wechat_image_url_map

        return _wechat_image_url_map

    def test_aligns_by_order(self):
        fn = self._make()
        html = (
            '<div id="js_content">'
            '<img data-src="https://mmbay.qpic.cn/a.jpg">'
            '<img data-src="https://mmbay.qpic.cn/b.png">'
            '<img data-src="https://mmbay.qpic.cn/a.jpg">'  # 重复 URL，服务端会去重
            "</div>"
        )
        md = "![图片](images/01.jpg)\n正文\n![图片](images/02.png)\n![图片](images/01.jpg)\n"
        assert fn(html, md) == {
            "01.jpg": "https://mmbay.qpic.cn/a.jpg",
            "02.png": "https://mmbay.qpic.cn/b.png",
        }

    def test_count_mismatch_returns_empty(self):
        fn = self._make()
        html = '<div id="js_content"><img data-src="https://mmbay.qpic.cn/a.jpg"></div>'
        md = "![a](images/01.jpg)\n![b](images/02.jpg)\n"
        assert fn(html, md) == {}

    def test_empty_page_html_returns_empty(self):
        fn = self._make()
        assert fn("", "![a](images/01.jpg)") == {}
