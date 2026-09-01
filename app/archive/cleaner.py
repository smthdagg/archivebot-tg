"""内容清洗（设计规格 §12）。

ArchiveBOT 已用 Readability/trafilatura 做主体提取；本模块在其产物之上做
二次清洗：移除残余广告/导航/脚本、空元素、压缩空白，并支持扩展 HTML 水印规则。
图片内嵌水印（像素级）属图像处理，不在本阶段范围。
"""

import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 常见广告/无关容器特征（id/class 关键词）
_BLOCKLIST_KEYWORDS = (
    "advert", "adsbygoogle", "ad-container", "banner-", "recommend",
    "related-", "share-", "comment", "cookie", "footer", "sidebar",
    "nav-", "popup", "modal", "toast", "watermark",
)

_SCRIPT_RE = re.compile(r"<(script|style|iframe|noscript)[^>]*>.*?</\1>", re.S | re.I)


def clean_html(html: str) -> str:
    """清洗渲染后 HTML：去脚本/样式、按特征移除无关块、压缩空白。"""
    if not html:
        return ""
    html = _SCRIPT_RE.sub("", html)
    soup = BeautifulSoup(html, "html.parser")

    # 结构性无关元素
    for element in soup.find_all(
        ["script", "style", "iframe", "noscript", "form", "svg", "nav", "aside", "footer"]
    ):
        element.decompose()

    for element in soup.find_all(True):
        class_str = " ".join(element.get("class", [])).lower()
        id_str = (element.get("id") or "").lower()
        if any(k in class_str or k in id_str for k in _BLOCKLIST_KEYWORDS):
            element.decompose()

    # 移除空块
    for element in soup.find_all(["div", "section", "p", "span"]):
        if not element.get_text(strip=True) and not element.find("img"):
            element.decompose()

    cleaned = str(soup)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def clean_text(text: str) -> str:
    """规范化纯文本：逐行去首尾空白、行内空白压缩为单个空格、合并连续空行。"""
    if not text:
        return ""
    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(line)
    # 结尾不留空行
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
