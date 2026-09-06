"""zhihu_comments 评论区增强单测（mock HTTP 会话，不触网）。

覆盖：无 cookie/非知乎 URL/网络失败 → 空降级（不影响正文）；
     分页抓取 + 富文本清洗（img/script/a）+ 内嵌子评论渲染；
     runner 集成：评论并入渲染源、追加进 Markdown（去重、失败不阻塞）。
"""

from pathlib import Path

import pytest

from app.archive import runner
from app.archive import zhihu_comments as zhc
from app.archive.zhihu_comments import ZhihuComments
from app.database.enums import OutputType, Platform


def _author(name="作者甲", **extra):
    d = {"name": name, "url_token": "abc"}
    d.update(extra)
    return d


def _comment(content="<p>说得好</p>", cid="1", author=None, **extra):
    d = {
        "id": cid,
        "content": content,
        "author": author or _author(),
        "like_count": 5,
        "created_time": 1_700_000_000,  # 2023-11-14（秒级）
        "status": "normal",
    }
    d.update(extra)
    return d


@pytest.fixture()
def zhihu_cookies():
    return [{"name": "z_c0", "value": "secret", "domain": ".zhihu.com", "path": "/"}]


def test_no_cookies_skips_quietly():
    assert zhc.fetch_zhihu_comments("https://zhuanlan.zhihu.com/p/123", None).ok is False
    assert zhc.fetch_zhihu_comments("https://zhuanlan.zhihu.com/p/123", []).ok is False


def test_non_zhihu_or_unknown_url_skipped(zhihu_cookies):
    assert zhc.fetch_zhihu_comments("https://example.com/p/123", zhihu_cookies).ok is False
    assert zhc.fetch_zhihu_comments("https://www.zhihu.com/hot", zhihu_cookies).ok is False


@pytest.mark.parametrize(
    "url,kind,item_id",
    [
        ("https://zhuanlan.zhihu.com/p/2079498514170636245", "articles", "2079498514170636245"),
        ("https://www.zhihu.com/p/123456", "articles", "123456"),
        ("https://www.zhihu.com/question/300000/answer/1234567", "answers", "1234567"),
        ("https://www.zhihu.com/answer/1234567?utm_source=wechat", "answers", "1234567"),
        ("https://www.zhihu.com/pin/987654", "pins", "987654"),
    ],
)
def test_classify_zhihu_url(url, kind, item_id):
    assert zhc.classify_zhihu_url(url) == (kind, item_id)


def test_http_error_degrades_to_empty(monkeypatch, zhihu_cookies):
    def _boom(url, cookies, parsed, *, max_root=100):
        raise RuntimeError("network error")

    monkeypatch.setattr(zhc, "_capture_comments_via_page", _boom)
    result = zhc.fetch_zhihu_comments("https://zhuanlan.zhihu.com/p/123", zhihu_cookies)
    assert result == ZhihuComments(total=0)


def test_fetch_and_render_with_children(monkeypatch, zhihu_cookies):
    child = _comment(
        content='<p>补充一点：<img src="https://pica.zhimg.com/emoji.png"/><script>bad()</script>'
        '<a href="https://evil.example.com">原文链接</a></p>',
        cid="11",
        author=_author("乙君"),
        reply_to_author=_author("作者甲"),
    )
    root1 = _comment(
        content="<p>观点很好，<strong>支持</strong>。</p>",
        author=_author("甲"),
        like_count=123,
        child_comments=[child],
    )
    root2 = _comment(cid="2", content="<blockquote>引用原文</blockquote><p>第二条评论</p>", author=_author("丙"))
    root3 = _comment(cid="3", content="<p>第三页评论</p>", author=_author("丁"))

    captured: dict = {"cookies": None, "parsed": None}
    monkeypatch.setattr(
        zhc,
        "_capture_comments_via_page",
        lambda url, cookies, parsed, *, max_root=100: (
            captured.update(cookies=cookies, parsed=parsed) or [root1, root2, root3]
        ),
    )

    result = zhc.fetch_zhihu_comments("https://zhuanlan.zhihu.com/p/2022463078160147125", zhihu_cookies)

    assert captured["cookies"] is zhihu_cookies
    assert captured["parsed"] == ("articles", "2022463078160147125")
    assert result.ok
    # HTML：标题带条数、strong 保留、img → [图片]、script/外链剥除
    assert "评论区 · 3 条" in result.html
    assert "<strong>支持</strong>" in result.html
    assert "[图片]" in result.html and "<img" not in result.html
    assert "<script" not in result.html and "evil.example.com" not in result.html
    assert "原文链接" in result.html  # <a> 只剩文字
    # 子评论：回复目标展示
    assert "回复 @作者甲" in result.html and "乙君" in result.html
    # Markdown：作者/赞/行内标签不断行（时间随本地时区，不精确断言）
    assert result.markdown.startswith("## 评论区")
    assert "- **甲**" in result.markdown and "123 赞" in result.markdown
    assert "观点很好，支持。" in result.markdown
    assert "回复 @作者甲" in result.markdown and "乙君" in result.markdown
    assert "[图片]" in result.markdown and "原文链接" in result.markdown
    assert "引用原文" in result.markdown and "第二条评论" in result.markdown


