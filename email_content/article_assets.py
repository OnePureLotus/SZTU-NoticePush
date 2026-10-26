"""从文章 raw_data 解析附件与资源链接。"""

import re
from html import escape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from config import BASE_URL


def absolutize_url(url: str, base: str = BASE_URL) -> str:
    if not url:
        return url
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(base.rstrip("/") + "/", url.lstrip("/"))


def parse_attachment_links(raw_data: str | None) -> list[dict[str, str]]:
    """返回 [{"name": 显示名, "url": 绝对地址}, ...]"""
    if not raw_data:
        return []

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    soup = BeautifulSoup(raw_data, "html.parser")
    for block in soup.select(".fujian"):
        for anchor in block.find_all("a", href=True):
            href = absolutize_url(anchor["href"])
            if href in seen:
                continue
            seen.add(href)
            name = anchor.get_text(strip=True) or _filename_from_url(href)
            results.append({"name": name, "url": href})

    pattern = r'附件【<a href="([^"]+)"[^>]*>(.*?)</a>】'
    for href, name in re.findall(pattern, raw_data, re.DOTALL):
        url = absolutize_url(href)
        if url in seen:
            continue
        seen.add(url)
        clean_name = BeautifulSoup(name, "html.parser").get_text(strip=True)
        results.append({"name": clean_name or _filename_from_url(url), "url": url})

    return results


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    if "/" in path:
        return path.rsplit("/", 1)[-1] or "attachment"
    return "attachment"


def format_attachment_notice(
    attached: list[dict[str, str]], skipped: list[dict[str, str]]
) -> str:
    parts = []
    if attached:
        names = ", ".join(escape(a["name"]) for a in attached)
        parts.append(f"<p><strong>本邮件附件：</strong>{names}</p>")
    if skipped:
        links = ", ".join(
            f'<a href="{escape(a["url"])}">{escape(a["name"])}</a>' for a in skipped
        )
        parts.append(
            f"<p class=\"attach-skip\"><strong>因大小限制未附送，请在线下载：</strong>{links}</p>"
        )
    return "\n".join(parts)
