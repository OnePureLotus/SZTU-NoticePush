"""邮件正文 HTML 清洗与图片 CID 内嵌。"""

import logging
import mimetypes
import re
import uuid
from html import escape
from typing import Any

from bs4 import BeautifulSoup

from config import BASE_URL, EMAIL_EMBED_IMAGES

from .article_assets import absolutize_url
from .models import InlineImage

logger = logging.getLogger(__name__)

_STRIP_TAGS = {"script", "style", "iframe", "object", "embed", "link", "meta"}


def sanitize_html_fragment(html: str) -> str:
    if not html:
        return "<p>（暂无正文）</p>"

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]

    return soup.decode_contents()


def _normalize_body_typography(soup: BeautifulSoup) -> None:
    """弱化原文内联字号，与邮件标题区保持一致。"""
    for tag in soup.find_all(True):
        style = tag.get("style")
        if style:
            style = re.sub(
                r"font-size\s*:\s*[^;]+;?",
                "",
                style,
                flags=re.IGNORECASE,
            )
            style = style.strip()
            if style:
                tag["style"] = style
            else:
                del tag["style"]


def prepare_body_html(
    article: Any, inline_images: list[InlineImage]
) -> str:
    """处理单篇文章正文：绝对化链接、可选 CID 图片。"""
    raw = article.content or ""
    if not raw.strip():
        raw = f"<p>{escape(article.title or '')}</p>"

    soup = BeautifulSoup(sanitize_html_fragment(raw), "html.parser")
    _normalize_body_typography(soup)

    for anchor in soup.find_all("a", href=True):
        anchor["href"] = absolutize_url(anchor["href"])
        anchor["target"] = "_blank"

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            img.decompose()
            continue
        abs_src = absolutize_url(src)
        if EMAIL_EMBED_IMAGES:
            from official_document_crawler.crawler.utils import http_get

            resp = http_get(abs_src)
            if resp and resp.content:
                subtype = _image_subtype(resp.headers.get("Content-Type", ""), abs_src)
                cid = f"img_{getattr(article, 'id', 0)}_{uuid.uuid4().hex[:8]}"
                inline_images.append(
                    InlineImage(cid=cid, data=resp.content, mime_subtype=subtype)
                )
                img["src"] = f"cid:{cid}"
            else:
                img["src"] = abs_src
                logger.warning("图片下载失败，使用外链: %s", abs_src)
        else:
            img["src"] = abs_src
        for attr in ("width", "height"):
            if attr in img.attrs:
                try:
                    w = int(str(img[attr]).replace("px", ""))
                    if w > 560:
                        img[attr] = "560"
                except ValueError:
                    pass

    return soup.decode_contents()


def _image_subtype(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    if "jpeg" in ct or "jpg" in ct:
        return "jpeg"
    if "png" in ct:
        return "png"
    if "gif" in ct:
        return "gif"
    if "webp" in ct:
        return "webp"
    guessed = mimetypes.guess_type(url)[0] or ""
    if "jpeg" in guessed:
        return "jpeg"
    if "png" in guessed:
        return "png"
    if "gif" in guessed:
        return "gif"
    return "png"
