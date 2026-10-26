"""下载附件并控制邮件总大小。"""

import logging
import mimetypes
import re
from typing import Any

from config import MAX_EMAIL_ATTACHMENT_MB
from official_document_crawler.crawler.utils import http_get

from .models import FileAttachment

logger = logging.getLogger(__name__)

MAX_BYTES = MAX_EMAIL_ATTACHMENT_MB * 1024 * 1024


def _safe_filename(name: str, seq_prefix: str | None) -> str:
    base = re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "attachment"
    if seq_prefix:
        return f"{seq_prefix}{base}"
    return base


def collect_file_attachments(
    articles: list[Any],
    attachment_links_by_article: dict[int, list[dict[str, str]]],
    article_seq_by_id: dict[int, int] | None = None,
) -> tuple[list[FileAttachment], list[dict[str, Any]]]:
    """
    为多篇文章下载附件。返回 (已附送列表, 每篇跳过的链接信息用于 HTML 说明)。
    skipped_per_article: {article_id: [link dicts]}
    """
    files: list[FileAttachment] = []
    skipped_per_article: dict[int, list[dict[str, str]]] = {}
    used_names: set[str] = set()
    total = 0

    article_seq_by_id = article_seq_by_id or {}

    for article in articles:
        aid = getattr(article, "id", None)
        seq = article_seq_by_id.get(aid)
        seq_prefix = f"#{seq}_" if seq is not None else None
        links = attachment_links_by_article.get(aid, [])
        skipped: list[dict[str, str]] = []

        for link in links:
            url = link["url"]
            display_name = link["name"]
            resp = http_get(url)
            if not resp:
                skipped.append(link)
                logger.warning("附件下载失败: %s", url)
                continue

            data = resp.content
            size = len(data)
            if total + size > MAX_BYTES:
                skipped.append(link)
                logger.info("附件超限跳过: %s (%s bytes)", display_name, size)
                continue

            fname = _safe_filename(display_name, seq_prefix)
            while fname in used_names:
                fname = _safe_filename(f"dup_{display_name}", seq_prefix)

            used_names.add(fname)
            total += size
            mime_type = (
                resp.headers.get("Content-Type", "").split(";")[0].strip()
                or mimetypes.guess_type(fname)[0]
                or "application/octet-stream"
            )
            files.append(FileAttachment(filename=fname, data=data, mime_type=mime_type))

        if skipped:
            skipped_per_article[aid] = skipped

    return files, skipped_per_article
