"""知乎评论区增强（本仓库 wrapper；vendor/ArchiveBOT 只读红线，见 AGENTS.md）。

上游 ZhihuService 只取正文。知乎评论区**未登录不可读**（匿名请求返回 200 但
data=[]，edit_status.toast="未登录用户"）；且评论内容另受 x-zse-96 签名校验
（带登录 cookie 的免签名请求同样只返回 totals、data 为空）。因此抓取评论用
Playwright 打开原页面并**拦截浏览器自身发出的 comment_v5 响应**（页面 JS 自动
带签名），把拦截到的真实评论数据合并进渲染；任何失败只降级跳过并记日志，
绝不影响正文归档任务。

仅当任务携带含 zhihu cookie 的 profile 时才尝试（未登录页面无评论数据）。

产物两路：
- html：富文本（保留评论内粗体/引用等），追加进渲染源 → PDF / 长截图；
- markdown：纯文本化（图片降级为 [图片]），追加进 Markdown 交付文件。
"""

from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# 评论数量/请求节制上限（单任务）
MAX_ROOT_COMMENTS = 100  # 根评论总数上限（拦截合并上限）
CHILD_RENDER_LIMIT = 5   # 每条根评论渲染的内嵌子评论上限

_URL_KIND_RE = [
    (re.compile(r"zhihu\.com/p/(\d+)"), "articles"),
    (re.compile(r"zhihu\.com/question/\d+/answer/(\d+)"), "answers"),
    (re.compile(r"zhihu\.com/answer/(\d+)"), "answers"),
    (re.compile(r"zhihu\.com/pin/(\d+)"), "pins"),
]

# 评论文本里允许保留的标签（其余剥掉，图片一律降级为 [图片]，防外链裂图/恶意富文本）
_KEEP_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "s", "blockquote", "code", "ul", "ol", "li"}


class ZhihuCommentsError(Exception):
    """评论抓取失败（调用方降级处理，不影响正文归档）。"""


@dataclass
class ZhihuComments:
    html: str = ""       # 富文本片段（PDF/长截图追加到正文后）
    markdown: str = ""   # 纯文本评论（Markdown 交付追加）
    total: int = 0       # 评论区总数（取到多少算多少）

    @property
    def ok(self) -> bool:
        return bool(self.html.strip())


def classify_zhihu_url(url: str) -> tuple[str, str] | None:
    """识别知乎内容 URL 形态并提取纯数字 id。

    返回 (kind, id)；kind 为 articles/answers/pins，与 comment_v5 资源路径一致。
    无法识别（非知乎内容 URL / id 非数字）返回 None。
    """
    for pattern, kind in _URL_KIND_RE:
        m = pattern.search(url)
        if m and m.group(1).isdigit():
            return kind, m.group(1)
    return None


def fetch_zhihu_comments(
    url: str,
    cookies: list[dict[str, Any]] | None,
    *,
    max_root: int = MAX_ROOT_COMMENTS,
) -> ZhihuComments:
    """抓取知乎评论区并渲染为 html + markdown。

    cookies：Cookie-Editor/Playwright 同构列表（{name,value,...}，来自
    cookie_profile 的 zhihu 平台条目）。为 None/空时（未带登录态）直接返回空
    ZhihuComments —— 知乎对未登录请求不返回评论内容。
    """
    if not cookies:
        logger.info("zhihu comments skipped: no cookies")
        return ZhihuComments()

    parsed = classify_zhihu_url(url)
    if parsed is None:
        logger.info("zhihu comments skipped: url not a zhihu article/answer/pin: %s", url[:80])
        return ZhihuComments()

    try:
        comments = _capture_comments_via_page(url, cookies, max_root=max_root)
    except Exception as e:  # noqa: BLE001 - 评论为增强特性，任何失败降级跳过
        logger.warning("zhihu comments capture failed for %s: %s", parsed[1], e)
        return ZhihuComments()

    rendered = [_render_root(c) for c in comments]
    rendered = [r for r in rendered if r]
    if not rendered:
        logger.info("zhihu comments: no visible comments for %s", parsed[1])
        return ZhihuComments()

    logger.info("zhihu comments: %d root comments captured for %s", len(rendered), parsed[1])
    return ZhihuComments(
        html=_build_html(rendered, len(rendered)),
        markdown=_build_markdown(rendered),
        total=len(rendered),
    )


# ---------------------------------------------------------------------------
# 抓取：Playwright 打开原页面，拦截浏览器自带签名的 comment_v5 响应
# ---------------------------------------------------------------------------