def test_blank_and_malformed_comments_dropped(monkeypatch, zhihu_cookies):
    """纯空白内容/author 缺失/created_time 非法 → 有默认值、无有效内容时降级为空。"""
    weird = _comment(cid="1", content="<p>   </p>", author={}, created_time=-5)
    monkeypatch.setattr(
        zhc, "_capture_comments_via_page", lambda url, cookies, parsed, *, max_root=100: [weird]
    )
    result = zhc.fetch_zhihu_comments("https://zhuanlan.zhihu.com/p/1", zhihu_cookies)
    assert result.ok is False
    assert result.total == 0


def test_fmt_time_units():
    import re

    pat = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
    assert zhc._fmt_time(None) == ""
    assert zhc._fmt_time(0) == ""
    assert zhc._fmt_time("boom") == ""
    assert pat.fullmatch(zhc._fmt_time(1_700_000_000))  # 秒
    assert pat.fullmatch(zhc._fmt_time(1_700_000_000_000))  # 毫秒 → 归一化到秒


# ---------------------------------------------------------------------------
# runner 集成：评论并入渲染源 + Markdown 追加
# ---------------------------------------------------------------------------

def _make_zhihu_article(tmp_path: Path):
    """知乎 FetchedArticle 替身：vendor content.md 主路径（html 为空走兜底）。"""
    from app.archive.fetcher import FetchedArticle

    return FetchedArticle(
        title="测试文章",
        author="作者",
        sitename="zhihu",
        source_url="https://zhuanlan.zhihu.com/p/123",
        markdown="# 测试文章\n\n正文内容。\n",
        html="",
        text="测试文章\n\n正文内容。",
    )


def test_runner_appends_comments_to_md(monkeypatch, tmp_path):
    """任务带 cookie_profile → 评论并入渲染源，并追加进 Markdown 交付（仅一次）。"""
    from app.archive import zhihu_comments as zhc_mod

    captured: dict = {"cookies": None}

    def fake_fetch_article(url, platform, task_dir, cookie_profile=None):
        return _make_zhihu_article(tmp_path)

    def fake_comments(url, cookies):
        captured["cookies"] = cookies
        return ZhihuComments(
            html='<hr><h2>评论区 · 2 条</h2><div class="comments"><div><b>甲</b>：好文</div></div>',
            markdown="## 评论区\n\n- **甲**：好文\n",
            total=2,
        )

    monkeypatch.setattr(runner, "fetch_article", fake_fetch_article)
    monkeypatch.setattr(zhc_mod, "fetch_zhihu_comments", fake_comments)
    # profile 解析走真实 resolve_cookies 需要 settings 有 profile；测试直供 cookie 列表
    monkeypatch.setattr(
        runner, "_profile_cookies", lambda name, platform: [{"name": "z_c0", "value": "s", "domain": ".zhihu.com"}]
    )

    result = runner.run_archive(
        task_dir=tmp_path,
        url="https://zhuanlan.zhihu.com/p/123",
        platform=Platform.ZHIHU,
        output_types=[OutputType.MARKDOWN],
        cookie_profile="zhihu",
    )

    assert captured["cookies"]  # profile 已解析并传给评论抓取
    md = result.markdown_path.read_text(encoding="utf-8")
    assert "正文内容" in md and "好文" in md
    assert md.count("## 评论区") == 1


def test_runner_comments_failure_does_not_fail_task(monkeypatch, tmp_path):
    """评论抓取抛错/无 cookie → 任务照常完成，md 不含评论区。"""
    from app.archive import zhihu_comments as zhc_mod

    def fake_fetch_article(url, platform, task_dir, cookie_profile=None):
        return _make_zhihu_article(tmp_path)

    def _boom(url, cookies):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "fetch_article", fake_fetch_article)
    monkeypatch.setattr(zhc_mod, "fetch_zhihu_comments", _boom)

    result = runner.run_archive(
        task_dir=tmp_path,
        url="https://zhuanlan.zhihu.com/p/123",
        platform=Platform.ZHIHU,
        output_types=[OutputType.MARKDOWN],
        cookie_profile=None,
    )

    md = result.markdown_path.read_text(encoding="utf-8")
    assert "正文内容" in md
    assert "评论区" not in md
