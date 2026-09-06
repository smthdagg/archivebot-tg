"""知乎纯问题页归档（vendor/ArchiveBOT 只读红线，本仓库 wrapper）。

上游 ZhihuService 只支持 answer/article/pin 形态；`zhihu.com/question/{qid}`
纯问题链接不在其列（save_post 抛 Invalid Zhihu URL → "Unknown error"）。

本模块把问题页归档为「问题 + 高赞回答合集」：
- Playwright（带 zhihu cookie，与评论区抓取同款会话）打开问题页
- 滚动触发回答正文懒加载展开，提取前 N 个 .AnswerItem：
  作者（AuthorInfo meta）、赞同数、正文 HTML
- 正文图片本地化下载（与 vendor save_post 一致：src → images/，
  data-original-src 保留原 URL，供交付 MD 重写为远程）
- 点击第一个（最高赞）回答的「N 条评论」按钮，拦截其 root_comment
  响应（自带 x-zse-96 签名），评论区并入产物 —— 复用 zhihu_comments 渲染
- 落盘 content.{html,md,txt} + images/ + metadata.json（统一产物布局），
  由 fetcher._from_save_result 读回，后续 PDF/长截图/Markdown 管道零改动

任何失败抛 ZhihuQuestionError，由 fetcher 归类为任务错误提示。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ANSWERS = 8      # 回答数量上限（按页面默认热度排序）
SCROLL_ROUNDS = 6    # 滚动轮数（触发正文展开/加载更多）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 非 question/answer 形态的纯问题页（qid 纯数字）
_QUESTION_RE = re.compile(r"zhihu\.com/question/(\d+)(?:[?#/]|$)")
_ANSWER_FORM_RE = re.compile(r"zhihu\.com/question/\d+/answer/\d+")


class ZhihuQuestionError(Exception):
    """问题页归档失败（由 fetcher 归类）。"""


def classify_zhihu_question(url: str) -> str | None:
    """识别纯问题页 URL 并返回 qid；answer 形态与其它知乎内容返回 None。"""
    if _ANSWER_FORM_RE.search(url):
        return None
    m = _QUESTION_RE.search(url)
    if m and m.group(1).isdigit():
        return m.group(1)
    return None


# 页面提取 JS：标题/问题描述 + 回答列表（作者/赞同/正文 HTML/评论按钮）
_EXTRACT_JS = """() => {
  const title = document.title.replace(/ - 知乎\\s*$/, '').trim();
  const detailEl = document.querySelector(
    '.QuestionHeader-detail, .QuestionRichText, .QuestionRichText.ztext');
  const answers = [];
  for (const it of document.querySelectorAll('.AnswerItem')) {
    const authorEl = it.querySelector('.AuthorInfo meta[itemprop=name]');
    const bodyEl = it.querySelector('.RichContent-inner .RichText, .RichContent-inner');
    const voteEl = it.querySelector('.VoteButton, [class*=VoteButton]');
    const comBtn = [...it.querySelectorAll('button')].find(b => /条评论/.test(b.textContent));
    if (!bodyEl || !bodyEl.innerHTML.trim()) continue;
    answers.push({
      author: authorEl ? authorEl.getAttribute('content') : '',
      vote: voteEl ? voteEl.textContent : '',
      body: bodyEl.innerHTML,
      commentLabel: comBtn ? comBtn.textContent.replace(/\\u200b/g, '').trim() : '',
    });
  }
  return {title, detail: detailEl ? detailEl.innerHTML : '', answers};
}"""


def _build_context(playwright, cookies: list[dict[str, Any]]):
    """匿名/登录上下文：登录 cookie 注入 + 反检测初始化。"""
    browser = playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="zh-CN")
    context.add_cookies(cookies)
    return browser, context


def fetch_zhihu_question(url: str, qid: str, cookies: list[dict[str, Any]], task_dir: Path):
    """抓取问题回答合集并落盘统一产物布局，返回 FetchedArticle。

    调用方（fetcher）保证 cookies 非空、ssrf_guard 已装。
    """
    from playwright.sync_api import sync_playwright

    from app.archive import zhihu_comments as zhc
    from app.archive.fetcher import FetchedArticle
    from app.archive.markdown import html_to_markdown

    # 评论拦截（点击最高赞回答的评论按钮后接收其 root_comment 响应）
    comment_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def on_response(response) -> None:
        resp_url = response.url
        if "/answers/" not in resp_url or "root_comment" not in resp_url:
            return
        if response.status != 200:
            return
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return
        for item in payload.get("data") or []:
            cid = str(item.get("id") or "")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                comment_items.append(item)

    with sync_playwright() as p:
        browser, context = _build_context(p, cookies)
        try:
            page = context.new_page()
            page.on("response", on_response)
            # 暖场（知乎首页）降低风控概率
            try:
                page.goto("https://www.zhihu.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
            except Exception as e:  # noqa: BLE001
                raise ZhihuQuestionError(f"问题页打开失败: {e}") from e
            # 滚动触发正文懒加载展开
            for _ in range(SCROLL_ROUNDS):
                try:
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                except Exception:  # noqa: BLE001
                    break
                page.wait_for_timeout(1100)
            page.wait_for_timeout(800)
            data = page.evaluate(_EXTRACT_JS) or {}
            # 点击第一个回答的评论入口（评论区默认折叠；滚动就位后再点）
            try:
                first_btn = page.query_selector('.AnswerItem button:has-text("条评论")')
                if first_btn:
                    first_btn.scroll_into_view_if_needed(timeout=4000)
                    first_btn.click(timeout=3000)
                    page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001 - 评论区是增强特性
                logger.info("zhihu question: comment button click failed, skip comments")
            page.wait_for_timeout(600)
        finally:
            context.close()
            browser.close()

    title = str(data.get("title") or "").strip()
    answers = [a for a in (data.get("answers") or []) if isinstance(a, dict)] or []
    if not answers:
        raise ZhihuQuestionError(
            "问题页没有提取到可见回答（页面可能要求登录，或回答被折叠）"
        )

    folder_name = f"{datetime.now().strftime('%Y-%m-%d')}_question_{qid}"
    folder_name += f"_{hashlib.md5(url.encode()).hexdigest()[:6]}"
    post_dir = task_dir / folder_name
    post_dir.mkdir(parents=True, exist_ok=True)
    img_dir = post_dir / "images"
    img_dir.mkdir(exist_ok=True)

    # 每个回答：正文清洗 + 图片下载本地化
    parts: list[str] = []
    for idx, raw in enumerate(answers[:MAX_ANSWERS], start=1):
        body_html = _clean_answer_body(raw.get("body") or "")
        body_html = _download_images(body_html, img_dir, idx, referer=url)
        author = str(raw.get("author") or "").strip() or f"答主 {idx}"
        vote_text = str(raw.get("vote") or "").strip()
        vote_num = ""
        m = re.search(r"([0-9]+(?:\.[0-9]+)?\s*万?)", vote_text)
        if m:
            vote_num = m.group(1)
        author_html = f"<h2 style=\"font-size:17px;margin:22px 0 2px\">{_escape(author)} 的回答</h2>"
        meta_html = (
            f'<p style="margin:0 0 6px;color:#666;font-size:13px">{_escape(vote_num)} 人赞同了该回答</p>'
            if vote_num
            else ""
        )
        parts.append(f'<section style="margin:8px 0">{author_html}{meta_html}{body_html}</section>')
        # 最高赞回答（第一项）的评论区并入其区块尾部
        if idx == 1 and comment_items:
            comments_html, _ = zhc.render_comment_blocks(comment_items)
            if comments_html:
                parts.append(comments_html)

    # 问题标题 + 描述 + 各回答
    detail = ""
    raw_detail = str(data.get("detail") or "").strip()
    if raw_detail:
        detail = (
            '<blockquote style="color:#555;border-left:3px solid #ddd;'
            'padding-left:10px;margin:4px 0 6px">' + raw_detail + "</blockquote>"
        )
    combined_html = (
        f"<h1>{_escape(title) or '知乎问题'}</h1>{detail}" + "".join(parts)
    )

    # 统一产物布局（fetcher._from_save_result 读回，交付管道零改动）
    md_text = html_to_markdown(combined_html)
    if comment_items:
        _, comments_md = zhc.render_comment_blocks(comment_items)
        if comments_md and "评论区" not in md_text:
            md_text = f"{md_text.rstrip()}\n\n---\n\n{comments_md}\n"
    text_clean = re.sub(r"\n{3,}", "\n\n", _strip_html(combined_html)).strip()
    (post_dir / "content.html").write_text(combined_html, encoding="utf-8")
    (post_dir / "content.md").write_text(md_text, encoding="utf-8")
    (post_dir / "content.txt").write_text(f"{title}\n\n{text_clean}", encoding="utf-8")
    meta = {
        "platform": "zhihu",
        "title": title or "知乎问题",
        "author": "",
        "sitename": "zhihu.com",
        "url": url,
        "question_id": qid,
        "answer_count": min(len(answers), MAX_ANSWERS),
        "comment_count": len(comment_items),
        "saved_at": datetime.now().isoformat(),
    }
    (post_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("zhihu question archived: qid=%s answers=%d comments=%d", qid, len(parts), len(comment_items))
    return FetchedArticle(
        title=meta["title"],
        author="知乎问题",
        sitename="zhihu.com",
        source_url=url,
        markdown=md_text,
        html=combined_html,
        text=text_clean,
        save_path=post_dir,
    )


def _escape(text: str) -> str:
    import html as _h

    return _h.escape(str(text))


def _strip_html(html_text: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html_text, "html.parser").get_text("\n")


def _clean_answer_body(raw: str) -> str:
    """回答正文清理：剥脚本/iframe/视频块，其余正文结构保留。"""
    if not raw:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for el in soup.find_all(["script", "style", "iframe", "noscript", "svg", "video", "figure"]):
        if el.name == "figure" and el.find("img"):
            # 图容器保留图片本身；无图 figure 移除
            continue
        el.decompose()
    # 链接保留文字/地址即可，去掉事件与追踪属性
    for a in soup.find_all("a"):
        a.attrs = {"href": a.get("href", "")}
    for tag in soup.find_all(True):
        keep = {"src", "href", "alt", "data-actualsrc", "data-original"}
        tag.attrs = {k: v for k, v in (tag.attrs or {}).items() if k in keep}
    return str(soup)


def _download_images(body_html: str, img_dir: Path, idx: int, *, referer: str) -> str:
    """把回答正文的远程图片下载到 images/，src 改写为本地相对路径。

    原 URL 保留在 data-original-src（交付 MD 重写为远程引用，同 vendor 布局）。
    """
    import mimetypes

    import requests as _req
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body_html, "html.parser")
    downloaded = 0
    for img in soup.find_all("img"):
        src = (
            img.get("data-actualsrc")
            or img.get("data-original")
            or img.get("src")
            or ""
        ).strip()
        if not src.startswith("http") or "data:image" in src:
            img.decompose()
            continue
        try:
            resp = _req.get(
                src,
                timeout=20,
                headers={"User-Agent": _UA, "Referer": referer},
            )
            resp.raise_for_status()
            mime = resp.headers.get("Content-Type", "")
            ext = mimetypes.guess_extension(mime.split(";")[0]) or ".jpg"
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                ext = ".jpg"
            if not resp.content:
                img.decompose()
                continue
            downloaded += 1
            name = f"q{idx}_{downloaded:02d}{ext}"
            (img_dir / name).write_bytes(resp.content)
            img["src"] = f"images/{name}"
            img["data-original-src"] = src
            img.attrs.pop("data-actualsrc", None)
            img.attrs.pop("data-original", None)
        except Exception as e:  # noqa: BLE001 - 单图失败不阻塞回答正文
            logger.info("zhihu question image download failed (%s): %s", src[:80], e)
            img.decompose()
    return str(soup)
