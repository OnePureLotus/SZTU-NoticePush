"""
推送生产者：爬取入库后按订阅偏好写入每用户待推队列。

消费者逻辑见 push_worker.py（独立线程轮询投递窗口）。
"""

from __future__ import annotations

import logging

from ops_abnormal_log import log_abnormal
from official_document_crawler.crawler.database import Article

logger = logging.getLogger(__name__)


def subscriber_wants_platform(db_manager, subscriber, platform_source: str) -> bool:
    if subscriber.all_platforms:
        return True
    platform_id = db_manager.resolve_platform_id(platform_source)
    if platform_id is None:
        return False
    return platform_id in [p.id for p in subscriber.platforms]


def enqueue_articles_for_all_subscribers(
    subscriber_service, articles: list
) -> tuple[int, int]:
    """
    将新文章写入待推队列。

    Returns:
        (新增队列条数, 跳过条数)
    """
    db = subscriber_service.db_manager
    session = db.get_session()
    try:
        from sqlalchemy.orm import joinedload
        from .subscriberDB import EmailSubscriberDB

        subscribers = (
            session.query(EmailSubscriberDB)
            .options(joinedload(EmailSubscriberDB.platforms))
            .all()
        )
    finally:
        session.close()

    if not subscribers:
        return 0, 0

    added = 0
    skipped = 0
    for article in articles:
        url = article.url if hasattr(article, "url") else article.get("url")
        platform = article.source if hasattr(article, "source") else article.get("source")
        if not url or not platform:
            log_abnormal(
                "push",
                f"入队跳过：文章缺少 url 或 source url={url!r} source={platform!r}",
            )
            continue
        for sub in subscribers:
            if not subscriber_wants_platform(db, sub, platform):
                skipped += 1
                continue
            if db.enqueue_article_pending(sub.id, url, platform):
                added += 1
            else:
                skipped += 1
    return added, skipped


def enqueue_from_urls(subscriber_service, articles_db_manager, new_urls: list[str]) -> int:
    if not new_urls:
        return 0
    session = articles_db_manager.get_session()
    try:
        rows = session.query(Article).filter(Article.url.in_(new_urls)).all()
        if not rows:
            log_abnormal(
                "push",
                f"爬取返回 {len(new_urls)} 条新 URL，但文章库中查不到对应记录",
            )
            return 0
        found = {a.url for a in rows}
        missing = set(new_urls) - found
        if missing:
            log_abnormal(
                "push",
                f"部分新 URL 未入库（{len(missing)} 条），示例: {next(iter(missing))}",
            )
        added, _ = enqueue_articles_for_all_subscribers(subscriber_service, rows)
        return added
    finally:
        session.close()


def _sync_one_platform_outbox(
    db, platform, platform_articles, subscriber_id, ok, err
):
    article_urls = [a.url for a in platform_articles]
    if ok:
        db.delete_push_outbox(subscriber_id, platform, article_urls)
        db.remove_pending_articles(subscriber_id, article_urls)
    else:
        db.upsert_push_outbox(
            subscriber_id,
            platform,
            article_urls,
            error=err,
            increment_attempt=True,
        )
