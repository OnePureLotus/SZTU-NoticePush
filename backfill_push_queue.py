"""
将文章库中已有文章写入待推送队列（升级后补漏用，可能产生重复邮件若此前已成功推送过）。

用法:
  python backfill_push_queue.py
  python backfill_push_queue.py --since 2025-10-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_document_crawler.crawler.database import Article, DatabaseManager
from email_subscriber.subscriber_manager import SubscriberService
from email_subscriber.push_pipeline import enqueue_articles_for_all_subscribers


def main() -> int:
    parser = argparse.ArgumentParser(description="将历史文章写入待推送队列")
    parser.add_argument(
        "--since",
        help="仅处理 date >= YYYY-MM-DD 的文章（Article.date 字段）",
    )
    args = parser.parse_args()

    articles_db = DatabaseManager()
    service = SubscriberService()
    session = articles_db.get_session()
    try:
        q = session.query(Article)
        if args.since:
            q = q.filter(Article.date >= args.since)
        articles = q.all()
    finally:
        session.close()

    if not articles:
        print("没有匹配的文章")
        return 0

    added, skipped = enqueue_articles_for_all_subscribers(service, articles)
    print(
        f"处理 {len(articles)} 篇文章，新增队列 {added} 条，跳过 {skipped} 条；"
        f"到期用户将在下一轮爬取或推送冲刷时收到邮件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
