"""推送消费者线程：按投递窗口与 SMTP 间隔消费每用户待推队列。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from config import EMAIL_PUSH_INTERVAL_SECONDS, PUSH_POLL_SECONDS
from ops_abnormal_log import log_abnormal

from .push_pipeline import _sync_one_platform_outbox

logger = logging.getLogger(__name__)

_push_cycle_lock = threading.Lock()
_last_cycle_stats: dict = {
    "last_run_at": None,
    "subscribers_processed": 0,
    "digests_ok": 0,
    "digests_failed": 0,
    "windows_closed": 0,
    "windows_closed_with_backlog": 0,
}


def get_last_push_cycle_stats() -> dict:
    return dict(_last_cycle_stats)


def _delivery_window_end(subscriber, started_at: datetime) -> datetime:
    hours = subscriber.send_frequency or 24
    return started_at + timedelta(hours=hours)


def _can_open_delivery_window(subscriber, now: datetime) -> bool:
    if subscriber.last_email_sent_time is None:
        return True
    hours = subscriber.send_frequency or 24
    return now >= subscriber.last_email_sent_time + timedelta(hours=hours)


def _group_pending_by_platform(pending_rows) -> dict[str, list[str]]:
    by_platform: dict[str, list[str]] = {}
    for row in pending_rows:
        by_platform.setdefault(row.platform, []).append(row.article_url)
    return by_platform


def _drain_subscriber_in_window(
    subscriber_service,
    articles_db_manager,
    subscriber,
    window_end: datetime,
    stats: dict,
) -> bool:
    """
    在投递窗口内按部门尽力发送。返回是否应关闭窗口（队列空或窗口已到期）。
    """
    from official_document_crawler.crawler.database import Article

    db = subscriber_service.db_manager
    art_session = articles_db_manager.get_session()
    window_expired = False
    try:
        while datetime.now() < window_end:
            pending_rows = db.list_pending_for_subscriber(subscriber.id)
            if not pending_rows:
                break

            by_platform = _group_pending_by_platform(pending_rows)
            platform_names = sorted(by_platform.keys())
            progressed = False

            for idx, platform in enumerate(platform_names):
                if datetime.now() >= window_end:
                    window_expired = True
                    break

                urls = by_platform[platform]
                articles = (
                    art_session.query(Article)
                    .filter(Article.url.in_(urls))
                    .order_by(Article.date.desc(), Article.detail_time.desc())
                    .all()
                )
                if not articles:
                    log_abnormal(
                        "push",
                        f"待推队列有记录但文章库无正文 subscriber={subscriber.email} "
                        f"platform={platform} urls={len(urls)}",
                    )
                    continue

                if db.has_outbox_for_urls(subscriber.id, platform, urls):
                    logger.debug(
                        "跳过 live 发送，outbox 重试中 subscriber=%s platform=%s",
                        subscriber.email,
                        platform,
                    )
                    continue

                ok, err = subscriber_service.send_platform_digest_to_subscriber(
                    subscriber,
                    platform,
                    articles,
                    update_sent_time=False,
                )
                if ok:
                    stats["digests_ok"] += 1
                    progressed = True
                    _sync_one_platform_outbox(
                        db, platform, articles, subscriber.id, True, None
                    )
                else:
                    stats["digests_failed"] += 1
                    log_abnormal(
                        "email",
                        f"推送失败 subscriber={subscriber.email} platform={platform} "
                        f"articles={len(articles)} err={err}",
                    )
                    _sync_one_platform_outbox(
                        db, platform, articles, subscriber.id, False, err
                    )

                if (
                    idx < len(platform_names) - 1
                    and EMAIL_PUSH_INTERVAL_SECONDS > 0
                ):
                    time.sleep(EMAIL_PUSH_INTERVAL_SECONDS)

            if window_expired:
                break
            if not progressed:
                break
        if datetime.now() >= window_end:
            window_expired = True
    finally:
        art_session.close()

    return window_expired or not db.list_pending_for_subscriber(subscriber.id)


def _process_one_subscriber(subscriber_service, articles_db_manager, subscriber_id, stats):
    db = subscriber_service.db_manager
    subscriber = db.get_subscriber_for_push(subscriber_id)
    if not subscriber:
        return

    pending_count = len(db.list_pending_for_subscriber(subscriber_id))
    if pending_count == 0:
        if subscriber.push_delivery_started_at is not None:
            db.close_delivery_window(subscriber_id)
            stats["windows_closed"] += 1
        return

    now = datetime.now()
    started = subscriber.push_delivery_started_at

    if started is not None:
        window_end = _delivery_window_end(subscriber, started)
        if now >= window_end:
            remaining = len(db.list_pending_for_subscriber(subscriber_id))
            db.close_delivery_window(subscriber_id, now)
            stats["windows_closed"] += 1
            if remaining > 0:
                stats["windows_closed_with_backlog"] += 1
                log_abnormal(
                    "push",
                    f"投递窗口到期仍有积压 subscriber={subscriber.email} "
                    f"pending={remaining} frequency_h={subscriber.send_frequency}",
                )
            return
    else:
        if not _can_open_delivery_window(subscriber, now):
            return
        db.start_delivery_window(subscriber_id, now)
        subscriber = db.get_subscriber_for_push(subscriber_id)
        if not subscriber or not subscriber.push_delivery_started_at:
            return
        started = subscriber.push_delivery_started_at
        window_end = _delivery_window_end(subscriber, started)

    stats["subscribers_processed"] += 1
    should_close = _drain_subscriber_in_window(
        subscriber_service,
        articles_db_manager,
        subscriber,
        window_end,
        stats,
    )

    if should_close:
        remaining = len(db.list_pending_for_subscriber(subscriber_id))
        db.close_delivery_window(subscriber_id, datetime.now())
        stats["windows_closed"] += 1
        if remaining > 0:
            stats["windows_closed_with_backlog"] += 1
            log_abnormal(
                "push",
                f"投递窗口关闭后仍有积压 subscriber={subscriber.email} pending={remaining}",
            )


def run_one_push_cycle(subscriber_service, articles_db_manager) -> dict:
    stats = {
        "subscribers_processed": 0,
        "digests_ok": 0,
        "digests_failed": 0,
        "windows_closed": 0,
        "windows_closed_with_backlog": 0,
    }
    db = subscriber_service.db_manager
    subscriber_ids = db.list_subscriber_ids_with_pending()

    for sub_id in subscriber_ids:
        try:
            _process_one_subscriber(
                subscriber_service, articles_db_manager, sub_id, stats
            )
        except Exception as e:
            log_abnormal("push", f"处理订阅者 id={sub_id} 异常", e)

    stats["last_run_at"] = datetime.now().isoformat()
    return stats


def run_push_worker_loop(subscriber_service, articles_db_manager):
    global _last_cycle_stats
    print(
        f"[{datetime.now()}] 推送消费者线程已启动：每 {PUSH_POLL_SECONDS}s 检查待推队列与投递窗口"
    )
    while True:
        try:
            with _push_cycle_lock:
                cycle = run_one_push_cycle(subscriber_service, articles_db_manager)
                _last_cycle_stats.update(cycle)
        except Exception as e:
            log_abnormal("push", "推送消费者周期异常", e)
            logger.exception("push worker cycle failed")
        time.sleep(PUSH_POLL_SECONDS)
