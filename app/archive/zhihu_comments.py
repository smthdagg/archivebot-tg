"""知乎评论区增强（本仓库 wrapper；vendor/ArchiveBOT 只读红线，见 AGENTS.md）。

上游 ZhihuService 只取正文。知乎评论区**未登录不可读**（匿名请求返回 200 但
data=[]，edit_status.toast="未登录用户"），因此仅当任务携带含 zhihu cookie 的
profile 时才抓取评论；任何失败只降级跳过并记日志，绝不影响正文归档任务。

接口为知乎 web 前端的 comment_v5 JSON API，与上游 _fetch_via_api 同款
curl_cffi chrome124 指纹（免 x-zse-96 签名）。请求 URL 是代码常量 host +
纯数字 id（从规范化 URL 正则提取），无用户输入拼接，不经过 SSRF URL 校验
（host 白名单写死为 www.zhihu.com）。

评论按「热度」（order_by=default）分页拉取根评论；响应条目自带
child_comments 时渲染内嵌子评论（不额外发请求）。数量上限见常量，防单任务
请求过多触发风控。

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
MAX_ROOT_COMMENTS = 100  # 根评论总数上限（limit=20/页 → 至多 5 页）
PAGE_SIZE = 20
CHILD_RENDER_LIMIT = 5  # 每条根评论渲染的内嵌子评论上限

_API_HOST = "https://www.zhihu.com"
# URL 形态 → comment_v5 resource 路径（知乎文章/回答/想法三端）
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
    total: int = 0       # 评论区总数（counts.total_counts，抓不到时按已取数）

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
    kind, item_id = parsed

    try:
        comments, total = _fetch_root_comments(kind, item_id, cookies, max_root=max_root)
    except Exception as e:  # noqa: BLE001 - 评论为增强特性，任何失败降级跳过
        logger.warning("zhihu comments fetch failed for %s (kind=%s): %s", item_id, kind, e)
        return ZhihuComments()

    if not comments:
        logger.info("zhihu comments: no comments for %s (totals=%s)", item_id, total)
        return ZhihuComments(total=int(total or 0))

    rendered = [_render_root(c) for c in comments]
    rendered = [r for r in rendered if r]
    if not rendered:
        return ZhihuComments(total=int(total or 0))

    logger.info("zhihu comments: %d root comments for %s", len(comments), item_id)
    return ZhihuComments(
        html=_build_html(rendered, total),
        markdown=_build_markdown(rendered),
        total=int(total or len(comments)),
    )


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def _make_session(cookies: list[dict[str, Any]]):
    """curl_cffi chrome124 会话 + 知乎浏览器请求头（同上游 _fetch_via_api）。"""
    from curl_cffi import requests as req

    session = req.Session(impersonate="chrome124")
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": _API_HOST,
    })
    cookie_dict = {str(c.get("name")): str(c.get("value")) for c in cookies if c.get("name") and c.get("value")}
    session.cookies.update(cookie_dict)
    return session


def _fetch_root_comments(
    kind: str, item_id: str, cookies: list[dict[str, Any]], *, max_root: int
) -> tuple[list[dict[str, Any]], int | None]:
    """按热度分页拉取根评论。返回 (评论条目列表, 评论区总数)。"""
    session = _make_session(cookies)
    comments: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0
    # 评论 API 由固定 host + 纯数字 id 构成，拒绝任何非常量注入
    if not re.fullmatch(r"\d+", item_id):
        raise ZhihuCommentsError(f"non-numeric zhihu id: {item_id!r}")

    while offset < max_root:
        api_url = (
            f"{_API_HOST}/api/v4/comment_v5/{kind}/{item_id}/root_comment"
            f"?limit={PAGE_SIZE}&offset={offset}&order_by=default"
        )
        resp = session.get(api_url, timeout=20)
        if resp.status_code != 200:
            raise ZhihuCommentsError(f"comment API HTTP {resp.status_code}")
        payload = resp.json()
        page_total = ((payload.get("paging") or {}).get("totals") or 0)
        counts = payload.get("counts") or {}
        if counts.get("total_counts") is not None:
            total = int(counts["total_counts"])
        elif page_total:
            total = int(page_total)
        page = payload.get("data") or []
        comments.extend(page)
        paging = payload.get("paging") or {}
        if not page or paging.get("is_end") is not False:
            break
        offset += PAGE_SIZE  # 固定按页推进；offset 超 total 时 API 返回空页自然停止
    return comments[:max_root], total


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