def _capture_comments_via_page(
    url: str, cookies: list[dict[str, Any]], *, max_root: int
) -> list[dict[str, Any]]:
    """打开知乎页面并滚动触发评论区，拦截 comment_v5 响应中的真实评论。

    知乎评论内容需 x-zse-96 签名（curl_cffi 免签名只能拿到 totals），页面 JS
    请求自带签名 —— 此处直接复用浏览器发出的响应，跨签名校验。
    """
    from playwright.sync_api import sync_playwright

    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def on_response(response) -> None:
        resp_url = response.url
        if "comment_v5/" not in resp_url or "root_comment" not in resp_url:
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
                collected.append(item)

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=launch_args)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        context.add_cookies(cookies)
        page = context.new_page()
        page.on("response", on_response)
        # 先去知乎首页暖场（复用上游同款防风控策略）
        try:
            page.goto("https://www.zhihu.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:  # noqa: BLE001
            pass
        # 滚动到底部触发评论区懒加载；滚动几轮后仍无评论响应则放弃
        for _ in range(8):
            if len(collected) >= max_root:
                break
            try:
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception:  # noqa: BLE001
                break
            page.wait_for_timeout(900)
        try:
            page.wait_for_timeout(1500)  # 等待最后一批响应回传
        except Exception:  # noqa: BLE001
            pass
        context.close()
    return collected[:max_root]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _clean_comment_html(raw: str) -> str:
    """清理评论富文本：剥脚本/链接，白名单标签保留，图片降级为 [图片]。

    返回 body 内部片段（不携带 BeautifulSoup 补齐的 <html><body> 包裹）。
    """
    if not raw:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    # 先降级图片/链接（figure 包裹的 img 也要先处理再整体删除）
    for img in soup.find_all("img"):
        img.replace_with("[图片]")
    for link in soup.find_all("a"):
        link.replace_with(link.get_text())
    for el in soup.find_all(["script", "style", "iframe", "figure"]):
        el.decompose()
    for tag in list(soup.find_all(True)):
        if tag.name not in _KEEP_TAGS:
            tag.unwrap()
    body = soup.body
    return body.decode_contents() if body is not None else str(soup)


def _fmt_time(ts: Any) -> str:
    """unix 秒/毫秒 → 'YYYY-MM-DD HH:MM'；解析失败返回空串。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value > 10**12:  # 毫秒
        value //= 1000
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _author_name(author: Any) -> str:
    if isinstance(author, dict):
        name = author.get("name") or author.get("url_token") or ""
        if name:
            return str(name)
    return "知乎用户"


def _render_one(comment: dict[str, Any], *, level: int = 0) -> dict[str, Any] | None:
    content = _clean_comment_html(comment.get("content") or "")
    if not content or not re.sub(r"<[^>]+>|\s", "", content):
        return None  # 空评论/纯空白 → 丢弃
    likes = comment.get("like_count") or comment.get("vote_count") or 0
    reply_to = ""
    target = comment.get("reply_to_author")
    if isinstance(target, dict):
        reply_to = _author_name(target)
    return {
        "author": _author_name(comment.get("author")),
        "reply_to": reply_to,
        "time": _fmt_time(comment.get("created_time") or comment.get("created_at")),
        "likes": int(likes) if str(likes).isdigit() else 0,
        "html": content,
        "level": level,
        "children": [],
    }


def _render_root(comment: dict[str, Any]) -> dict[str, Any] | None:
    """渲染一条根评论；内嵌 child_comments（如有）附加为子评论。"""
    root = _render_one(comment)
    if root is None:
        return None
    children = comment.get("child_comments")
    if isinstance(children, list):
        for child in children[:CHILD_RENDER_LIMIT]:
            item = _render_one(child, level=1) if isinstance(child, dict) else None
            if item:
                root["children"].append(item)
    return root


def _text_of(html_fragment: str) -> str:
    """渲染后的富文本 → 纯文本（行内标签不断行，块级标签自然换行）。

    图片已在清洗时降级为 [图片]；<br> 视为换行；连续空行折叠。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_fragment, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "blockquote", "ul", "ol", "pre"]):
        block.append("\n")
    lines = [ln.strip() for ln in soup.get_text("").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _build_html(comments: list[dict[str, Any]], total: int | None) -> str:
    """评论区 HTML 片段（追加在正文之后，样式继承模板）。"""
    label = str(total) if total is not None else str(len(comments))
    parts = [
        '<hr class="rule" style="margin-top:26px">',
        f'<h2 style="font-size:17px;margin:14px 0 4px">评论区 · {label} 条</h2>',
        '<div class="comments">',
    ]
    for c in comments:
        parts.append(_comment_div(c))
    parts.append("</div>")
    return "\n".join(parts)


def _comment_div(c: dict[str, Any]) -> str:
    meta_bits = [f"<b>{_html.escape(c['author'])}</b>"]
    if c["reply_to"] and c["reply_to"] != c["author"]:
        meta_bits.append(f"回复 @{_html.escape(c['reply_to'])}")
    if c["time"]:
        meta_bits.append(_html.escape(c["time"]))
    if c["likes"]:
        meta_bits.append(f"{c['likes']} 赞")
    child_html = "".join(_comment_div(ch) for ch in c["children"])
    return (
        f'<div style="margin:10px 0 4px">{" &nbsp;·&nbsp; ".join(meta_bits)}</div>'
        f'<div style="margin:2px 0 6px">{c["html"]}</div>'
        f'<div style="margin:0 0 4px 1.4em">{child_html}</div>'
    )


def _build_markdown(comments: list[dict[str, Any]]) -> str:
    lines = ["## 评论区"]
    for c in comments:
        lines.append(_markdown_one(c, indent=0))
    return "\n".join(lines)


def _markdown_one(c: dict[str, Any], indent: int) -> str:
    prefix = "  " * indent
    meta = f"**{c['author']}**"
    if c["reply_to"] and c["reply_to"] != c["author"]:
        meta += f" 回复 @{c['reply_to']}"
    if c["time"]:
        meta += f"（{c['time']}）"
    if c["likes"]:
        meta += f" · {c['likes']} 赞"
    text = _text_of(c["html"]).replace("\n", "\n" + prefix)
    block = [f"{prefix}- {meta}：{text}"]
    block.extend(_markdown_one(ch, indent + 1) for ch in c["children"])
    return "\n".join(block)
