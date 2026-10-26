"""外网 SMTP 失败后的持久化 outbox 后台重试。"""

import json
import logging
import time
from datetime import datetime

from config import (
    EMAIL_PUSH_INTERVAL_SECONDS,
    EMAIL_RETRY_ITEM_INTERVAL_SECONDS,
    EMAIL_RETRY_POLL_SECONDS,
)
from official_document_crawler.crawler.database import Article, DatabaseManager
from ops_abnormal_log import log_abnormal

logger = logging.getLogger(__name__)


def _item_interval_seconds():
    if EMAIL_RETRY_ITEM_INTERVAL_SECONDS is not None:
        return EMAIL_RETRY_ITEM_INTERVAL_SECONDS
    return EMAIL_PUSH_INTERVAL_SECONDS


def process_one_outbox_item(subscriber_service, articles_db_manager):
    """处理一条到期的 outbox 记录。返回是否处理了一条（不论成败）。"""
    db = subscriber_service.db_manager
    rows = db.list_due_outbox(limit=1)
    if not rows:
        return False

    row = rows[0]
    subscriber = db.get_subscriber_by_id(row.subscriber_id)
    if not subscriber:
        logger.warning("outbox id=%s 订阅者不存在，删除该条", row.id)
        db.delete_outbox_by_id(row.id)
        return True

    try:
        urls = json.loads(row.article_urls)
    except json.JSONDecodeError:
        urls = []

    session = articles_db_manager.get_session()
    try:
        articles = session.query(Article).filter(Article.url.in_(urls)).all()
    finally:
        session.close()

    if not articles:
        err = "文章库中未找到 outbox 对应 URL，稍后重试"
        log_abnormal(
            "outbox",
            f"id={row.id} platform={row.platform} subscriber={subscriber.email}: {err}",
        )
        db.upsert_push_outbox(
            row.subscriber_id,
            row.platform,
            urls,
            error=err,
            increment_attempt=True,
        )
        return True

    ok, err = subscriber_service.send_platform_digest_to_subscriber(
        subscriber,
        row.platform,
        articles,
        update_sent_time=True,
    )
    if ok:
        db.delete_push_outbox(row.subscriber_id, row.platform, urls)
        db.remove_pending_articles(row.subscriber_id, urls)
        logger.info(
            "[%s] outbox 补发成功: %s / %s",
            datetime.now(),
            subscriber.email,
            row.platform,
        )
    else:
        db.upsert_push_outbox(
            row.subscriber_id,
            row.platform,
            urls,
            error=err,
            increment_attempt=True,
        )
        log_abnormal(
            "outbox",
            f"补发失败 subscriber={subscriber.email} platform={row.platform}: {err}",
        )
    return True


def run_push_retry_loop(subscriber_service):
    """在 daemon 线程中轮询并重试待发邮件。"""
    articles_db = DatabaseManager()
    interval = _item_interval_seconds()
    logger.info(
        "邮件 outbox 重试线程已启动（轮询 %s 秒，条间间隔 %s 秒）",
        EMAIL_RETRY_POLL_SECONDS,
        interval,
    )
    print(
        f"[{datetime.now()}] 邮件 outbox 重试线程已启动："
        f"每 {EMAIL_RETRY_POLL_SECONDS}s 检查待发队列，外网恢复后自动补发"
    )

    while True:
        try:
            pending = subscriber_service.db_manager.count_pending_outbox()
            if pending:
                logger.debug("待发 outbox 数量: %s", pending)

            processed_any = False
            while process_one_outbox_item(subscriber_service, articles_db):
                processed_any = True
                if interval > 0:
                    time.sleep(interval)

            if not processed_any:
                time.sleep(EMAIL_RETRY_POLL_SECONDS)
        except Exception as e:
            logger.exception("outbox 重试循环异常: %s", e)
            time.sleep(EMAIL_RETRY_POLL_SECONDS)
