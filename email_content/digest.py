"""按部门聚合的富 HTML 邮件。"""

from html import escape
from typing import Any

from config import EMAIL_DIGEST_ALWAYS_OPEN, EMAIL_INCLUDE_SOURCE_LINK, OFFICAL_URL

from .article_assets import format_attachment_notice, parse_attachment_links
from .attachments import collect_file_attachments
from .html_email import prepare_body_html
from .models import DigestEmail, InlineImage


def build_platform_digest(platform: str, articles: list[Any]) -> DigestEmail:
    if not articles:
        raise ValueError("articles 不能为空")

    if len(articles) == 1:
        subject = f"【SZTU-NoticePush】{articles[0].title}"
    else:
        subject = f"【SZTU-NoticePush】{platform} - {len(articles)}条新通知"

    inline_images: list[InlineImage] = []
    attachment_map: dict[int, list[dict[str, str]]] = {}

    article_seq_by_id = {article.id: i for i, article in enumerate(articles, 1)}

    for article in articles:
        if article.raw_data:
            attachment_map[article.id] = parse_attachment_links(article.raw_data)

    file_attachments, skipped_map = collect_file_attachments(
        articles, attachment_map, article_seq_by_id
    )

    open_attr = " open" if EMAIL_DIGEST_ALWAYS_OPEN else ""
    article_blocks: list[str] = []
    title_index_items: list[str] = []

    for i, article in enumerate(articles, 1):
        title_index_items.append(
            f'<li><span class="ti-idx">#{i}</span> {escape(article.title or "无标题")}</li>'
        )
        date_display = (
            f"{article.date} {article.detail_time}"
            if article.detail_time
            else (article.date or "")
        )
        body_html = prepare_body_html(article, inline_images)
        attach_html = ""
        links = attachment_map.get(article.id, [])
        if links:
            skipped_urls = {s["url"] for s in skipped_map.get(article.id, [])}
            attached = [f for f in links if f["url"] not in skipped_urls]
            skipped = skipped_map.get(article.id, [])
            attach_html = format_attachment_notice(attached, skipped)

        source_link = ""
        if EMAIL_INCLUDE_SOURCE_LINK and article.url:
            source_link = (
                f'<p class="source-link"><a href="{escape(article.url)}" '
                f'target="_blank">在公文通官网查看原文</a></p>'
            )

        summary_title = escape(article.title or "无标题")
        article_blocks.append(
            f"""
            <details class="article-details"{open_attr}>
              <summary class="article-summary">
                <span class="idx">#{i}</span>
                <span class="summary-title">{summary_title}</span>
                <span class="summary-meta">{escape(date_display)}</span>
              </summary>
              <div class="article-body">
                {body_html}
                {attach_html}
                {source_link}
              </div>
            </details>
            """
        )

    subscribe_url = f"http://{OFFICAL_URL}/subscribe"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{
      font-family: 'PingFang SC', 'Helvetica Neue', Helvetica, Arial, sans-serif;
      background-color: #f5f5f5;
      color: #333;
      padding: 20px;
      max-width: 640px;
      margin: 0 auto;
      line-height: 1.6;
    }}
    .header {{
      text-align: center;
      margin-bottom: 20px;
      padding-bottom: 15px;
      border-bottom: 2px solid #007bff;
    }}
    .summary-box {{
      background-color: #e7f3ff;
      padding: 15px;
      border-radius: 8px;
      margin-bottom: 20px;
      text-align: left;
    }}
    .title-index {{
      margin: 12px 0 0;
      padding-left: 20px;
      font-size: 14px;
      line-height: 1.5;
    }}
    .title-index .ti-idx {{
      color: #666;
      margin-right: 4px;
    }}
    .article-details {{
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.08);
      margin-bottom: 16px;
      border-left: 4px solid #007bff;
      padding: 0 12px 12px;
    }}
    .article-summary {{
      cursor: pointer;
      font-weight: bold;
      padding: 12px 4px;
      list-style: none;
      font-size: 14px;
    }}
    .article-summary .idx {{ color: #999; margin-right: 6px; }}
    .summary-title {{ font-size: 14px; }}
    .summary-meta {{
      display: block;
      font-size: 12px;
      font-weight: normal;
      color: #666;
      margin-top: 4px;
    }}
    .article-body {{
      padding: 8px 4px 12px;
      border-top: 1px solid #eee;
      font-size: 14px;
    }}
    .article-body p, .article-body span, .article-body div, .article-body li {{
      font-size: 14px !important;
      line-height: 1.55 !important;
    }}
    .article-body img {{ max-width: 100%; height: auto; }}
    .attach-skip {{ font-size: 13px; color: #856404; }}
    .source-link {{ font-size: 13px; margin-top: 12px; }}
    .footer {{
      text-align: center;
      margin-top: 25px;
      font-size: 12px;
      color: #888;
      padding-top: 15px;
      border-top: 1px solid #eee;
    }}
  </style>
</head>
<body>
  <div class="header">
    <h2>深圳技术大学公文通</h2>
  </div>
  <div class="summary-box">
    <h3 style="margin-top:0;text-align:center;">本次推送汇总</h3>
    <p style="text-align:center;margin-bottom:8px;"><strong>{escape(platform)}</strong> 共 <strong>{len(articles)}</strong> 条通知</p>
    <p style="font-size:12px;color:#666;text-align:center;">目录如下，正文与图片见下方各条；附件文件名带 #序号 前缀</p>
    <ul class="title-index">{''.join(title_index_items)}</ul>
  </div>
  {''.join(article_blocks)}
  <div class="footer">
    <p>推送频率按您的订阅设置</p>
    <p><a href="{escape(subscribe_url)}">管理订阅</a></p>
    <p>SZTU-NoticePush</p>
  </div>
</body>
</html>"""

    return DigestEmail(
        subject=subject,
        html=html,
        inline_images=inline_images,
        file_attachments=file_attachments,
    )
